"""The first-party directories and the map from dotted names to files (ADR
0030 Decision 3, *First-party directories* and *How a dotted name is
placed*, as ADR 0035 narrows it).

The map is built without running code. Python's own path finder,
``importlib.util.find_spec``, is the oracle: it runs in a child process on
generated layouts, and the two must place every name alike.
"""

import functools
import importlib
import importlib.machinery
import json
import os
import site
import subprocess
import sys
import sysconfig
import textwrap
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from neolith import _check_source
from neolith._check_source import (
    Edge,
    Source,
    app_files,
    findings,
    first_party,
    is_test_module,
    library_directories,
    module_map,
)

_EXTENSION = importlib.machinery.EXTENSION_SUFFIXES[0]

# The oracle: find_spec for each name, and the file Python loads for it, in
# a process whose path starts with the given directories.
_ORACLE = """\
import importlib.util, json, sys
directories, names = json.loads(sys.argv[1])
sys.path[:0] = directories
found = {}
for name in names:
    try:
        spec = importlib.util.find_spec(name)
    except Exception as error:
        found[name] = ["error", type(error).__name__]
        continue
    if spec is None:
        found[name] = None
    elif spec.origin is None:
        found[name] = ["namespace", list(spec.submodule_search_locations)]
    else:
        found[name] = ["file", spec.origin]
print(json.dumps(found))
"""


def _oracle(directories: list[str], names: list[str]) -> dict[str, object]:
    ran = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-S", "-c", _ORACLE, json.dumps([directories, names])],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    found: dict[str, object] = json.loads(ran.stdout)
    return found


_BASE_PATH = """\
import json, sys
print(json.dumps(sys.path))
"""


@functools.cache
def _base_path() -> list[str]:
    """The child's own path, which follows the directories it is given."""
    ran = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-S", "-c", _BASE_PATH],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    path: list[str] = json.loads(ran.stdout)
    return path


# Names that are and are not identifiers, and two the standard library
# holds: a module and a built-in module.
_NAMES = st.sampled_from(["a", "b", "0001_m", "my-x", "locale", "sys"])
_LEAVES = st.sampled_from(
    [
        "py",
        "pyc",
        "ext",
        "cache",
        "dotted file",
        "dotted dir",
        "linked package",
        "linked directory",
        "broken py",
    ]
)


_Tree = list[tuple[str, str, list[object]]]


def _tree(depth: int) -> st.SearchStrategy[_Tree]:
    leaf: st.SearchStrategy[tuple[str, str, list[object]]] = st.tuples(
        _NAMES, _LEAVES, st.just([])
    )
    if depth == 0:
        return st.lists(leaf, max_size=4)
    inner: st.SearchStrategy[tuple[str, str, list[object]]] = st.tuples(
        _NAMES, st.sampled_from(["package", "namespace"]), _tree(depth - 1)
    )
    return st.lists(st.one_of(leaf, inner), max_size=4)


def _write(
    directory: Path, tree: list[tuple[str, str, list[object]]], elsewhere: Path
) -> list[Path]:
    """Write ``tree`` under ``directory``, with the targets of its links
    under ``elsewhere``; return the module files written, other than those
    reached only through a link.
    """
    written: list[Path] = []
    for name, kind, below in tree:
        if kind in ("package", "namespace"):
            target = directory / name
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                continue
            target.mkdir(exist_ok=True)
            if kind == "package" and not (target / "__init__.py").exists():
                (target / "__init__.py").write_text("")
                written.append(target / "__init__.py")
            written += _write(target, below, elsewhere)  # type: ignore[arg-type]
            continue
        if kind in ("linked package", "linked directory"):
            target = directory / name
            if target.exists() or target.is_symlink():
                continue
            linked = elsewhere / f"{len(list(elsewhere.iterdir()))}"
            linked.mkdir()
            (
                linked / ("__init__.py" if kind == "linked package" else "m.py")
            ).write_text("")
            target.symlink_to(linked, target_is_directory=True)
            continue
        file = {
            "py": f"{name}.py",
            "pyc": f"{name}.pyc",
            "ext": f"{name}{_EXTENSION}",
            "cache": "__pycache__",
            "dotted file": f"{name}.v2.py",
            "dotted dir": f"{name}.d",
            "broken py": f"{name}.py",
        }[kind]
        target = directory / file
        if target.exists() or target.is_symlink():
            continue
        if kind in ("cache", "dotted dir"):
            target.mkdir()
            (target / "__init__.py").write_text("")
            (target / "hidden.py").write_text("")
        elif kind == "broken py":
            target.symlink_to(elsewhere / "nothing.py")
        else:
            target.write_text("")
            if kind != "dotted file":
                written.append(target)
    return written


