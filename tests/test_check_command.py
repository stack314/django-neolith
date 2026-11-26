"""The check before deployment: its two entries, its inputs, its modes and
its statuses (spec 05, APP-70 and APP-71; ADR 0030 Decisions 6 to 8).

Each run is a child process in a project written under ``tmp_path``
(``tests/checking.py``): ``neolith check <app> ... --settings deployer``
builds a closed host from the deployer's module, and ``manage.py checkapps``
(``python -m django checkapps``) checks the host a settings module builds.
The shape of ``[tool.neolith]`` is also read in this process, by
``read_config``, from a working directory under ``tmp_path``.
"""

import contextlib
import logging
import os
import textwrap
import types
from pathlib import Path

import pytest

from neolith import UsageError, _check
from tests.checking import Project, Ran

_GUARD = (
    "check: database {}: a connection was opened while the host was set up or "
    "checked, and the check runs without a database"
)
_CLEAN = "neolith check: 0 errors, 0 errors in report-only apps, 0 warnings"
_ITEM = """\
from django.db import models


class Item(models.Model):
    name = models.CharField(max_length=10)
"""
_NOTE = """\
from django.db import models
Item = "shop.Item"


class Note(models.Model):
    item = models.ForeignKey(Item, on_delete=models.CASCADE)
"""
_FK = (
    "desk/models.py:5: neolith.E106 app desk: model desk.Note: field item is a "
    "foreign key to shop.Item, a model of app shop; keep the other record's "
    "identifier in a plain field, and reach that app through a contract it provides"
)
_STALE = (
    "neolith.E113 app shop: is listed in [tool.neolith] boundary-report-only and "
    "has no error finding; remove it from the list, so that the app's next finding "
    "fails the check"
)
_ENTRIES = ["neolith check", "manage.py checkapps"]


