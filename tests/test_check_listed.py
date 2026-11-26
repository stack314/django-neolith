"""The listed names the check matches by resolved name, pinned by a walk of
Django and neolith (ADR 0030 Decision 3).

Each listed object stands for the module that defines it and every module
of the installed Django and of neolith whose ``__all__`` lists the same
name, bound to the same object. The walk imports every module of both, so
it runs in a child process here, on each Django version CI installs, and
the check carries the set as data (``neolith._check_source.LISTED``).
"""

import inspect
import json
import os
import subprocess
import sys

import django
import django.db.transaction

from neolith._check_source import _TRANSACTION, LISTED

_WALK = """\
import importlib, json, pkgutil, sys, warnings
warnings.simplefilter("ignore")
from django.conf import settings
settings.configure(
    INSTALLED_APPS=[
        "django.contrib.admin", "django.contrib.admindocs", "django.contrib.auth",
        "django.contrib.contenttypes", "django.contrib.flatpages",
        "django.contrib.humanize", "django.contrib.messages",
        "django.contrib.redirects", "django.contrib.sessions",
        "django.contrib.sitemaps", "django.contrib.sites",
        "django.contrib.staticfiles", "django.contrib.syndication",
    ],
)
import django
django.setup()
import neolith
modules = {}
for package in (django, neolith):
    names = [package.__name__] + [
        info.name
        for info in pkgutil.walk_packages(package.__path__, package.__name__ + ".",
                                          onerror=lambda name: None)
    ]
    for name in names:
        try:
            modules[name] = importlib.import_module(name)
        except BaseException:
            continue
listed = json.loads(sys.argv[1])
found = {}
for canonical in listed:
    home, _, attribute = canonical.rpartition(".")
    module = importlib.import_module(home)
    if not hasattr(module, attribute):
        continue
    target = getattr(module, attribute)
    found[canonical] = sorted(
        f"{name}.{attribute}"
        for name, module in modules.items()
        if name != home
        and attribute in (getattr(module, "__all__", None) or ())
        and getattr(module, attribute, None) is target
    )
print(json.dumps([len(modules), found]))
"""


def test_the_listed_set_is_what_a_walk_of_django_and_neolith_finds() -> None:
    ran = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _WALK, json.dumps(sorted(LISTED))],
        capture_output=True,
        text=True,
        check=True,
        timeout=300,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )
    walked, found = json.loads(ran.stdout)
    # Django's own package alone holds several hundred modules.
    assert walked > 500
    expected = {
        canonical: sorted(others)
        for canonical, others in LISTED.items()
        if canonical in found or canonical != "django.db.transaction.savepoint_create"
    }
    assert found == expected


def test_the_functions_of_transaction_are_those_django_defines() -> None:
    module = django.db.transaction
    defined = {
        name
        for name, value in vars(module).items()
        if inspect.isfunction(value)
        and value.__module__ == module.__name__
        and not name.startswith("_")
    }
    # Django 6.1 adds savepoint_create; the data holds every supported
    # version's functions.
    extra = set() if django.VERSION >= (6, 1) else {"savepoint_create"}
    assert set(_TRANSACTION) == defined | extra
    assert not defined & extra


def test_the_listed_names_are_those_adr_0030_gives() -> None:
    # ADR 0030 Decision 3: neolith.connect, command_scope and testing.boot,
    # get_user_model, import_module, import_string, include, connection,
    # connections, DEFAULT_DB_ALIAS and the functions of transaction, each
    # by the module that defines it.
    assert set(LISTED) == {
        "neolith._host.connect",
        "neolith._host.command_scope",
        "neolith.testing.boot",
        "django.contrib.auth.get_user_model",
        "importlib.import_module",
        "django.utils.module_loading.import_string",
        "django.urls.conf.include",
        "django.db.connection",
        "django.db.connections",
        "django.db.utils.DEFAULT_DB_ALIAS",
        *(f"django.db.transaction.{name}" for name in _TRANSACTION),
    }