def _dotted(path: Path, roots: list[Path]) -> str:
    """A written file's dotted name, from its innermost root."""
    root = max((r for r in roots if r in path.parents), key=lambda r: len(r.parts))
    parts = list(path.relative_to(root).parts)
    stem = parts[-1].split(".")[0]
    if stem == "__init__":
        parts = parts[:-1]
    else:
        parts[-1] = stem
    return ".".join(parts)


@st.composite
def _layouts(
    draw: st.DrawFn,
) -> tuple[list[str], list[list[tuple[str, str, list[object]]]]]:
    roots = draw(
        st.sampled_from(
            [
                ["r0"],
                ["r0", "r1"],
                ["r1", "r0"],
                ["r0", "r0/inner"],
                ["lib", "r0"],
                ["r0", "lib"],
            ]
        )
    )
    trees = [draw(_tree(2)) for _ in roots]
    return roots, trees


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(layout=_layouts())
def test_the_map_places_each_name_where_python_s_path_finder_does(
    tmp_path_factory: pytest.TempPathFactory,
    layout: tuple[list[str], list[list[tuple[str, str, list[object]]]]],
) -> None:
    base = tmp_path_factory.mktemp("layout")
    elsewhere = base / "elsewhere"
    elsewhere.mkdir()
    names, trees = layout
    # "lib" is on the path but is no first-party directory, as a library
    # directory is.
    roots = [base / name for name in names]
    owned = [base / name for name in names if name != "lib"]
    written: list[Path] = []
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
    for root, tree in zip(roots, trees, strict=True):
        written += _write(root, tree, elsewhere)
    written = [path for path in written if base / "lib" not in path.parents]
    directories = [str(root) for root in roots]
    first = [str(root) for root in owned]
    placed = module_map(first, [*directories, *_base_path()])
    expected = {_dotted(path, owned) for path in written} - {""}
    # Each top-level package of a first-party directory, walked as an
    # app's package.
    apps = {
        name: app_files(name, [os.path.dirname(where.path)])[0]
        for name, where in placed.items()
        if "." not in name
        and where.path is not None
        and os.path.basename(where.path).startswith("__init__.")
    }
    listed = {file.module for files in apps.values() for file in files}
    found = _oracle(directories, sorted(set(placed) | expected | listed))
    for name, where in placed.items():
        if where.path is None:
            assert found[name] == ["namespace", list(where.portions)], name
        else:
            assert found[name] == ["file", where.path], name
    # Each file Python loads for its name is in the map under that name.
    for path in written:
        name = _dotted(path, owned)
        if name and found[name] == ["file", str(path)]:
            assert placed[name].path == str(path), name
    # An app's walk gives each name the file Python loads for it, once, and
    # misses none of them.
    for name, files in apps.items():
        modules = [file.module for file in files]
        assert len(modules) == len(set(modules)), name
        for file in files:
            if os.path.isfile(file.path):
                assert found[file.module] == ["file", file.path], file
            else:
                assert found[file.module] is None, file
        package = os.path.dirname(placed[name].path or "")
        for path in written:
            module = _dotted(path, owned)
            if Path(package) in path.parents and found[module] == ["file", str(path)]:
                assert module in modules, module


def test_a_namespace_package_spans_first_party_directories(tmp_path: Path) -> None:
    for root in ("one", "two"):
        (tmp_path / root / "shared").mkdir(parents=True)
        (tmp_path / root / "shared" / f"{root}.py").write_text("")
    placed = module_map([str(tmp_path / "one"), str(tmp_path / "two")])
    assert placed["shared"].path is None
    assert placed["shared"].portions == (
        str(tmp_path / "one" / "shared"),
        str(tmp_path / "two" / "shared"),
    )
    assert placed["shared.one"].path == str(tmp_path / "one" / "shared" / "one.py")
    assert placed["shared.two"].path == str(tmp_path / "two" / "shared" / "two.py")