class Two(Project):
    """A project with the hosted apps ``shop`` and ``desk``, where desk's
    model has a foreign key to shop's, beside a deployer's module and a
    settings module for one host of both.
    """

    def __init__(self, root: Path, *, note: str = _NOTE, urls: str = "") -> None:
        super().__init__(root)
        self.app("shop", models=_ITEM)
        self.app("desk", models=note)
        if urls:
            self.write("urls.py", urls)
        self.deployer("shop", "desk")
        self.settings(
            ["shop", "desk"], neolith={"DATABASES": self.databases("shop", "desk")}
        )

    def entry(
        self,
        entry: str,
        *options: str,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> Ran:
        if entry == "neolith check":
            return self.check("shop", "desk", *options, cwd=cwd, env=env)
        return self.checkapps(*options, cwd=cwd, env=env)

    def no_database_file(self) -> bool:
        return not any(self.root.glob("*.sqlite3"))


@pytest.fixture
def two(tmp_path: Path) -> Two:
    return Two(tmp_path)


# Phase 1: [tool.neolith] in pyproject.toml.


@pytest.mark.parametrize(
    ("text", "labels"),
    [
        (None, []),
        ("", []),
        ("[project]\nname = 'x'\n", []),
        ("tool = 1\n", []),
        ("[tool.other]\nkey = 1\n", []),
        ("[tool.neolith]\n", []),
        ("[tool.neolith]\nboundary-report-only = ['desk']\n", ["desk"]),
        ("[tool.neolith]\nboundary-report-only = ['desk', 'shop']\n", ["desk", "shop"]),
    ],
    ids=[
        "no file",
        "empty",
        "no tool table",
        "tool not a table",
        "no neolith table",
        "empty neolith table",
        "one label",
        "two labels",
    ],
)
def test_pyproject_admits_its_one_key_or_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str | None, labels: list[str]
) -> None:
    if text is not None:
        (tmp_path / "pyproject.toml").write_text(text, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert _check.read_config() == (labels, [])


_LISTED = "[tool.neolith] boundary-report-only"


@pytest.mark.parametrize(
    ("data", "problems"),
    [
        (b"[tool.neolith\n", ["pyproject.toml: cannot be read: TOMLDecodeError: "]),
        (b"a = '\xff'\n", ["pyproject.toml: cannot be read: UnicodeDecodeError: "]),
        (b"[tool]\nneolith = 1\n", ["[tool.neolith]: expected a table, got int"]),
        (b"[[tool.neolith]]\n", ["[tool.neolith]: expected a table, got list"]),
        (
            b"[tool.neolith]\nreport-only = ['desk']\n",
            ["[tool.neolith]: report-only is not a key the check reads"],
        ),
        (
            b'[tool.neolith]\n"a\\nb" = 1\n',
            ["[tool.neolith]: a\\nb is not a key the check reads"],
        ),
        (
            b"[tool.neolith]\nboundary-report-only = 'desk'\n",
            [f"{_LISTED}: expected a list of str"],
        ),
        (
            b"[tool.neolith]\nboundary-report-only = ['desk', 1]\n",
            [f"{_LISTED}: expected a list of str"],
        ),
        (
            b"[tool.neolith]\nboundary-report-only = []\n",
            [f"{_LISTED}: is empty; leave the key out"],
        ),
        (
            b"[tool.neolith]\nboundary-report-only = ['d', 's', 'd', 'd']\n",
            [f"{_LISTED}: d listed twice"],
        ),
        (
            # A basic string, so TOML reads the escape as the character.
            b'[tool.neolith]\nboundary-report-only = ["x\\u2028y", "x\\u2028y"]\n',
            [f"{_LISTED}: x\\u2028y listed twice"],
        ),
        (
            b"[tool.neolith]\nmode = 1\nboundary-report-only = ['a', 'a', 'b', 'b']\n",
            [
                "[tool.neolith]: mode is not a key the check reads",
                f"{_LISTED}: a listed twice",
                f"{_LISTED}: b listed twice",
            ],
        ),
        (
            b"[tool.neolith]\nmode = 1\nboundary-report-only = 3\n",
            [
                "[tool.neolith]: mode is not a key the check reads",
                f"{_LISTED}: expected a list of str",
            ],
        ),
    ],
    ids=[
        "not TOML",
        "not UTF-8",
        "not a table",
        "array of tables",
        "unknown key",
        "key with a line break",
        "not a list",
        "not a str",
        "empty",
        "listed twice",
        "line separator escaped",
        "every mistake in one run",
        "unknown key and wrong type",
    ],
)
def test_pyproject_refuses_every_other_shape_with_every_mistake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: bytes, problems: list[str]
) -> None:
    (tmp_path / "pyproject.toml").write_bytes(data)
    monkeypatch.chdir(tmp_path)
    _, found = _check.read_config()
    assert len(found) == len(problems), found
    for line, start in zip(found, problems, strict=True):
        assert line.startswith(start), found
        assert "\n" not in line


