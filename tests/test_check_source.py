"""Reading an app's source: which files are read, how a file is read, and
the findings that come from reading alone, ``neolith.E112`` and
``neolith.W104`` (ADR 0030 Decision 3, as ADR 0035 narrows it).

The walk is checked against Python's own listing, ``pkgutil.iter_modules``,
on generated package directories. The findings are checked through the
command, in a child process (``tests/checking.py``).
"""

import ast
import importlib.machinery
import itertools
import os
import pkgutil
import re
import string
import symtable
import sys
import textwrap
import types
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from neolith import _check_source
from neolith._check_source import File, app_files, read, split
from tests.checking import Project, Ran

_EXTENSION = importlib.machinery.EXTENSION_SUFFIXES[0]
_UNREADABLE_HINT = (
    "make it a file Python can import; the check judges nothing else in it"
)
_SOURCELESS_HINT = (
    "keep the module's source in the app, so that the check can read what it does "
    "across the boundary"
)
_ENTRIES = ["neolith check", "manage.py checkapps"]
_SUMMARY = re.compile(
    r"neolith check: (\d+) errors, (\d+) errors in report-only apps, (\d+) warnings"
)


# Which files of an app.


@pytest.mark.parametrize(
    ("name", "parts"),
    [
        ("x.py", ("x", ".py")),
        ("__init__.py", ("__init__", ".py")),
        ("0001_initial.py", ("0001_initial", ".py")),
        ("my-module.py", ("my-module", ".py")),
        ("x.pyc", ("x", ".pyc")),
        (f"x{_EXTENSION}", ("x", _EXTENSION)),
        ("x.y.py", None),
        (".py", None),
        ("x.txt", None),
        ("x.py.txt", None),
        ("x", None),
    ],
)
def test_a_file_s_module_name_is_its_name_less_a_suffix_python_loads(
    name: str, parts: tuple[str, str] | None
) -> None:
    assert split(name) == parts


def test_a_generated_migrations_package_is_read(tmp_path: Path) -> None:
    package = tmp_path / "app" / "migrations"
    package.mkdir(parents=True)
    for name in ["__init__", "0001_initial", "0002_add-field", "0003_more"]:
        (package / f"{name}.py").write_text("x = 1\n")
    files, failed = app_files("app.migrations", [str(package)])
    assert failed == []
    assert sorted(file.module for file in files) == [
        "app.migrations",
        "app.migrations.0001_initial",
        "app.migrations.0002_add-field",
        "app.migrations.0003_more",
    ]
    assert {file.package for file in files} == {"app.migrations"}
    assert all(read(file).problem is None for file in files)


_NAME = st.one_of(
    st.sampled_from(
        ["a", "b", "0001_initial", "my-mod", "x y", "_p", "__init__", "__pycache__"]
    ),
    st.text(string.ascii_lowercase + string.digits + "-_", min_size=1, max_size=4),
)
_FILE_SUFFIX = st.sampled_from([".py", ".pyc", _EXTENSION, ".txt", ".y.py", ""])
# Inside a directory: nothing, an __init__ of a kind, or a module.
_INSIDE = st.sampled_from(
    [None, "__init__.py", "__init__.pyc", f"__init__{_EXTENSION}", "m.py"]
)


@st.composite
def _layout(draw: st.DrawFn) -> tuple[list[str], list[tuple[str, str | None]]]:
    files = draw(st.lists(st.tuples(_NAME, _FILE_SUFFIX), max_size=8))
    directories = draw(st.lists(st.tuples(_NAME, st.booleans(), _INSIDE), max_size=4))
    names = [name + suffix for name, suffix in files]
    folders = [
        (name + (".d" if dotted else ""), inside)
        for name, dotted, inside in directories
    ]
    return names, folders


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(layout=_layout())
def test_the_walk_reads_exactly_the_names_pkgutil_lists(
    tmp_path_factory: pytest.TempPathFactory,
    layout: tuple[list[str], list[tuple[str, str | None]]],
) -> None:
    root = tmp_path_factory.mktemp("package")
    names, folders = layout
    for folder, inside in folders:
        target = root / folder
        if target.exists():
            continue
        target.mkdir()
        if inside is not None and folder != "__pycache__":
            (target / inside).write_text("")
    for name in names:
        target = root / name
        if name and not target.exists():
            target.write_text("")
    files, failed = app_files("p", [str(root)])
    assert failed == []
    # The modules directly in the package: a module file, or a package whose
    # __init__ the walk lists, with or without source.
    walked = {
        file.module.removeprefix("p.")
        for file in files
        if file.module.startswith("p.") and file.module.count(".") == 1
    }
    listed = {info.name for info in pkgutil.iter_modules([str(root)])}
    assert walked == listed