def test_python_s_order_picks_between_two_files_of_one_name(tmp_path: Path) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    for root in (one, two):
        (root / "pkg").mkdir(parents=True)
        (root / "pkg" / "__init__.py").write_text("")
        (root / "mod.py").write_text("")
    (one / "pkg.py").write_text("")
    (two / "ns").mkdir()
    (one / "ns.py").write_text("")
    placed = module_map([str(one), str(two)])
    # A package before a module of its name, the first directory first, and
    # a module before a namespace portion.
    assert placed["pkg"].path == str(one / "pkg" / "__init__.py")
    assert placed["mod"].path == str(one / "mod.py")
    assert placed["ns"].path == str(one / "ns.py")
    assert module_map([str(two), str(one)])["mod"].path == str(two / "mod.py")


def test_a_nested_first_party_directory_names_its_files_itself(tmp_path: Path) -> None:
    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / "src" / "app" / "__init__.py").write_text("")
    (tmp_path / "manage.py").write_text("")
    placed = module_map([str(tmp_path), str(tmp_path / "src")])
    assert sorted(placed) == ["app", "manage"]
    assert placed["app"].root == str(tmp_path / "src")
    assert placed["app"].parts() == ["app", "__init__.py"]


def test_a_virtual_environment_and_a_linked_directory_are_skipped(
    tmp_path: Path,
) -> None:
    (tmp_path / "venv" / "lib").mkdir(parents=True)
    (tmp_path / "venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (tmp_path / "venv" / "lib" / "site.py").write_text("")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "m.py").write_text("")
    root = tmp_path / "project"
    root.mkdir()
    (root / "env").symlink_to(tmp_path / "venv", target_is_directory=True)
    (root / ".venv").mkdir()
    (root / ".venv" / "m.py").write_text("")
    (root / "local").mkdir()
    (root / "local" / "pyvenv.cfg").write_text("")
    (root / "local" / "m.py").write_text("")
    (root / "linked").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    (root / "kept.py").write_text("")
    assert sorted(module_map([str(root)])) == ["kept"]


def test_first_party_directories_follow_the_path_and_leave_out_libraries(
    tmp_path: Path,
) -> None:
    project, library, other = tmp_path / "project", tmp_path / "lib", tmp_path / "other"

    class Spec:
        def __init__(self, locations: list[str] | None, origin: str | None) -> None:
            self.submodule_search_locations = locations
            self.origin = origin

    class Module:
        def __init__(self, spec: Spec) -> None:
            self.__spec__ = spec

    modules = {
        "app": Module(Spec([str(project / "app")], str(project / "app/__init__.py"))),
        "settings": Module(Spec(None, str(other / "settings.py"))),
        "installed": Module(Spec([str(library / "installed")], None)),
        "spread": Module(Spec([str(project / "spread"), str(other / "spread")], None)),
        "built": Module(Spec(None, "built-in")),
    }
    found = first_party(
        ["settings", "app", "installed", "spread", "built", "absent"],
        modules,
        [str(other), "", str(project), str(library)],
        [str(library)],
    )
    assert found == [str(other), str(project)]


_USER_SITE = """\
import json, sys
import mine, theirs
from neolith._check_source import first_party, library_directories
print(json.dumps(first_party(["mine", "theirs"], sys.modules, sys.path,
                             library_directories())))
"""


def test_an_app_in_the_user_site_directory_is_not_first_party(tmp_path: Path) -> None:
    base = tmp_path / "userbase"
    probe = subprocess.run(
        [sys.executable, "-c", "import site; print(site.getusersitepackages())"],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONUSERBASE": str(base)},
    )
    user_site = Path(probe.stdout.strip())
    (user_site / "theirs").mkdir(parents=True)
    (user_site / "theirs" / "__init__.py").write_text("")
    project = tmp_path / "project"
    (project / "mine").mkdir(parents=True)
    (project / "mine" / "__init__.py").write_text("")
    ran = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _USER_SITE],
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "PYTHONUSERBASE": str(base),
            "PYTHONPATH": os.pathsep.join([str(project), str(user_site), *sys.path]),
        },
    )
    assert json.loads(ran.stdout) == [str(project)]


def test_the_library_directories_hold_the_standard_library_and_site() -> None:
    found = library_directories()
    paths = sysconfig.get_paths()
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        assert paths[key] in found
    assert site.getusersitepackages() in found


@pytest.mark.parametrize(
    ("path", "test"),
    [
        ("conftest.py", True),
        ("tests.py", True),
        ("test_views.py", True),
        ("views_test.py", True),
        ("shop/tests/helpers.py", True),
        ("tests/fixtures/data.py", True),
        ("shop/conftest.py", True),
        ("shop/views.py", False),
        ("shop/testing.py", False),
        ("shop/test.py", False),
        ("shop/contest.py", False),
        ("shop/test_data/x.py", False),
        ("shop/tests.d/x.py", False),
        ("tests", False),
        ("", False),
    ],
)
def test_a_test_module_is_named_by_its_file(path: str, test: bool) -> None:
    assert is_test_module(path.split("/") if path else []) is test


