"""The check's import edges against grimp's, on Django's installed package
(ADR 0030 Decision 3, *How they resolve*, and its Consequences).

grimp 3.19 reads imports with a scanner of its own. On Django's package the
two must give the same modules, the same edges and the same lines. grimp
differs on three forms, each tested on its own below and left out of the
comparison: ``from <m>.<missing> import <n>`` and a name missing two levels
down give grimp no edge, and a relative ``from`` past the top-level package
makes it raise. grimp ships wheels for CPython 3.11 to 3.15 only, so where
it is not installed these tests are skipped.
"""

import ast
import importlib.util
import os
import sys
from pathlib import Path

import django
import pytest

from neolith._check_source import File, app_files, imports, module_map, read

grimp = pytest.importorskip(
    "grimp",
    reason="grimp is installed only on the CPython versions it ships wheels for",
)

_Edges = set[tuple[str, str, int]]


def _other_form(node: ast.AST, package: str, known: set[str], name: str) -> bool:
    """Whether an import statement is one of the forms grimp reads
    otherwise: a ``from`` whose module has no file, or an ``import`` of a
    name whose parent has none.
    """
    if isinstance(node, ast.ImportFrom):
        base = node.module or ""
        if node.level:
            try:
                base = importlib.util.resolve_name("." * node.level + base, package)
            except ImportError:
                return True
        return base.partition(".")[0] == name and base not in known
    if isinstance(node, ast.Import):
        return any(
            alias.name.partition(".")[0] == name
            and alias.name.rpartition(".")[0]
            and alias.name.rpartition(".")[0] not in known
            for alias in node.names
        )
    return False


def _walk(name: str, directory: str) -> tuple[set[str], _Edges, set[tuple[str, int]]]:
    """The modules of package ``name`` in ``directory``, the walk's edges
    between them, and the statements of the forms grimp reads otherwise.
    """
    placed = {
        dotted
        for dotted in module_map([os.path.dirname(directory)])
        if dotted.partition(".")[0] == name
    }
    files, failed = app_files(name, [directory])
    assert failed == []
    modules: set[str] = set()
    edges: _Edges = set()
    left_out: set[tuple[str, int]] = set()
    for file in files:
        if not file.source:
            continue
        modules.add(file.module)
        module = read(file)
        assert module.tree is not None, module.problem
        for edge in imports(module.tree, file.package, placed.__contains__):
            if edge.target.partition(".")[0] == name and edge.target != file.module:
                edges.add((file.module, edge.target, edge.line))
        for node in ast.walk(module.tree):
            if _other_form(node, file.package, placed, name):
                left_out.add((file.module, node.lineno))  # type: ignore[attr-defined]
    return modules, edges, left_out


def _grimp(name: str) -> tuple[set[str], _Edges]:
    graph = grimp.build_graph(name, cache_dir=None)
    edges: _Edges = set()
    for importer in graph.modules:
        for imported in graph.find_modules_directly_imported_by(importer):
            for detail in graph.get_import_details(
                importer=importer, imported=imported
            ):
                edges.add((importer, imported, detail["line_number"]))
    return set(graph.modules), edges


def test_the_walk_gives_grimp_s_edges_on_django_s_package() -> None:
    directory = os.path.dirname(django.__file__)
    modules, edges, left_out = _walk("django", directory)
    theirs, their_edges = _grimp("django")
    assert modules == theirs
    # Django's own package holds several hundred modules and thousands of
    # edges; on 5.2.19 and 6.1.3 none of grimp's other forms occurs.
    assert len(modules) > 800
    assert len(edges) > 3000

    def kept(found: _Edges) -> _Edges:
        return {edge for edge in found if (edge[0], edge[2]) not in left_out}

    assert kept(edges) == kept(their_edges)


@pytest.fixture
def package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "pkg"
    (root / "a").mkdir(parents=True)
    for file in ("__init__.py", "a/__init__.py", "a/b.py"):
        (root / file).write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))
    return root


def _ours(root: Path, module: str) -> list[str]:
    known = set(module_map([str(root.parent)]))
    package = module.rpartition(".")[0]
    file = File(str(root / f"{module.rpartition('.')[2]}.py"), module, package)
    found = read(file)
    assert found.tree is not None
    return [edge.target for edge in imports(found.tree, package, known.__contains__)]


@pytest.mark.parametrize(
    ("statement", "ours", "theirs"),
    [
        # The rules both follow: a missing name one level down is its
        # package, and a from-import of a name that is not a module is the
        # module it is imported from.
        ("import pkg.a.missing", ["pkg.a"], {"pkg.a"}),
        ("from pkg.a import b, name", ["pkg.a.b", "pkg.a"], {"pkg.a", "pkg.a.b"}),
        # grimp's other forms: no edge, where the check takes the nearest
        # existing package.
        ("from pkg.missing import n", ["pkg"], set()),
        ("from pkg.a.missing import n", ["pkg.a"], set()),
        ("import pkg.a.missing.deeper", ["pkg.a"], set()),
    ],
)
def test_each_form_grimp_reads_otherwise_on_its_own(
    package: Path, statement: str, ours: list[str], theirs: set[str]
) -> None:
    (package / "m.py").write_text(f"{statement}\n")
    assert _ours(package, "pkg.m") == ours
    graph = grimp.build_graph("pkg", cache_dir=None)
    assert graph.find_modules_directly_imported_by("pkg.m") == theirs


def test_a_relative_import_past_the_top_level_package_gives_no_edge(
    package: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    (package / "m.py").write_text("from .... import x\n")
    assert _ours(package, "pkg.m") == []
    with pytest.raises(BaseException) as raised:  # noqa: PT011
        grimp.build_graph("pkg", cache_dir=None)
    assert type(raised.value).__name__ == "PanicException"
    capfd.readouterr()
    assert "grimp" in sys.modules