def test_a_symbolic_link_to_a_directory_is_not_followed(tmp_path: Path) -> None:
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "m.py").write_text("")
    package = tmp_path / "p"
    package.mkdir()
    (package / "linked").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    (package / "kept.py").write_text("")
    (package / "linked_file.py").symlink_to(tmp_path / "elsewhere" / "m.py")
    files, _ = app_files("p", [str(package)])
    assert sorted(file.module for file in files) == ["p.kept", "p.linked_file"]


def test_a_namespace_directory_inside_an_app_is_read(tmp_path: Path) -> None:
    (tmp_path / "ns" / "deeper").mkdir(parents=True)
    (tmp_path / "ns" / "deeper" / "m.py").write_text("")
    (tmp_path / "cached" / "__pycache__").mkdir(parents=True)
    (tmp_path / "cached" / "__pycache__" / "m.cpython-313.pyc").write_text("")
    files, _ = app_files("p", [str(tmp_path)])
    assert [(file.module, file.package) for file in files] == [
        ("p.ns.deeper.m", "p.ns.deeper")
    ]


def test_a_module_whose_path_finder_file_has_no_source_is_listed_as_such(
    tmp_path: Path,
) -> None:
    for name in ["alone.pyc", f"fast{_EXTENSION}", "kept.py", "kept.pyc"]:
        (tmp_path / name).write_text("")
    # The path finder loads an extension module before source of its name,
    # and skips a broken link to one.
    (tmp_path / "both.py").write_text("")
    (tmp_path / f"both{_EXTENSION}").write_text("")
    (tmp_path / "linked.py").write_text("")
    (tmp_path / f"linked{_EXTENSION}").symlink_to(tmp_path / "nothing")
    files, _ = app_files("p", [str(tmp_path)])
    assert sorted((file.module, file.source) for file in files) == [
        ("p.alone", False),
        ("p.both", False),
        ("p.fast", False),
        ("p.kept", True),
        ("p.linked", True),
    ]


def _shadowed(root: Path) -> Path:
    """A package ``app`` where each name has a file the path finder never
    loads for it beside the one it does.
    """
    app = root / "app"
    for package in ("x", "y", "z", "q", "so"):
        (app / package).mkdir(parents=True)
        (app / package / "__init__.py").write_text("X = 1\n")
    (app / "__init__.py").write_text("")
    # A module beside a package of its name, which wins.
    (app / "x.py").write_text("def broken(:\n")
    (app / "y.py").write_text("import importlib\nimportlib.import_module(name)\n")
    (app / "z.pyc").write_bytes(b"")
    (app / f"so{_EXTENSION}").write_bytes(b"")
    # A module beside a namespace directory of its name, which it hides.
    (app / "m.py").write_text("M = 1\n")
    (app / "m").mkdir()
    (app / "m" / "inner.py").write_text("def broken(:\n")
    # A module beside a link to a package directory, which wins: the name
    # is a blind spot.
    (app / "s").symlink_to(app / "q", target_is_directory=True)
    (app / "s.py").write_text("def broken(:\n")
    # A link to a directory with no __init__ beside a module: the module.
    (root / "elsewhere").mkdir()
    (app / "n").symlink_to(root / "elsewhere", target_is_directory=True)
    (app / "n.py").write_text("N = 1\n")
    # A broken link, which the path finder skips for a namespace directory.
    (app / "w").mkdir()
    (app / "w" / "inner.py").write_text("W = 1\n")
    (app / "w.py").symlink_to(root / "nothing.py")
    return app