def test_the_map_holds_a_non_identifier_name(tmp_path: Path) -> None:
    (tmp_path / "app" / "migrations").mkdir(parents=True)
    for file in ["app/__init__.py", "app/migrations/__init__.py"]:
        (tmp_path / file).write_text("")
    (tmp_path / "app" / "migrations" / "0001_initial.py").write_text("")
    (tmp_path / "app" / "my-module.py").write_text(textwrap.dedent(""))
    assert sorted(module_map([str(tmp_path)])) == [
        "app",
        "app.migrations",
        "app.migrations.0001_initial",
        "app.my-module",
    ]


def test_first_party_directories_are_sorted_by_the_path_not_by_the_apps(
    tmp_path: Path,
) -> None:
    project, other = tmp_path / "project", tmp_path / "other"

    class Spec:
        def __init__(self, origin: str) -> None:
            self.submodule_search_locations = None
            self.origin = origin

    class Module:
        def __init__(self, origin: str) -> None:
            self.__spec__ = Spec(origin)

    modules = {
        "app": Module(str(project / "app.py")),
        "settings": Module(str(other / "settings.py")),
    }
    path = [str(other), str(project)]
    assert first_party(["app", "settings"], modules, path, []) == [
        str(other),
        str(project),
    ]
    assert first_party(["settings", "app"], modules, path[::-1], []) == [
        str(project),
        str(other),
    ]


def test_a_library_directory_reached_through_a_link_is_a_library(
    tmp_path: Path,
) -> None:
    library = tmp_path / "lib"
    (library / "installed").mkdir(parents=True)
    (tmp_path / "lib64").symlink_to(library, target_is_directory=True)

    spec = SimpleNamespace(
        submodule_search_locations=[str(tmp_path / "lib64" / "installed")], origin=None
    )
    modules = {"installed": SimpleNamespace(__spec__=spec)}
    assert first_party(["installed"], modules, [], [str(library)]) == []
    assert first_party(["installed"], modules, [], [str(tmp_path / "lib64")]) == []
    assert first_party(["installed"], modules, [], [str(tmp_path / "x")]) == [
        str(tmp_path / "lib64")
    ]


@pytest.mark.parametrize(
    ("layout", "name", "first"),
    [
        # A directory named as a module of the standard library, as Django's
        # LOCALE_PATHS often is, or as a library's package: a namespace
        # portion loses to them wherever it stands on the path.
        (["locale/de/LC_MESSAGES/django.po"], "locale", True),
        (["email/x.txt"], "email", True),
        # A package or module loses to one earlier on the path.
        (["json/__init__.py"], "json", False),
        (["json.py"], "json", False),
        # A built-in module, wherever it stands.
        (["sys.py"], "sys", True),
    ],
)
def test_a_name_python_imports_from_elsewhere_is_not_first_party(
    tmp_path: Path, layout: list[str], name: str, first: bool
) -> None:
    for file in layout:
        (tmp_path / file).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / file).write_text("")
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / "__init__.py").write_text("")
    path = [str(tmp_path), *sys.path] if first else [*sys.path, str(tmp_path)]
    placed = module_map([str(tmp_path)], path)
    assert name not in placed
    assert "shop" in placed
    # The nearest admitted shape: a package or module first on the path.
    if not first:
        assert name in module_map([str(tmp_path)], [str(tmp_path), *sys.path])


def test_a_namespace_shared_with_a_library_is_placed_as_python_places_it(
    tmp_path: Path,
) -> None:
    project, library = tmp_path / "project", tmp_path / "lib"
    for file in [
        project / "acme" / "billing" / "__init__.py",
        project / "acme" / "common" / "__init__.py",
        project / "acme" / "own.py",
        library / "acme" / "common.py",
        library / "acme" / "own" / "__init__.py",
    ]:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("")
    path = [str(library), str(project)]
    placed = module_map([str(project)], path)
    found = _oracle(path, ["acme", "acme.billing", "acme.common", "acme.own"])
    assert found["acme"] == [
        "namespace",
        [str(library / "acme"), str(project / "acme")],
    ]
    assert placed["acme"].portions == (str(library / "acme"), str(project / "acme"))
    assert placed["acme"].root == str(project)
    assert placed["acme.billing"].path == found["acme.billing"][1]  # type: ignore[index]
    # The library's files win the names it holds first.
    assert "acme.common" not in placed
    assert "acme.own" not in placed
    assert found["acme.common"] == ["file", str(library / "acme" / "common.py")]


