"""The package skeleton and the guarantees ADR 0012 asks of the test harness."""

import ast
import importlib.metadata
import importlib.resources
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


def test_nothing_private_is_imported_from_pydantic() -> None:
    # Pydantic's private modules may change in any minor release, and the
    # dependency allows every 2.x, so importing one could break import neolith.
    imported: set[str] = set()
    for path in Path(neolith.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.update(f"{node.module}.{alias.name}" for alias in node.names)
    assert "pydantic.BaseModel" in imported
    assert not [
        name
        for name in imported
        if name.split(".")[0] in ("pydantic", "pydantic_core")
        and any(part.startswith("_") for part in name.split(".")[1:])
    ]


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