def test_a_file_python_never_loads_for_its_name_is_not_listed(
    tmp_path: Path,
) -> None:
    app = _shadowed(tmp_path)
    files, failed = app_files("app", [str(app)])
    assert failed == []
    assert sorted((file.module, file.path) for file in files) == sorted(
        [
            ("app", str(app / "__init__.py")),
            ("app.m", str(app / "m.py")),
            ("app.n", str(app / "n.py")),
            ("app.q", str(app / "q" / "__init__.py")),
            ("app.so", str(app / "so" / "__init__.py")),
            ("app.w.inner", str(app / "w" / "inner.py")),
            ("app.x", str(app / "x" / "__init__.py")),
            ("app.y", str(app / "y" / "__init__.py")),
            ("app.z", str(app / "z" / "__init__.py")),
        ]
    )
    # Python's own path finder agrees, name by name.
    names = ["app.m", "app.n", "app.q", "app.s", "app.so", "app.w", "app.w.inner"]
    names += ["app.x", "app.y", "app.z"]
    found = _origins(tmp_path, names)
    assert found["app.s"] == str(app / "s" / "__init__.py")
    assert found["app.w"] is None
    for file in files:
        if file.module != "app":
            assert found[file.module] == file.path, file.module


_ORIGINS = """\
import importlib.util, json, sys
root, names = json.loads(sys.argv[1])
sys.path.insert(0, root)
found = {}
for name in names:
    spec = importlib.util.find_spec(name)
    found[name] = None if spec is None else spec.origin
print(json.dumps(found))
"""


def _origins(root: Path, names: list[str]) -> dict[str, str | None]:
    import json
    import subprocess

    ran = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-S", "-c", _ORIGINS, json.dumps([str(root), names])],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    found: dict[str, str | None] = json.loads(ran.stdout)
    return found


def test_a_file_python_never_loads_for_its_name_gives_no_finding(
    tmp_path: Path,
) -> None:
    app = _shadowed(tmp_path)
    (app / "good.py").write_text("def broken(:\n")
    config = types.SimpleNamespace(
        name="app", module=types.SimpleNamespace(__path__=[str(app)]), path=str(app)
    )
    source = _check_source.Source({"app": config}, lambda name: "app")  # type: ignore[dict-item]
    found = [
        (os.path.relpath(finding.path or "", tmp_path), finding.message.id)
        for finding in _check_source.findings(source)
    ]
    assert found == [("app/good.py", "neolith.E112")]


# How a file is read.


@pytest.mark.parametrize(
    ("data", "problem", "line"),
    [
        (b"x = 1\n", None, 1),
        (b"x = '\\d'\n", None, 1),
        (b"# -*- coding: latin-1 -*-\nx = '\xe9'\n", None, 1),
        (b"\xef\xbb\xbfx = 1\n", None, 1),
        (b"x = " + b" + ".join([b"1"] * 3000) + b"\n", None, 1),
        (b"x = 1\ny = (\n", "cannot be compiled: SyntaxError: ", 2),
        (b"x = 1\nreturn 1\n", "cannot be compiled: SyntaxError: ", 2),
        (b"for x in y:\n    pass\nbreak\n", "cannot be compiled: SyntaxError: ", 3),
        (b"x = 1\0\n", "cannot be compiled: SyntaxError: ", 1),
        (b"# -*- coding: no-such -*-\nx = 1\n", "cannot be compiled: SyntaxError: ", 1),
        (b"x = '\xff'\n", "cannot be compiled: SyntaxError: ", 1),
        (b"def f():\n    global x\n    x: int = 1\n", "cannot be compiled: ", 3),
    ],
    ids=[
        "plain",
        "invalid escape, a warning",
        "declared encoding",
        "byte order mark",
        "a 3000-term chain",
        "unclosed",
        "return at module level",
        "break outside a loop",
        "null byte",
        "unknown encoding",
        "bytes that do not decode",
        "annotated global",
    ],
)
def test_a_file_is_read_as_python_imports_it(
    tmp_path: Path, data: bytes, problem: str | None, line: int
) -> None:
    path = tmp_path / "m.py"
    path.write_bytes(data)
    module = read(File(str(path), "m", ""))
    if problem is None:
        assert module.problem is None
        assert module.tree is not None
        assert module.table is not None
        compile(module.tree, str(path), "exec")
    else:
        assert module.problem is not None
        assert module.problem.startswith(problem), module.problem
        assert module.line == line
        assert module.tree is None
    if problem is None:
        return
    with pytest.raises(Exception):  # noqa: B017, PT011
        compile(data, str(path), "exec", dont_inherit=True)