# The source of one run, over a host.


def _host(tmp_path: Path) -> tuple[Path, Path]:
    project, library = tmp_path / "project", tmp_path / "lib"
    for file, text in [
        ("project/acme/__init__.py", ""),
        ("project/acme/billing/__init__.py", ""),
        (
            "project/acme/billing/views.py",
            "from acme import common\nimport acme.common\n",
        ),
        ("project/acme/billing/desk/__init__.py", ""),
        ("project/acme/billing/desk/broken.py", "def (\n"),
        ("project/acme/billing/broken.py", "def (\n"),
        ("project/conf/settings.py", ""),
        ("project/conf/__init__.py", ""),
        ("project/elsewhere/billing/extra.py", "def (\n"),
        ("lib/acme_lib/__init__.py", ""),
    ]:
        (tmp_path / file).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / file).write_text(text)
    return project, library


@pytest.fixture
def clean_modules() -> Iterator[None]:
    before = set(sys.modules)
    try:
        yield
    finally:
        for name in set(sys.modules) - before:
            del sys.modules[name]


def test_the_source_of_a_run_reads_its_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_modules: None
) -> None:
    project, _ = _host(tmp_path)
    settings_dir = tmp_path / "settings_home"
    (settings_dir / "deploy").mkdir(parents=True)
    (settings_dir / "deploy" / "__init__.py").write_text("")
    (settings_dir / "deploy" / "prod.py").write_text("")
    monkeypatch.syspath_prepend(str(settings_dir))
    monkeypatch.syspath_prepend(str(project))
    importlib.import_module("acme.billing")
    importlib.import_module("deploy.prod")
    package = sys.modules["acme.billing"]
    # The app's __path__ is read, not the directory Django gives it.
    config = SimpleNamespace(
        name="acme.billing",
        module=SimpleNamespace(
            __path__=[*package.__path__, str(project / "elsewhere" / "billing")]
        ),
        path=str(tmp_path / "nowhere"),
    )

    def owner(name: str) -> str | None:
        if name.startswith("acme.billing.desk"):
            return "desk"
        return "billing" if name.startswith("acme.billing") else None

    source = Source({"billing": config}, owner, settings_module="deploy.prod")  # type: ignore[dict-item]
    assert source.first_party() == [str(project), str(settings_dir)]
    assert sorted(file.module for file in source.files()["billing"]) == [
        "acme.billing",
        "acme.billing.broken",
        "acme.billing.extra",
        "acme.billing.views",
    ]
    # A first-party name the map holds, one it does not, and a loaded
    # library module.
    assert source.known("acme.billing.views")
    assert source.known("deploy.prod")
    assert not source.known("acme.billing.gone")
    assert source.known("json")
    assert not source.known("no_such_module_anywhere")
    found = sorted(
        (finding.path or "", finding.message.id) for finding in findings(source)
    )
    assert found == [
        (str(project / "acme" / "billing" / "broken.py"), "neolith.E112"),
        (str(project / "elsewhere" / "billing" / "extra.py"), "neolith.E112"),
    ]


def test_an_import_of_a_library_module_in_a_shared_namespace_keeps_its_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_modules: None
) -> None:
    project, library = tmp_path / "project", tmp_path / "lib"
    for file, text in [
        ("project/acme/billing/__init__.py", ""),
        (
            "project/acme/billing/views.py",
            "from acme import common\nimport acme.common\n",
        ),
        ("lib/acme/common.py", ""),
    ]:
        (tmp_path / file).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / file).write_text(text)
    monkeypatch.syspath_prepend(str(library))
    monkeypatch.syspath_prepend(str(project))
    importlib.import_module("acme.billing")
    importlib.import_module("acme.common")
    libraries = [*library_directories(), str(library)]
    monkeypatch.setattr(_check_source, "library_directories", lambda: libraries)
    config = SimpleNamespace(
        name="acme.billing",
        module=sys.modules["acme.billing"],
        path=str(project / "acme" / "billing"),
    )
    source = Source({"billing": config}, lambda name: "billing")  # type: ignore[dict-item]
    assert source.first_party() == [str(project)]
    (views,) = [m for m in source.modules("billing") if m.file.module.endswith("views")]
    assert source.edges(views) == [Edge("acme.common", 1), Edge("acme.common", 2)]
