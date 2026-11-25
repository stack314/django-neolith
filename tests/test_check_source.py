"""Reading an app's source: which files are read, how a file is read, and
the findings that come from reading alone, ``neolith.E112`` and
``neolith.W104`` (ADR 0030 Decision 3, as ADR 0035 narrows it).

The walk is checked against Python's own listing, ``pkgutil.iter_modules``,
on generated package directories. The findings are checked through the
command, in a child process (``tests/checking.py``).
"""

import importlib.machinery
import os
import pkgutil
import re
import string
import sys
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
    (tmp_path / "both.py").write_text("")
    (tmp_path / f"both{_EXTENSION}").write_text("")
    files, _ = app_files("p", [str(tmp_path)])
    assert sorted((file.module, file.source) for file in files) == [
        ("p.alone", False),
        ("p.both", False),
        ("p.both", True),
        ("p.fast", False),
        ("p.kept", True),
    ]


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
