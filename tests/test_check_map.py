"""The first-party directories and the map from dotted names to files (ADR
0030 Decision 3, *First-party directories* and *How a dotted name is
placed*, as ADR 0035 narrows it).

The map is built without running code. Python's own path finder,
``importlib.util.find_spec``, is the oracle: it runs in a child process on
generated layouts, and the two must place every name alike.
"""

import importlib.machinery
import json
import os
import site
import subprocess
import sys
import sysconfig
import textwrap
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from neolith._check_source import (
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


_NAMES = st.sampled_from(["a", "b", "0001_m", "my-x"])
_LEAVES = st.sampled_from(["py", "pyc", "ext", "cache", "dotted file", "dotted dir"])


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


def _write(directory: Path, tree: list[tuple[str, str, list[object]]]) -> list[Path]:
    """Write ``tree`` under ``directory``; return the module files written."""
    written: list[Path] = []
    for name, kind, below in tree:
        if kind in ("package", "namespace"):
            target = directory / name
            if target.exists() and not target.is_dir():
                continue
            target.mkdir(exist_ok=True)
            if kind == "package" and not (target / "__init__.py").exists():
                (target / "__init__.py").write_text("")
                written.append(target / "__init__.py")
            written += _write(target, below)  # type: ignore[arg-type]
            continue
        file = {
            "py": f"{name}.py",
            "pyc": f"{name}.pyc",
            "ext": f"{name}{_EXTENSION}",
            "cache": "__pycache__",
            "dotted file": f"{name}.v2.py",
            "dotted dir": f"{name}.d",
        }[kind]
        target = directory / file
        if target.exists():
            continue
        if kind in ("cache", "dotted dir"):
            target.mkdir()
            (target / "__init__.py").write_text("")
            (target / "hidden.py").write_text("")
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
        st.sampled_from([["r0"], ["r0", "r1"], ["r1", "r0"], ["r0", "r0/inner"]])
    )
    trees = [draw(_tree(2)) for _ in roots]
    return roots, trees


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(layout=_layouts())
def test_the_map_places_each_name_where_python_s_path_finder_does(
    tmp_path_factory: pytest.TempPathFactory,
    layout: tuple[list[str], list[list[tuple[str, str, list[object]]]]],
) -> None:
    base = tmp_path_factory.mktemp("layout")
    names, trees = layout
    roots = [base / name for name in names]
    written: list[Path] = []
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
    for root, tree in zip(roots, trees, strict=True):
        written += _write(root, tree)
    directories = [str(root) for root in roots]
    placed = module_map(directories)
    expected = {_dotted(path, roots) for path in written} - {""}
    found = _oracle(directories, sorted(set(placed) | expected))
    for name, where in placed.items():
        if where.path is None:
            assert found[name] == ["namespace", list(where.portions)], name
        else:
            assert found[name] == ["file", where.path], name
    # Each file Python loads for its name is in the map under that name.
    for path in written:
        name = _dotted(path, roots)
        if name and found[name] == ["file", str(path)]:
            assert placed[name].path == str(path), name


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