def test_a_file_that_cannot_be_opened_is_kept_as_unreadable(tmp_path: Path) -> None:
    broken = tmp_path / "broken.py"
    broken.symlink_to(tmp_path / "nothing.py")
    os.mkfifo(tmp_path / "pipe.py")
    for path in (broken, tmp_path / "pipe.py", tmp_path / "gone.py"):
        module = read(File(str(path), "m", ""))
        assert module.problem is not None
        assert module.problem.startswith("cannot be read: "), module.problem


def test_the_walk_over_a_tree_never_recurses(tmp_path: Path) -> None:
    depth = sys.getrecursionlimit() * 2
    path = tmp_path / "m.py"
    path.write_text("x = " + " + ".join(["1"] * depth) + "\n")
    module = read(File(str(path), "m", ""))
    assert module.problem is None, module.problem


# E112 and W104 through the command.


class Shop(Project):
    """A host of one app, ``shop``, whose files are written by each test."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.app("shop")
        self.deployer("shop")
        self.settings(["shop"], neolith={"DATABASES": self.databases("shop")})

    def entry(self, entry: str) -> Ran:
        if entry == "neolith check":
            return self.check("shop")
        return self.checkapps()


@pytest.mark.parametrize("entry", _ENTRIES)
def test_each_file_that_cannot_be_read_or_compiled_is_e112(
    tmp_path: Path, entry: str
) -> None:
    shop = Shop(tmp_path)
    shop.write("shop/syntax.py", "x = 1\ny = (\n")
    shop.write("shop/returns.py", "x = 1\nreturn x\n")
    shop.write("shop/migrations/0002_broken.py", "def (\n")
    shop.write("shop/sub/deeper.py", "x = 1\n\n\nbreak\n")
    (shop.root / "shop" / "nul.py").write_bytes(b"x = 1\0\n")
    (shop.root / "shop" / "link.py").symlink_to(shop.root / "nothing.py")
    # Admitted beside them: a warning of the compiler, a long chain, an
    # encoding the file declares, and files no dotted name reaches.
    shop.write("shop/escape.py", "x = '\\d'\n")
    shop.write("shop/chain.py", "x = " + " + ".join(["1"] * 3000) + "\n")
    (shop.root / "shop" / "latin.py").write_bytes(
        b"# -*- coding: latin-1 -*-\nx = '\xe9'\n"
    )
    shop.write("shop/not.a.module.py", "def (\n")
    shop.write("shop/data.d/m.py", "def (\n")
    shop.write("shop/__pycache__/m.py", "def (\n")
    ran = shop.entry(entry)
    expected = [
        ("shop/link.py", 1, "shop.link", "cannot be read: "),
        ("shop/migrations/0002_broken.py", 1, "shop.migrations.0002_broken", "c"),
        ("shop/nul.py", 1, "shop.nul", "cannot be compiled: "),
        ("shop/returns.py", 2, "shop.returns", "cannot be compiled: SyntaxError: "),
        ("shop/sub/deeper.py", 4, "shop.sub.deeper", "cannot be compiled: "),
        ("shop/syntax.py", 2, "shop.syntax", "cannot be compiled: SyntaxError: "),
    ]
    assert len(ran.findings) == len(expected), ran.stdout + ran.stderr
    for line, (path, number, module, problem) in zip(
        ran.findings, expected, strict=True
    ):
        start = f"{path}:{number}: neolith.E112 app shop: module {module}: {problem}"
        assert line.startswith(start), line
        assert line.endswith(f"; {_UNREADABLE_HINT}"), line
    assert (
        ran.summary
        == "neolith check: 6 errors, 0 errors in report-only apps, 0 warnings"
    )
    assert ran.status == 1


@pytest.mark.parametrize("entry", _ENTRIES)
def test_a_module_file_without_source_is_w104(tmp_path: Path, entry: str) -> None:
    shop = Shop(tmp_path)
    shop.write("shop/alone.pyc", "")
    shop.write(f"shop/fast{_EXTENSION}", "")
    # Admitted: bytecode beside its source, and Python's own cache.
    shop.write("shop/kept.py", "x = 1\n")
    shop.write("shop/kept.pyc", "")
    shop.write("shop/__pycache__/kept.cpython-313.pyc", "")
    ran = shop.entry(entry)
    assert ran.findings == [
        f"shop/alone.pyc:1: neolith.W104 app shop: module shop.alone: has no Python "
        f"source, so the check cannot read it; {_SOURCELESS_HINT}",
        f"shop/fast{_EXTENSION}:1: neolith.W104 app shop: module shop.fast: has no "
        f"Python source, so the check cannot read it; {_SOURCELESS_HINT}",
    ], ran.stderr
    assert (
        ran.summary
        == "neolith check: 0 errors, 0 errors in report-only apps, 2 warnings"
    )
    assert ran.status == 0


def test_a_directory_of_an_app_that_cannot_be_listed_is_e112(tmp_path: Path) -> None:
    shop = Shop(tmp_path)
    shop.write("shop/closed/m.py", "x = 1\n")
    closed = shop.root / "shop" / "closed"
    closed.chmod(0)
    try:
        if os.access(closed, os.R_OK):
            pytest.skip("this user reads a directory whatever its mode")
        ran = shop.entry("neolith check")
    finally:
        closed.chmod(0o755)
    assert len(ran.findings) == 1, ran.stdout + ran.stderr
    assert ran.findings[0].startswith(
        "shop/closed:1: neolith.E112 app shop: directory shop/closed: cannot be "
        "read: PermissionError: "
    )
    assert ran.status == 1


def test_a_nested_hosted_app_s_files_are_its_own(tmp_path: Path) -> None:
    project = Project(tmp_path)
    project.app("shop")
    project.app("shop.desk", label="desk")
    project.write("shop/broken.py", "def (\n")
    project.write("shop/desk/broken.py", "def (\n")
    project.deployer("shop", "desk")
    ran = project.check("shop", "shop.desk")
    assert [line.split(": ")[1:3] for line in ran.findings] == [
        ["neolith.E112 app shop", "module shop.broken"],
        ["neolith.E112 app desk", "module shop.desk.broken"],
    ], ran.stdout + ran.stderr


def test_sources_are_read_once_per_run(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("x = 1\n")
    source = _check_source.Source({}, lambda name: None)
    file = File(str(tmp_path / "m.py"), "m", "")
    assert source.read(file) is source.read(file)


# Which statements, and how they resolve.

_KNOWN = frozenset(
    {"pkg", "pkg.a", "pkg.a.b", "pkg.sub", "pkg.sub.m", "pkg.sub.n", "other"}
)


def _edges(text: str, package: str = "pkg.sub") -> list[tuple[str, int]]:
    tree = ast.parse(textwrap.dedent(text))
    return [
        (edge.target, edge.line)
        for edge in _check_source.imports(tree, package, _KNOWN.__contains__)
    ]


@pytest.mark.parametrize(
    ("statement", "targets"),
    [
        ("import pkg.a.b", ["pkg.a.b"]),
        ("import pkg.a.b as x", ["pkg.a.b"]),
        ("import pkg, other", ["pkg", "other"]),
        ("import pkg.a.missing", ["pkg.a"]),
        ("import pkg.a.missing.deeper", ["pkg.a"]),
        ("from pkg.a import b", ["pkg.a.b"]),
        ("from pkg.a import b as x, name", ["pkg.a.b", "pkg.a"]),
        ("from pkg.a.missing import n", ["pkg.a"]),
        ("from pkg.missing.deeper import n", ["pkg"]),
        ("from . import m", ["pkg.sub.m"]),
        ("from . import name", ["pkg.sub"]),
        ("from .m import name", ["pkg.sub.m"]),
        ("from .. import a", ["pkg.a"]),
        ("from ..a import b", ["pkg.a.b"]),
        ("from ..missing import b", ["pkg"]),
        ("from ... import x", []),
        ("from ...deeper import x", []),
        ("from pkg import *", ["pkg"]),
        ("from .m import *", ["pkg.sub.m"]),
        ("from pkg.a.b import *", ["pkg.a.b"]),
        ("import os.path", ["os.path"]),
    ],
)
def test_each_import_form_gives_its_edge(statement: str, targets: list[str]) -> None:
    assert _edges(statement) == [(target, 1) for target in targets]


def test_a_relative_import_in_a_top_level_module_gives_no_edge() -> None:
    assert _edges("from . import m\nfrom .m import x\n", package="") == []


def test_every_import_statement_is_read_wherever_it_stands() -> None:
    text = """\
        from typing import TYPE_CHECKING
        if TYPE_CHECKING:
            import pkg.a
        try:
            import pkg.a.b
        except ImportError:
            import other
        def f():
            from . import m
            class K:
                import pkg.sub.n
            return [__import__ for _ in ()]
        with x:
            from pkg import a
        match y:
            case _:
                from ..a import b
        """
    assert _edges(text) == [
        ("typing", 1),
        ("pkg.a", 3),
        ("pkg.a.b", 5),
        ("other", 7),
        ("pkg.sub.m", 9),
        ("pkg.sub.n", 11),
        ("pkg.a", 14),
        ("pkg.a.b", 17),
    ]


@pytest.mark.skipif(sys.version_info < (3, 15), reason="lazy imports are 3.15's")
def test_a_lazy_import_is_an_import() -> None:  # pragma: no cover
    assert _edges("lazy import pkg.a\nlazy from pkg.a import b\n") == [
        ("pkg.a", 1),
        ("pkg.a.b", 2),
    ]


def _calls(text: str) -> tuple[list[tuple[str, int]], list[tuple[str, str, int]]]:
    text = textwrap.dedent(text)
    tree = ast.parse(text)
    table = symtable.symtable(text, "<fixture>", "exec")
    edges, unread = _check_source.calls(tree, table, "pkg.sub", _KNOWN.__contains__)
    assert all(edge.dynamic for edge in edges)
    return (
        [(edge.target, edge.line) for edge in edges],
        [(call.name, call.what, call.line) for call in unread],
    )


@pytest.mark.parametrize(
    ("text", "targets"),
    [
        ("from importlib import import_module\nimport_module('pkg.a')", ["pkg.a"]),
        ("import importlib\nimportlib.import_module('pkg.a.b')", ["pkg.a.b"]),
        ("import importlib as il\nil.import_module('pkg.a.gone')", ["pkg.a"]),
        (
            "from importlib import import_module\n"
            "import_module('.m', package='pkg.sub')",
            ["pkg.sub.m"],
        ),
        (
            "from importlib import import_module\nimport_module('.m', 'pkg.sub')",
            ["pkg.sub.m"],
        ),
        (
            "from importlib import import_module\nimport_module('..a', __package__)",
            ["pkg.a"],
        ),
        (
            "from importlib import import_module\n"
            "import_module(name='.n', package=__package__)",
            ["pkg.sub.n"],
        ),
        ("from importlib import import_module\nimport_module('.m')", []),
        ("from importlib import import_module\nimport_module('....x', 'pkg.sub')", []),
        ("__import__('pkg.a')", ["pkg.a"]),
        ("__import__('pkg.a', globals(), locals(), [], 0)", ["pkg.a"]),
        # __import__ read as the statement it performs: a relative level
        # against the file's package, and a fromlist as a from import.
        ("__import__('a', globals(), locals(), [], 1)", ["pkg.sub"]),
        ("__import__('m', globals(), None, [], 1)", ["pkg.sub.m"]),
        ("__import__('', globals(), None, ['m', 'gone'], 1)", ["pkg.sub.m", "pkg.sub"]),
        ("__import__('a', globals(), None, ('b',), level=2)", ["pkg.a.b"]),
        ("__import__('pkg.sub', fromlist=['m'])", ["pkg.sub.m"]),
        ("__import__('pkg.sub', None, None, ['*'])", ["pkg.sub"]),
        ("__import__('pkg.sub', fromlist=None)", ["pkg.sub"]),
        # Python refuses these, so they import nothing.
        ("__import__('m', None, None, [], 1)", []),
        ("__import__('m', level=1)", []),
        ("__import__('m', globals(), None, [], 5)", []),
        ("__import__('')", []),
        (
            "from django.utils.module_loading import import_string\n"
            "import_string('pkg.a.Thing')",
            ["pkg.a"],
        ),
        (
            "from django.utils.module_loading import import_string\n"
            "import_string(dotted_path='pkg.a.Thing')",
            ["pkg.a"],
        ),
        (
            "from django.utils.module_loading import import_string\n"
            "import_string('nodot')",
            [],
        ),
        ("from django.urls import include\ninclude('pkg.a')", ["pkg.a"]),
        ("from django.conf.urls import include\ninclude(('pkg.a', 'x'))", ["pkg.a"]),
        (
            "from django.urls import include\ninclude(arg='pkg.a', namespace='n')",
            ["pkg.a"],
        ),
        ("from django.urls import include\ninclude(('pkg.a',))", []),
        ("from django.urls import include\ninclude(['pkg.a'])", []),
        ("from django.urls import include\ninclude(router.urls)", []),
        # __import__ bound by the file is not the builtin, whatever binds it.
        ("__import__ = f\n__import__('pkg.a')", []),
        ("def f(__import__):\n    __import__('pkg.a')", []),
        ("from x import __import__\n__import__('pkg.a')", []),
        ("def f():\n    from x import __import__\n__import__('pkg.a')", []),
        ("[__import__ for __import__ in ()]\n__import__('pkg.a')", []),
        ("from x import *\n__import__('pkg.a')", []),
        # A callee the file binds otherwise is not followed.
        (
            "from importlib import import_module\n"
            "def g(import_module):\n    pass\nimport_module('pkg.a')",
            [],
        ),
        ("import_module('pkg.a')", []),
    ],
)
def test_each_literal_dynamic_import_gives_its_edge(
    text: str, targets: list[str]
) -> None:
    edges, unread = _calls(text)
    assert [target for target, _ in edges] == targets
    assert unread == []


@pytest.mark.parametrize(
    ("text", "unread"),
    [
        ("from neolith import connect\nconnect(name)", ("connect", "argument")),
        ("import neolith\nneolith.connect(*names)", ("connect", "argument")),
        (
            "from neolith import command_scope\ncommand_scope(label)",
            ("command_scope", "argument"),
        ),
        (
            "from neolith import command_scope\ncommand_scope(app=label)",
            ("command_scope", "argument"),
        ),
        (
            "from neolith import command_scope\ncommand_scope(**options)",
            ("command_scope", "argument"),
        ),
        (
            "from importlib import import_module\nimport_module(name)",
            ("import_module", "argument"),
        ),
        (
            "from importlib import import_module\nimport_module('.x', name)",
            ("import_module", "package"),
        ),
        (
            "from importlib import import_module\nimport_module('x', package=name)",
            ("import_module", "package"),
        ),
        ("__import__(name)", ("__import__", "argument")),
        ("__import__('pkg', fromlist=names)", ("__import__", "argument")),
        ("__import__('pkg', None, None, [name])", ("__import__", "argument")),
        ("__import__('pkg', None, None, [], level)", ("__import__", "argument")),
        ("__import__('m', scope, None, [], 1)", ("__import__", "argument")),
        ("__import__('pkg', *rest)", ("__import__", "argument")),
        (
            "from django.utils.module_loading import import_string\n"
            "import_string(dotted_path=path)",
            ("import_string", "argument"),
        ),
        (
            "from django.utils.module_loading import import_string\n"
            "import_string(path)",
            ("import_string", "argument"),
        ),
        ("apps.get_model(label, 'Item')", ("get_model", "argument")),
        ("apps.get_model('shop', model)", ("get_model", "argument")),
        ("apps.get_model(app_label=label)", ("get_model", "argument")),
        ("config.get_model(name)", ("get_model", "argument")),
        ("self.registry.get_model(*parts)", ("get_model", "argument")),
        ("apps.get_app_config(label)", ("get_app_config", "argument")),
        ("x.get_app_config(app_label=label)", ("get_app_config", "argument")),
    ],
)
def test_each_computed_form_is_unread(text: str, unread: tuple[str, str]) -> None:
    found = _calls(text)[1]
    assert found == [(*unread, 2 if "\n" in text else 1)]


@pytest.mark.parametrize(
    "text",
    [
        "from neolith import connect\nconnect('shop.orders.v1')",
        "from neolith import command_scope\ncommand_scope('shop', timeout=t)",
        "from importlib import import_module\nimport_module('x', __package__)",
        "from importlib import import_module\nimport_module('x', None)",
        "apps.get_model('shop', 'Item')",
        "apps.get_model('shop.Item')",
        "config.get_model('Item', False)",
        "apps.get_app_config('shop')",
        "from django.urls import include\ninclude(admin.site.urls)",
        # Not a listed name: a connect the file binds otherwise.
        "from neolith import connect\nconnect = other\nconnect(name)",
        "def f(connect):\n    connect(name)",
        "get_model(label)",
    ],
)
def test_the_nearest_admitted_shapes_are_read(text: str) -> None:
    assert _calls(text)[1] == []


@pytest.mark.parametrize("entry", _ENTRIES)
def test_a_computed_argument_is_w101(tmp_path: Path, entry: str) -> None:
    shop = Shop(tmp_path)
    shop.write(
        "shop/services.py",
        """\
        from django.apps import apps
        from neolith import connect


        def orders(name, label):
            connect("shop.orders.v1")
            apps.get_model("shop", "Item")
            return connect(name), apps.get_model(label, "Item")
        """,
    )
    ran = shop.entry(entry)
    hint = (
        "write it as a literal where it is fixed; the check does not judge this "
        "call otherwise"
    )
    assert ran.findings == [
        "shop/services.py:8: neolith.W101 app shop: module shop.services: connect() "
        f"is called with a computed argument, which the check cannot read; {hint}",
        "shop/services.py:8: neolith.W101 app shop: module shop.services: get_model() "
        f"is called with a computed argument, which the check cannot read; {hint}",
    ], ran.stdout + ran.stderr
    assert _SUMMARY.fullmatch(ran.summary).groups() == ("0", "0", "2")  # type: ignore[union-attr]
    assert ran.status == 0


# The settings module's directory is first-party under both entries.

_FIRST_PARTY_PROBE = """\
import sys
from neolith import _check_source

_findings = _check_source.findings


def findings(source):
    print("first-party:", *source.first_party(), sep="\\n", file=sys.stderr)
    return _findings(source)


_check_source.findings = findings
"""


@pytest.mark.parametrize("entry", _ENTRIES)
def test_the_settings_module_s_directory_is_first_party(
    tmp_path: Path, entry: str
) -> None:
    shop = Shop(tmp_path / "project")
    library = shop.root / "lib"
    library.mkdir()
    (shop.root / "shop").rename(library / "shop")
    probe = tmp_path / "probe"
    probe.mkdir()
    (probe / "sitecustomize.py").write_text(_FIRST_PARTY_PROBE)
    path = os.pathsep.join([str(probe), str(shop.root), str(library), *sys.path])
    if entry == "neolith check":
        ran = shop.check("shop", env={"PYTHONPATH": path})
    else:
        ran = shop.checkapps(env={"PYTHONPATH": path})
    lines = ran.errors
    assert "first-party:" in lines, ran.stdout + ran.stderr
    after = lines[lines.index("first-party:") + 1 :]
    found = list(itertools.takewhile(os.path.isabs, after))
    assert found == [str(shop.root), str(library)], ran.stderr