def test_only_the_nearest_pyproject_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.neolith]\nboundary-report-only = ['desk']\n", encoding="utf-8"
    )
    below = tmp_path / "a" / "b"
    below.mkdir(parents=True)
    monkeypatch.chdir(below)
    assert _check.read_config() == (["desk"], [])
    (tmp_path / "a" / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    assert _check.read_config() == ([], [])


@pytest.mark.parametrize("kind", ["directory", "pipe", "broken link"])
def test_a_pyproject_that_is_not_a_regular_file_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    path = tmp_path / "pyproject.toml"
    if kind == "directory":
        path.mkdir()
    elif kind == "pipe":
        if not hasattr(os, "mkfifo"):
            pytest.skip("this platform has no named pipes")
        os.mkfifo(path)
    else:
        path.symlink_to(tmp_path / "missing.toml")
    monkeypatch.chdir(tmp_path)
    labels, found = _check.read_config()
    assert labels == []
    if kind == "broken link":
        assert len(found) == 1
        assert found[0].startswith(
            "pyproject.toml: cannot be read: FileNotFoundError: "
        )
    else:
        assert found == [
            f"pyproject.toml: cannot be read: OSError: {path} is not a regular file"
        ]


def test_a_pyproject_mistake_ends_neolith_check_before_the_host_is_built(
    tmp_path: Path,
) -> None:
    project = Project(tmp_path)
    project.pyproject("[tool.neolith]\nmode = 1\nboundary-report-only = []\n")
    # No such settings module: building the host would refuse it.
    ran = project.check("shop", settings="missing")
    assert ran.status == 2
    assert ran.errors == [
        "neolith: [tool.neolith]: mode is not a key the check reads",
        f"neolith: {_LISTED}: is empty; leave the key out",
    ]
    assert ran.stdout == ""


def test_a_pyproject_mistake_ends_checkapps_before_any_finding(two: Two) -> None:
    two.pyproject("[tool.neolith]\nboundary-report-only = 'desk'\n")
    ran = two.checkapps()
    assert ran.status == 2
    assert ran.errors == [f"neolith: {_LISTED}: expected a list of str"]
    assert ran.stdout == ""


@pytest.mark.parametrize("entry", _ENTRIES)
def test_both_entries_judge_the_labels_only_once_the_shape_is_admitted(
    two: Two, entry: str
) -> None:
    two.pyproject("[tool.neolith]\nboundary-report-only = ['ghost', 'desk', 'desk']\n")
    ran = two.entry(entry)
    assert ran.status == 2
    assert ran.errors == [f"neolith: {_LISTED}: desk listed twice"]
    assert ran.stdout == ""


@pytest.mark.parametrize("entry", _ENTRIES)
def test_a_listed_label_that_is_not_a_hosted_app_is_refused_after_set_up(
    two: Two, entry: str
) -> None:
    two.pyproject("[tool.neolith]\nboundary-report-only = ['ghost', 'desk', 'auth']\n")
    ran = two.entry(entry)
    assert ran.status == 2
    assert ran.errors == [
        f"neolith: {_LISTED}: ghost is not a hosted app",
        f"neolith: {_LISTED}: auth is not a hosted app",
    ]
    assert ran.stdout == ""


@pytest.mark.parametrize(
    ("text", "printed"),
    [
        ("a\nb", "a\\nb"),
        ("a\x1bb", "a\\x1bb"),
        ("a\u200bb", "a\\u200bb"),
        ("a\ud800b", "a\\ud800b"),
        ("a\u2028b", "a\\u2028b"),
        ("a\u2029b", "a\\u2029b"),
        ("é ü ok", "é ü ok"),
    ],
    ids=["Cc line feed", "Cc escape", "Cf", "Cs", "Zl", "Zp", "printable"],
)
def test_shown_escapes_what_would_break_a_line_and_nothing_else(
    text: str, printed: str
) -> None:
    assert _check.shown(text) == printed
    assert _check.shown(text * 200, 10).endswith("...")


@pytest.mark.parametrize("entry", _ENTRIES)
def test_a_label_is_printed_on_one_line_whatever_it_holds(two: Two, entry: str) -> None:
    # A line separator (Zl), a zero-width space (Cf), an escape (Cc) and a
    # paragraph separator (Zp), each as TOML reads a basic string's escape.
    two.pyproject(
        "[tool.neolith]\nboundary-report-only = "
        '["a\\u2028b", "c\\u200bd", "e\\u001bf", "g\\u2029h"]\n'
    )
    ran = two.entry(entry)
    assert ran.status == 2
    assert ran.errors == [
        f"neolith: {_LISTED}: {shown} is not a hosted app"
        for shown in ["a\\u2028b", "c\\u200bd", "e\\x1bf", "g\\u2029h"]
    ]


@pytest.mark.parametrize(
    ("name", "printed"),
    [
        (b"b\xff", "b\\udcff"),
        (b"b\nc", "b\\nc"),
        ("b\u2028c".encode(), "b\\u2028c"),
        ("b\u200bc".encode(), "b\\u200bc"),
    ],
    ids=["byte 0xff", "line feed", "line separator", "zero-width space"],
)
@pytest.mark.parametrize("entry", _ENTRIES)
def test_a_path_is_printed_on_one_line_whatever_it_holds(
    tmp_path: Path, entry: str, name: bytes, printed: str
) -> None:
    # The byte 0xff in a directory's name reaches Python as a lone surrogate,
    # which no UTF-8 stream can write; the others would break the line.
    root = Path(os.fsdecode(bytes(tmp_path) + b"/" + name))
    root.mkdir()
    project = Two(root)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    ran = project.entry(entry, cwd=elsewhere)
    path = f"{tmp_path}/{printed}/desk/models.py"
    assert ran.findings == [_FK.replace("desk/models.py", path)], ran.stderr
    assert ran.status == 1


@pytest.mark.parametrize("entry", _ENTRIES)
def test_a_hosted_app_without_a_database_needs_no_migrations(
    tmp_path: Path, entry: str
) -> None:
    project = Two(tmp_path)
    project.app("plain", migrations=False)
    project.write(
        "plain/apps.py",
        """\
        from neolith import NeolithAppConfig


        class Config(NeolithAppConfig):
            name = "plain"
            label = "plain"
        """,
    )
    project.deployer("shop", "desk")
    neolith = {"DATABASES": project.databases("shop", "desk")}
    project.settings(["shop", "desk", "plain"], neolith=neolith)
    ran = (
        project.check("shop", "desk", "plain")
        if entry == "neolith check"
        else project.checkapps()
    )
    assert ran.errors == []
    assert ran.lines == [
        _FK,
        "neolith check: 1 errors, 0 errors in report-only apps, 0 warnings",
    ]


_LOADING_ADMIN = """\
from django.contrib import admin
from shop.models import Item


class LoadingAdmin(admin.ModelAdmin):
    pass


site = admin.AdminSite(name="loading")
site.register(Item, LoadingAdmin)
"""


@pytest.mark.parametrize("entry", _ENTRIES)
def test_an_admin_set_up_warned_about_is_not_warned_again(
    tmp_path: Path, entry: str
) -> None:
    # Registered while the apps load, so set-up warns; phase 4 does not.
    project = Two(tmp_path)
    project.write("loading_admin.py", _LOADING_ADMIN)
    project.app(
        "desk",
        models=_NOTE,
        config="""\
        def ready(self):
            import loading_admin  # noqa: F401
        """,
    )
    ran = project.entry(entry)
    found = [line for line in ran.errors if "(neolith.W001)" in line]
    assert len(found) == 1, ran.stderr
    assert found[0].endswith(
        "(neolith.W001) loading_admin.LoadingAdmin: admin class in no installed "
        "app; its views have no calling app, and a contract call from them is "
        "refused"
    )


# The arguments.


@pytest.mark.parametrize(
    "arguments",
    [
        ["check", "--settings", "deployer"],
        ["check", "shop"],
        ["check", "shop", "--settings", "deployer", "--settings", "deployer"],
        ["check", "shop", "--set", "deployer"],
        ["check", "shop", "--settings", "deployer", "--report"],
    ],
    ids=[
        "no app",
        "no settings",
        "settings twice",
        "abbreviated option",
        "unknown option",
    ],
)
def test_a_mistake_in_neolith_check_s_arguments_exits_with_usage_and_status_2(
    two: Two, arguments: list[str]
) -> None:
    ran = two.run("-m", "neolith", *arguments)
    assert ran.status == 2
    assert ran.stderr.startswith("usage: neolith")
    assert "--settings" in " ".join(arguments) or "settings" in ran.stderr


@pytest.mark.parametrize("option", ["--report", "shop", "--fail-level=ERROR"])
def test_checkapps_takes_no_argument_of_its_own(two: Two, option: str) -> None:
    ran = two.checkapps(option)
    assert ran.status == 2
    assert "usage:" in ran.stderr
    assert "checkapps" in ran.stderr


# The output and the statuses.


@pytest.mark.parametrize("entry", _ENTRIES)
def test_an_error_finding_of_an_enforced_app_exits_1(two: Two, entry: str) -> None:
    ran = two.entry(entry)
    assert ran.lines == [
        _FK,
        "neolith check: 1 errors, 0 errors in report-only apps, 0 warnings",
    ]
    assert ran.errors == []
    assert ran.status == 1


@pytest.mark.parametrize("entry", _ENTRIES)
def test_a_host_with_no_finding_exits_0(tmp_path: Path, entry: str) -> None:
    project = Two(tmp_path, note=_ITEM.replace("Item", "Note"))
    ran = project.entry(entry)
    assert ran.lines == [_CLEAN]
    assert ran.errors == []
    assert ran.status == 0


@pytest.mark.parametrize("entry", _ENTRIES)
def test_a_line_the_output_cannot_encode_is_printed_escaped(
    tmp_path: Path, entry: str
) -> None:
    project = Two(tmp_path, note=_NOTE.replace("class Note(", "class Noté("))
    ran = project.entry(entry, env={"PYTHONIOENCODING": "ascii"})
    assert ran.lines == [
        _FK.replace("desk.Note", "desk.Not\\xe9"),
        "neolith check: 1 errors, 0 errors in report-only apps, 0 warnings",
    ]
    assert ran.status == 1


@pytest.mark.parametrize("entry", _ENTRIES)
def test_a_finding_outside_the_working_directory_prints_an_absolute_path(
    two: Two, entry: str
) -> None:
    elsewhere = two.root / "elsewhere"
    elsewhere.mkdir()
    ran = two.entry(entry, cwd=elsewhere)
    path = str(two.root / "desk" / "models.py")
    assert ran.findings == [_FK.replace("desk/models.py", path)]


@pytest.mark.spec("APP-71")
@pytest.mark.parametrize("entry", _ENTRIES)
def test_a_report_only_app_s_errors_are_marked_and_end_with_status_0(
    two: Two, entry: str
) -> None:
    two.pyproject("[tool.neolith]\nboundary-report-only = ['desk']\n")
    ran = two.entry(entry)
    assert ran.lines == [
        f"{_FK} (report only)",
        "neolith check: 0 errors, 1 errors in report-only apps, 0 warnings",
    ]
    assert ran.status == 0


@pytest.mark.spec("APP-71")
@pytest.mark.parametrize("entry", _ENTRIES)
def test_a_listed_app_with_no_error_finding_is_a_stale_entry(
    two: Two, entry: str
) -> None:
    two.pyproject("[tool.neolith]\nboundary-report-only = ['shop', 'desk']\n")
    ran = two.entry(entry)
    assert ran.lines == [
        f"{_FK} (report only)",
        _STALE,
        "neolith check: 1 errors, 1 errors in report-only apps, 0 warnings",
    ]
    assert ran.status == 1


@pytest.mark.parametrize("entry", _ENTRIES)
def test_boot_s_checks_that_read_no_database_are_refusals(
    tmp_path: Path, entry: str
) -> None:
    project = Two(tmp_path)
    # A hosted app with a database and no migrations (neolith.E008).
    project.app("bare", models="", migrations=False)
    project.deployer("shop", "desk", "bare")
    project.settings(
        ["shop", "desk", "bare"],
        neolith={"DATABASES": project.databases("shop", "desk", "bare")},
    )
    ran = (
        project.check("shop", "desk", "bare")
        if entry == "neolith check"
        else project.checkapps()
    )
    assert ran.errors == [
        "neolith: host: Boot would refuse the host:",
        "neolith: (neolith.E008) app bare: has a database and no migrations",
    ]
    assert ran.lines == [
        _FK,
        "neolith check: 1 errors, 0 errors in report-only apps, 0 warnings",
    ]
    assert ran.status == 1


_CONTRACT = """\
from typing import Protocol

from pydantic import BaseModel

from neolith import action, contract


class Count(BaseModel):
    n: int


@contract("shop.v1")
class Shop(Protocol):
    @action(idempotent=True)
    def count(self, request: Count) -> Count: ...
"""

# The implementation in a module of its own, outside the contract package.
_SERVICE = """\
from neolith import CallContext
from shop.contracts import Count


class Service:
    def count(self, request: Count, ctx: CallContext) -> Count:
        return request
"""

_SHADOWING_URLS = """\
from django.http import HttpResponse
from django.urls import path


def anything(request):
    return HttpResponse()


urlpatterns = [path("shop/v1/", anything)]
"""


def test_both_entries_print_the_same_lines_for_phases_4_and_5(tmp_path: Path) -> None:
    project = Two(tmp_path, urls=_SHADOWING_URLS)
    project.write("shop/contracts.py", _CONTRACT)
    project.write("shop/services.py", _SERVICE)
    project.app(
        "shop",
        models=_ITEM,
        imports="from neolith import Provided\nfrom shop.contracts import Shop\n",
        config="""\
        provides = (
            Provided(Shop, implementation="shop.services.Service", principal_from=()),
        )
        """,
    )
    project.app("bare", models="", migrations=False)
    neolith = {"DATABASES": project.databases("shop", "desk", "bare")}
    silenced = 'SILENCED_SYSTEM_CHECKS = ["neolith.W004"]\n'
    project.deployer("shop", "desk", "bare", below=silenced)
    project.settings(["shop", "desk", "bare"], neolith=neolith, below=silenced)
    project.pyproject("[tool.neolith]\nboundary-report-only = ['desk']\n")
    script = project.check("shop", "desk", "bare")
    command = project.checkapps()
    assert script.stdout == command.stdout
    assert script.errors == command.errors
    assert script.status == command.status == 1
    assert script.errors == [
        "neolith: warning: (neolith.W003) shop.v1: the host answers /shop/v1/ itself, "
        "so the URL pattern 'shop/v1/' of ROOT_URLCONF, which matches it, is never "
        "reached for that path",
        "neolith: host: Boot would refuse the host:",
        "neolith: (neolith.E008) app bare: has a database and no migrations",
    ]
    assert script.lines == [
        f"{_FK} (report only)",
        "urls.py:5: neolith.W102 URL pattern 'shop/v1/': view urls.anything is the "
        "project's code in no installed app; a request it serves has no calling "
        "app, and a contract call from it is refused; move the view into the app "
        "whose work it does, so that its requests run in that app's root",
        "neolith check: 0 errors, 1 errors in report-only apps, 1 warnings",
    ]


_ADMIN_URLS = """\
from django.contrib import admin
from shop.models import Item


class ProjectAdmin(admin.ModelAdmin):
    pass


site = admin.AdminSite(name="project")
site.register(Item, ProjectAdmin)
urlpatterns = []
"""


@pytest.mark.parametrize("silenced", [False, True])
@pytest.mark.parametrize("entry", _ENTRIES)
def test_an_admin_the_urlconf_registers_is_warned_once_unless_silenced(
    tmp_path: Path, entry: str, silenced: bool
) -> None:
    # Set-up ran before the URLconf was imported, so only phase 4 sees it.
    project = Two(tmp_path, urls=_ADMIN_URLS)
    if silenced:
        below = 'SILENCED_SYSTEM_CHECKS = ["neolith.W001"]\n'
        neolith = {"DATABASES": project.databases("shop", "desk")}
        project.deployer("shop", "desk", below=below)
        project.settings(["shop", "desk"], neolith=neolith, below=below)
    ran = project.entry(entry)
    warned = (
        "neolith: warning: (neolith.W001) urls.ProjectAdmin: admin class in no "
        "installed app; its views have no calling app, and a contract call from "
        "them is refused"
    )
    assert ran.errors == ([] if silenced else [warned])
    assert ran.lines == [
        _FK,
        "neolith check: 1 errors, 0 errors in report-only apps, 0 warnings",
    ]
    assert ran.status == 1


@pytest.mark.parametrize("entry", _ENTRIES)
def test_a_root_urlconf_that_does_not_import_is_a_refusal_beside_the_findings(
    tmp_path: Path, entry: str
) -> None:
    project = Two(tmp_path, urls="raise ValueError('no routes today')\n")
    ran = project.entry(entry)
    assert ran.errors == [
        "neolith: host: Boot would refuse the host:",
        "neolith: settings: ROOT_URLCONF urls cannot be imported: ValueError: no "
        "routes today",
    ]
    assert ran.lines == [
        _FK,
        "neolith check: 1 errors, 0 errors in report-only apps, 0 warnings",
    ]
    assert ran.status == 1


_LOGGING = """\
LOGGING = {
    "version": 1,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "WARNING"},
}
"""


@pytest.mark.parametrize("entry", _ENTRIES)
def test_the_project_s_logging_neither_hides_nor_repeats_boot_s_lines(
    tmp_path: Path, entry: str
) -> None:
    # dictConfig disables the loggers that exist, neolith.host among them,
    # and the root handler would print each logged line a second time.
    project = Two(tmp_path, urls="raise ValueError('no routes today')\n")
    project.deployer("shop", "desk", below=_LOGGING)
    neolith = {"DATABASES": project.databases("shop", "desk")}
    project.settings(["shop", "desk"], neolith=neolith, below=_LOGGING)
    ran = project.entry(entry)
    assert ran.errors == [
        "neolith: host: Boot would refuse the host:",
        "neolith: settings: ROOT_URLCONF urls cannot be imported: ValueError: no "
        "routes today",
    ]
    assert ran.status == 1


def test_a_refusal_while_the_host_is_built_ends_neolith_check_with_status_1(
    two: Two,
) -> None:
    two.deployer(neolith={"DATABASES": two.databases("shop")})
    ran = two.check("shop", "desk")
    assert ran.status == 1
    assert ran.stdout == ""
    assert ran.errors
    assert all(line.startswith("neolith: ") for line in ran.errors)
    assert any("desk" in line for line in ran.errors)


# An error the check did not foresee, and a signal to stop.

_BROKEN = """\
import sys

import neolith._check_models

def broken(hosted, source):
    raise {error}

neolith._check_models.findings = broken
{run}
"""
_SCRIPT = "from neolith._cli import main\nsys.exit(main(sys.argv[1:]))\n"
_MANAGE = (
    "from django.core.management import execute_from_command_line\n"
    "execute_from_command_line(['manage.py', *sys.argv[1:]])\n"
)


@pytest.mark.parametrize("entry", _ENTRIES)
def test_an_error_the_check_did_not_foresee_exits_2(two: Two, entry: str) -> None:
    if entry == "neolith check":
        code = _BROKEN.format(error="LookupError('no such thing')", run=_SCRIPT)
        ran = two.run("-c", code, "check", "shop", "desk", "--settings", "deployer")
    else:
        code = _BROKEN.format(error="LookupError('no such thing')", run=_MANAGE)
        ran = two.run("-c", code, "checkapps", "--settings=settings")
    assert ran.status == 2
    assert ran.errors == ["neolith: check: internal error: LookupError: no such thing"]
    assert ran.stdout == ""


def test_checkapps_with_traceback_also_prints_the_traceback(two: Two) -> None:
    code = _BROKEN.format(error="LookupError('no such thing')", run=_MANAGE)
    ran = two.run("-c", code, "checkapps", "--settings=settings", "--traceback")
    assert ran.status == 2
    assert ran.errors[0] == "neolith: check: internal error: LookupError: no such thing"
    assert ran.errors[1] == "Traceback (most recent call last):"
    assert ran.errors[-1] == "LookupError: no such thing"


# In one process: the guard and the host's log handler are put back however
# the command ends, a second run prints the same, and nothing boots.
_IN_PROCESS = """\
import io
import json
import logging
import os
import sys

os.environ["DJANGO_SETTINGS_MODULE"] = "settings"
import django

django.setup()

from django.core.management import call_command
from django.db.backends.base.base import BaseDatabaseWrapper

import neolith._check_models
from neolith import _boot

original = BaseDatabaseWrapper.connect
handlers = list(logging.getLogger("neolith.host").handlers)
seen = []


def run():
    out = io.StringIO()
    try:
        call_command("checkapps", stdout=out)
        status = 0
    except SystemExit as ended:
        status = ended.code
    seen.append(
        [
            status,
            out.getvalue(),
            BaseDatabaseWrapper.connect is original,
            logging.getLogger("neolith.host").handlers == handlers,
        ]
    )


run()
run()
findings = neolith._check_models.findings


def stopped(hosted, source):
    assert BaseDatabaseWrapper.connect is not original
    raise KeyboardInterrupt


neolith._check_models.findings = stopped
try:
    run()
except KeyboardInterrupt:
    seen.append(
        [
            "stopped",
            BaseDatabaseWrapper.connect is original,
            logging.getLogger("neolith.host").handlers == handlers,
        ]
    )
print(json.dumps([seen, _boot._kept is None]))
"""


@pytest.mark.spec("APP-70")
def test_the_guard_is_put_back_however_the_command_ends(two: Two) -> None:
    ran = two.run("-c", _IN_PROCESS)
    assert ran.status == 0, ran.stderr
    seen, unbooted = __import__("json").loads(ran.lines[-1])
    first, second, stopped = seen
    assert first == second
    assert first == [
        1,
        f"{_FK}\nneolith check: 1 errors, 0 errors in report-only apps, 0 warnings\n",
        True,
        True,
    ]
    assert stopped == ["stopped", True, True]
    assert unbooted


# The guard: the check opens no database connection.


@pytest.mark.spec("APP-70")
@pytest.mark.parametrize("order", ["first in, first out", "last in, first out"])
def test_runs_that_overlap_share_the_guard_and_put_it_back_once(order: str) -> None:
    # Two runs of the command on two threads, in either order of ending.
    from django.db.backends.base.base import BaseDatabaseWrapper

    original = vars(BaseDatabaseWrapper)["connect"]
    handlers = list(logging.getLogger("neolith.host").handlers)
    with contextlib.ExitStack() as first, contextlib.ExitStack() as second:
        first.enter_context(_check._host_log())
        first.enter_context(_check.guarded())
        second.enter_context(_check._host_log())
        second.enter_context(_check.guarded())
        held = vars(BaseDatabaseWrapper)["connect"]
        with pytest.raises(UsageError, match="check: database x: a connection"):
            held(types.SimpleNamespace(alias="x"))
        (first if order == "first in, first out" else second).close()
        assert vars(BaseDatabaseWrapper)["connect"] is held
        assert len(logging.getLogger("neolith.host").handlers) == len(handlers) + 1
    assert vars(BaseDatabaseWrapper)["connect"] is original
    assert logging.getLogger("neolith.host").handlers == handlers


@pytest.mark.spec("APP-70")
@pytest.mark.parametrize("entry", _ENTRIES)
def test_a_query_while_the_urlconf_is_imported_is_refused_under_both_entries(
    tmp_path: Path, entry: str
) -> None:
    urls = """\
    from django.db import connections

    connections["desk"].cursor()
    urlpatterns = []
    """
    project = Two(tmp_path, urls=textwrap.dedent(urls))
    ran = project.entry(entry)
    assert ran.errors == [
        "neolith: host: Boot would refuse the host:",
        "neolith: settings: ROOT_URLCONF urls cannot be imported: UsageError: "
        + _GUARD.format("desk"),
    ]
    assert ran.status == 1
    assert project.no_database_file()


@pytest.mark.spec("APP-70")
def test_a_query_while_the_apps_load_is_refused_under_neolith_check(
    tmp_path: Path,
) -> None:
    note = _NOTE + 'from django.db import connections\nconnections["desk"].cursor()\n'
    project = Two(tmp_path, note=note)
    ran = project.check("shop", "desk")
    assert ran.status == 1
    assert ran.errors == [
        "neolith: host: the apps cannot be loaded: UsageError: " + _GUARD.format("desk")
    ]
    assert ran.stdout == ""
    assert project.no_database_file()


@pytest.mark.spec("APP-70")
@pytest.mark.parametrize("entry", _ENTRIES)
def test_a_json_field_on_a_missing_sqlite_path_creates_no_file(
    tmp_path: Path, entry: str
) -> None:
    # On Django 6.1 a command that runs Django's checks asks every alias's
    # features, and SQLite's JSON support opens a connection (ADR 0030 fact 4).
    note = _ITEM.replace("Item", "Note").replace(
        "name = models.CharField(max_length=10)", "data = models.JSONField()"
    )
    project = Two(tmp_path, note=note)
    ran = project.entry(entry)
    assert ran.lines == [_CLEAN], ran.stderr
    assert ran.status == 0
    assert project.no_database_file()
