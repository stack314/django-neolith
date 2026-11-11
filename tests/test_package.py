"""The package skeleton and the guarantees ADR 0012 asks of the test harness."""

import ast
import importlib.metadata
import importlib.resources
import re
import warnings
from pathlib import Path

import django
import pytest
from django.utils import deprecation
from packaging.version import Version

import neolith


def test_version_is_pep_440() -> None:
    assert str(Version(neolith.__version__)) == neolith.__version__


def test_version_matches_installed_metadata() -> None:
    assert importlib.metadata.version("django-neolith") == neolith.__version__


def test_package_is_typed() -> None:
    assert importlib.resources.files("neolith").joinpath("py.typed").is_file()


# A dotted name in Pydantic with a private (not dunder) part.
_PRIVATE_IN_PYDANTIC = re.compile(r"pydantic(_core)?(\.\w+)*\._(?!_)\w*(\.\w+)*")


def _dotted_names(source: str) -> set[str]:
    """Every dotted name the source imports, reads or spells out as a string.

    A read such as ``pydantic._internal.x`` is resolved through the names the
    module's imports bind.
    """
    tree = ast.parse(source)
    names: set[str] = set()
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
                top = alias.name.split(".")[0]
                bound[alias.asname or top] = alias.name if alias.asname else top
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                names.add(f"{node.module}.{alias.name}")
                bound[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.add(node.value)
    for node in ast.walk(tree):
        parts: list[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if parts and isinstance(node, ast.Name) and node.id in bound:
            names.add(".".join([bound[node.id], *reversed(parts)]))
    return names


def test_nothing_private_in_pydantic_is_used() -> None:
    # Pydantic's private modules may change in any minor release, and the
    # dependency allows any 2.x from 2.12, so using one could break neolith.
    names = set().union(
        *(
            _dotted_names(path.read_text())
            for path in Path(neolith.__file__).parent.rglob("*.py")
        )
    )
    assert "pydantic.BaseModel" in names
    assert not [name for name in names if _PRIVATE_IN_PYDANTIC.fullmatch(name)]


@pytest.mark.parametrize(
    "source",
    [
        "from pydantic._internal._model_construction import init_private_attributes",
        "from pydantic._internal import _model_construction",
        "from pydantic import _internal",
        "import pydantic._internal._model_construction as m",
        "import pydantic\npydantic._internal._model_construction.init",
        "import pydantic as p\np._internal",
        "from pydantic import main\nmain._object_setattr",
        "import importlib\nimportlib.import_module('pydantic._internal._utils')",
        "from pydantic_core import _pydantic_core",
    ],
)
def test_private_use_of_pydantic_is_found(source: str) -> None:
    assert any(_PRIVATE_IN_PYDANTIC.fullmatch(name) for name in _dotted_names(source))


@pytest.mark.parametrize(
    "source",
    [
        "from pydantic import BaseModel\nBaseModel.__subclasses__",
        "import pydantic\npydantic.__version__",
        "from pydantic_core import core_schema",
        "class _Private: pass\n_Private.model_post_init",
        "'see pydantic/_internal/_model_construction.py'",
    ],
)
def test_public_use_of_pydantic_is_not_found(source: str) -> None:
    assert not any(
        _PRIVATE_IN_PYDANTIC.fullmatch(name) for name in _dotted_names(source)
    )


def test_running_on_a_supported_django() -> None:
    assert django.VERSION[:2] >= (5, 2)


@pytest.mark.parametrize("category", [DeprecationWarning, PendingDeprecationWarning])
def test_deprecation_warnings_are_errors(category: type[Warning]) -> None:
    with pytest.raises(category):
        warnings.warn("deprecated", category, stacklevel=1)


def _django_removal_warnings() -> list[type[Warning]]:
    return [
        value
        for name, value in vars(deprecation).items()
        if name.startswith("RemovedInDjango")
        and isinstance(value, type)
        and issubclass(value, Warning)
    ]


def test_django_has_removal_warnings() -> None:
    assert _django_removal_warnings()


@pytest.mark.parametrize("category", _django_removal_warnings())
def test_django_removal_warnings_are_errors(category: type[Warning]) -> None:
    with pytest.raises(category):
        warnings.warn("removed soon", category, stacklevel=1)
