"""The package skeleton and the guarantees ADR 0012 asks of the test harness."""

import importlib.metadata
import importlib.resources
import warnings

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
