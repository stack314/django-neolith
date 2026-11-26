"""Every family of the boundary pass through the command, under both entries
(spec 05, APP-66 to APP-71; spec 02, TRN-41; ADR 0030 Decisions 6 to 8).

The families are functions over a host, each tested on its own in a child
process (``tests/test_check_models.py``, ``test_check_source.py``,
``test_check_imports.py``, ``test_check_calls.py``, ``test_check_views.py``).
Here the command runs them in phase 5: ``neolith check <app> ...
--settings deployer`` on a closed host, and ``manage.py checkapps`` on the
host a settings module builds, each in a fresh process in a project written
under ``tmp_path`` (``tests/checking.py``).

- One row of each family, in a project of its own, with its app listed as
  report-only: an error is printed marked and does not change the status,
  and the app is not a stale entry; a warning names no app's error, so the
  listed app is ``neolith.E113``, which does.
- One project with every row of ADR 0030's findings table at once: each
  printed once, sorted by path, line and id, ``neolith.E113`` after them, the
  summary counting each, the same lines under both entries, and no database
  file created; and every family reads that project's source from one
  reading, so each file is parsed once.
"""

import json
import py_compile
import re
from pathlib import Path
from typing import Final

import pytest

from tests.checking import Project, Ran

_ENTRIES: Final = ["neolith check", "manage.py checkapps"]
_SILENCED: Final = 'SILENCED_SYSTEM_CHECKS = ["neolith.W004"]\n'
# A finding line: <path>:<line>: <id> <message>; <hint>, with
# " (report only)" after an error of a listed app.
_LINE: Final = re.compile(
    r"(?P<path>[^:\s]+):(?P<line>[1-9][0-9]*): (?P<id>neolith\.[EW]1[0-9][0-9]) "
    r"(?P<text>.+; .+?)(?P<marked> \(report only\))?"
)
_PLACELESS: Final = re.compile(r"(?P<id>neolith\.E113) (?P<text>.+; .+)")
_SUMMARY: Final = re.compile(
    r"neolith check: (?P<errors>[0-9]+) errors, (?P<marked>[0-9]+) errors in "
    r"report-only apps, (?P<warnings>[0-9]+) warnings"
)

_ITEM: Final = """\
from django.db import models


class Item(models.Model):
    name = models.CharField(max_length=10)
"""

_RECORDS: Final = """\
from pydantic import BaseModel


class Ping(BaseModel):
    n: int


def later():
    import shop.services

    return shop.services
"""

_CONTRACTS: Final = """\
from typing import Protocol

from neolith import action, contract
from shop.records import Ping


@contract("shop.v1")
class Shop(Protocol):
    @action
    def ping(self, request: Ping) -> Ping: ...
"""

_SERVICES: Final = """\
from shop.records import Ping


class Service:
    def ping(self, request: Ping) -> Ping:
        return request
"""

# A contract module that also holds its implementation (E105).
_POST: Final = """\
from typing import Protocol

from neolith import action, contract
from pydantic import BaseModel


class Echo(BaseModel):
    n: int


@contract("post.v1")
class Post(Protocol):
    @action
    def echo(self, request: Echo) -> Echo: ...


class Service:
    def echo(self, request: Echo) -> Echo:
        return request
"""

_LEGACY_APP: Final = """\
from django.apps import AppConfig


class LegacyConfig(AppConfig):
    name = "legacy"
"""

# One file of desk per family's row.
_DESK_MODELS: Final = """\
from django.db import models


class Note(models.Model):
    item = models.ForeignKey("shop.Item", on_delete=models.CASCADE)


class Same(models.Model):
    class Meta:
        db_table = "SHOP_ITEM"
"""

# The same import on two lines, printed on each.
_DESK_VIEWS: Final = """\
import helpers
import legacy.models
from shop.services import Service
from shop.services import Service
"""

# The same finding twice on one line, printed once.
_DESK_CALLS: Final = """\
from django.apps import apps
from django.db import connections, transaction
from neolith import command_scope, connect


def run(name):
    connect("shop.v1"), connect("shop.v1")
    connect(name)
    with transaction.atomic():
        pass
    command_scope("shop"), connections["default"]
    return apps.get_model("shop", "Item")
"""

_URLS: Final = """\
from django.urls import path


def home(request):
    return None


urlpatterns = [path("", home)]
"""

# Two class-based views of the project's in one file, which is parsed once.
_PROJECT_URLS: Final = """\
from django.urls import path
from django.views import View


def home(request):
    return None


class First(View):
    def get(self, request):
        return None


class Second(View):
    def get(self, request):
        return None


urlpatterns = [
    path("", home),
    path("first", First.as_view()),
    path("second", Second.as_view()),
]
"""

_WSGI: Final = """\
from neolith import testing

testing.boot()
"""


def _line_of(project: Project, path: str, text: str) -> int:
    """The first line of ``path`` that starts with ``text``, unindented."""
    lines = (project.root / path).read_text(encoding="utf-8").splitlines()
    return next(n for n, each in enumerate(lines, 1) if each.strip().startswith(text))


class Host(Project):
    """A project with the hosted apps ``shop`` and ``desk``, each with a
    database, and whatever else a test adds before it calls ``write_hosts``.
    """

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.hosted = ["shop", "desk"]
        self.databases_of = ["shop", "desk"]
        self.undeclared: list[str] = []
        self.app("shop", models=_ITEM)
        self.app("desk")

    def write_hosts(self) -> None:
        """Write the deployer's module and the settings module for one host
        of every hosted app.
        """
        neolith: dict[str, object] = {"DATABASES": self.databases(*self.databases_of)}
        if self.undeclared:
            neolith["UNDECLARED_APPS"] = list(self.undeclared)
        self.deployer(*self.databases_of, neolith=neolith, below=_SILENCED)
        self.settings(self.hosted, neolith=neolith, below=_SILENCED)

    def entry(self, entry: str) -> Ran:
        if entry == "neolith check":
            return self.check(*self.hosted)
        return self.checkapps()

    def no_database_file(self) -> bool:
        return not any(self.root.rglob("*.sqlite3"))


def _parsed(ran: Ran) -> tuple[list[tuple[str, int, str, bool]], list[str]]:
    """Each located finding as (path, line, id, marked), and the ids of
    those with no place, in the order printed; every line of standard
    output before the summary is one or the other.
    """
    located: list[tuple[str, int, str, bool]] = []
    placeless: list[str] = []
    for line in ran.findings:
        match = _LINE.fullmatch(line)
        if match is not None:
            located.append(
                (
                    match["path"],
                    int(match["line"]),
                    match["id"],
                    match["marked"] is not None,
                )
            )
            continue
        bare = _PLACELESS.fullmatch(line)
        assert bare is not None, line
        placeless.append(bare["id"])
    return located, placeless


def _counts(ran: Ran) -> tuple[int, int, int]:
    match = _SUMMARY.fullmatch(ran.summary)
    assert match is not None, ran.stdout + ran.stderr
    return int(match["errors"]), int(match["marked"]), int(match["warnings"])


# One row of each family, its app listed as report-only.


def _models(project: Host) -> tuple[str, int, str]:
    project.write(
        "desk/models.py",
        _DESK_MODELS.split("\n\nclass Same")[0] + "\n",
    )
    return "desk/models.py", _line_of(project, "desk/models.py", "class Note"), "E106"


def _source(project: Host) -> tuple[str, int, str]:
    project.write("desk/broken.py", "x = 1\nreturn x\n")
    return "desk/broken.py", 2, "E112"


def _imports(project: Host) -> tuple[str, int, str]:
    project.write("desk/views.py", "from shop.services import Service\n")
    project.write("shop/services.py", "class Service:\n    pass\n")
    return "desk/views.py", 1, "E101"


def _calls(project: Host) -> tuple[str, int, str]:
    project.write(
        "desk/calls.py", "from neolith import connect\n\nconnect('shop.v1')\n"
    )
    return "desk/calls.py", 3, "E108"


def _views(project: Host) -> tuple[str, int, str]:
    project.write("urls.py", _URLS)
    return "urls.py", _line_of(project, "urls.py", "def home"), "W102"


_FAMILIES: Final = {
    "models": _models,
    "source": _source,
    "imports": _imports,
    "calls": _calls,
    "views": _views,
}


@pytest.mark.spec("APP-66", "APP-67", "APP-68", "APP-69", "APP-71", "TRN-41")
@pytest.mark.parametrize("entry", _ENTRIES)
@pytest.mark.parametrize("family", sorted(_FAMILIES))
def test_one_row_of_each_family_through_the_command_of_a_report_only_app(
    tmp_path: Path, family: str, entry: str
) -> None:
    project = Host(tmp_path)
    path, line, kind = _FAMILIES[family](project)
    project.pyproject("[tool.neolith]\nboundary-report-only = ['desk']\n")
    project.write_hosts()
    ran = project.entry(entry)
    located, placeless = _parsed(ran)
    error = kind.startswith("E")
    assert located == [(path, line, f"neolith.{kind}", error)], ran.stdout + ran.stderr
    assert ran.errors == []
    if error:
        # An error of a listed app is marked, counted apart and does not
        # change the status; the app is not a stale entry.
        assert placeless == []
        assert _counts(ran) == (0, 1, 0)
        assert ran.status == 0
    else:
        # A warning is no app's error: the listed app is a stale entry,
        # which is an error of its own and ends the check with status 1.
        assert placeless == ["neolith.E113"]
        assert ran.findings[-1].startswith("neolith.E113 app desk: ")
        assert _counts(ran) == (1, 0, 1)
        assert ran.status == 1
    assert project.no_database_file()


# Every row at once.


@pytest.fixture(scope="module")
def every_row(tmp_path_factory: pytest.TempPathFactory) -> Host:
    """A project whose code gives every row of ADR 0030's findings table:
    the hosted apps ``shop`` (a contract package that imports its app's
    internals), ``desk`` (every row about an app's own code), ``post`` (an
    implementation in its contract module), ``bare`` and ``bare0`` (no
    finding, listed as report-only), the undeclared first-party app ``legacy``, and the
    project's code in no app.
    """
    project = Host(tmp_path_factory.mktemp("every_row"))
    project.write("shop/records.py", _RECORDS)
    project.write("shop/contracts.py", _CONTRACTS)
    project.write("shop/services.py", _SERVICES)
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
    project.write("post/contracts.py", _POST)
    project.app(
        "post",
        imports="from neolith import Provided\nfrom post.contracts import Post\n",
        config="""\
        provides = (
            Provided(Post, implementation="post.contracts.Service", principal_from=()),
        )
        """,
    )
    project.app("bare")
    project.app("bare0")
    project.app("desk", models=_DESK_MODELS)
    project.write("desk/views.py", _DESK_VIEWS)
    project.write("desk/calls.py", _DESK_CALLS)
    project.write("desk/broken.py", "return\n")
    project.write("desk/old.py", "x = 1\n")
    py_compile.compile(
        str(project.root / "desk" / "old.py"),
        cfile=str(project.root / "desk" / "old.pyc"),
        doraise=True,
    )
    (project.root / "desk" / "old.py").unlink()
    project.write("legacy/__init__.py")
    project.write("legacy/apps.py", _LEGACY_APP)
    project.write("legacy/models.py")
    project.write("legacy/views.py", "import shop.services\n")
    project.write("helpers.py", "import shop.services\n")
    project.write("urls.py", _PROJECT_URLS)
    project.write("wsgi.py", _WSGI)
    project.hosted = ["shop", "post", "bare", "bare0", "desk"]
    project.databases_of = list(project.hosted)
    project.undeclared = ["legacy"]
    project.write_hosts()
    return project


def _every_row(project: Host) -> list[tuple[str, int, str]]:
    """Each row's (path, line, id), worked out from the files."""

    def at(path: str, text: str) -> int:
        return _line_of(project, path, text)

    calls = "desk/calls.py"
    return [
        (calls, at(calls, 'connect("shop.v1")'), "E108"),
        (calls, at(calls, "connect(name)"), "W101"),
        (calls, at(calls, "with transaction.atomic()"), "E109"),
        # One line, whose ids sort apart from their messages.
        (calls, at(calls, "command_scope"), "E109"),
        (calls, at(calls, "command_scope"), "E110"),
        (calls, at(calls, "return apps.get_model"), "E111"),
        ("desk/broken.py", 1, "E112"),
        ("desk/models.py", at("desk/models.py", "class Note"), "E106"),
        ("desk/models.py", at("desk/models.py", "class Same"), "E107"),
        ("desk/old.pyc", 1, "W104"),
        ("desk/views.py", 1, "E103"),
        ("desk/views.py", 2, "E102"),
        ("desk/views.py", 3, "E101"),
        ("desk/views.py", 4, "E101"),
        ("legacy/views.py", 1, "W105"),
        ("post/contracts.py", at("post/contracts.py", "class Service"), "E105"),
        ("shop/models.py", at("shop/models.py", "class Item"), "E107"),
        ("shop/records.py", at("shop/records.py", "import shop.services"), "E104"),
        ("urls.py", at("urls.py", "def home"), "W102"),
        ("urls.py", at("urls.py", "class First"), "W102"),
        ("urls.py", at("urls.py", "class Second"), "W102"),
        ("wsgi.py", at("wsgi.py", "testing.boot()"), "W103"),
    ]


def _sorted(rows: list[tuple[str, int, str]]) -> list[tuple[str, int, str]]:
    return sorted(rows, key=lambda row: (row[0], row[1], f"neolith.{row[2]}"))


@pytest.mark.spec("APP-66", "APP-67", "APP-68", "APP-69", "APP-70", "APP-71", "TRN-41")
def test_every_row_at_once_under_both_entries(every_row: Host) -> None:
    every_row.pyproject("[tool.neolith]\nboundary-report-only = ['bare', 'bare0']\n")
    script = every_row.entry("neolith check")
    command = every_row.entry("manage.py checkapps")
    assert script.stdout == command.stdout
    assert script.errors == command.errors == []
    assert script.status == command.status == 1
    located, placeless = _parsed(script)
    rows = _every_row(every_row)
    # Every id of the findings table, each row once.
    ids = {row[2] for row in rows} | {"E113"}
    assert ids == {f"E{n}" for n in range(101, 114)} | {
        f"W{n}" for n in range(101, 106)
    }
    assert [(path, line, kind) for path, line, kind, _ in located] == [
        (path, line, f"neolith.{kind}") for path, line, kind in _sorted(rows)
    ], script.stdout
    assert not any(marked for *_, marked in located)
    # By app, though by message "app bare0:" sorts before "app bare:".
    assert placeless == ["neolith.E113", "neolith.E113"]
    assert script.findings[-2].startswith("neolith.E113 app bare: ")
    assert script.findings[-1].startswith("neolith.E113 app bare0: ")
    assert len(set(script.findings)) == len(script.findings)
    errors = sum(1 for _, _, kind in rows if kind.startswith("E")) + 2
    warnings = sum(1 for _, _, kind in rows if kind.startswith("W"))
    assert _counts(script) == (errors, 0, warnings)
    assert every_row.no_database_file()


# The entry run with each ``ast.parse`` that neolith's own code makes
# counted per file, the counts printed to standard error however the entry
# ends. Other code's parses are not counted: on Python 3.12 the standard
# library parses with no file name, as ``inspect.findsource`` does a
# class's whole module.
_COUNTED: Final = """\
import ast, collections, json, sys

seen = collections.Counter()
parse = ast.parse


def counted(source, filename="<unknown>", *args, **kwargs):
    caller = sys._getframe(1).f_globals.get("__name__", "")
    if caller == "neolith" or caller.startswith("neolith."):
        seen[filename] += 1
    return parse(source, filename, *args, **kwargs)


ast.parse = counted
try:
    if sys.argv[1] == "check":
        from neolith._cli import main

        status = main(sys.argv[1:])
    else:
        from django.core.management import execute_from_command_line

        execute_from_command_line(["django", *sys.argv[1:]])
        status = 0
finally:
    print("PARSED " + json.dumps(seen), file=sys.stderr)
sys.exit(status)
"""


@pytest.mark.parametrize(
    "arguments",
    [
        ["check", "shop", "post", "bare", "bare0", "desk", "--settings", "deployer"],
        ["checkapps", "--settings=settings"],
    ],
    ids=_ENTRIES,
)
def test_every_family_reads_one_reading_of_the_source(
    every_row: Host, arguments: list[str]
) -> None:
    every_row.pyproject("[tool.neolith]\nboundary-report-only = ['bare', 'bare0']\n")
    ran = every_row.run("-c", _COUNTED, *arguments)
    assert ran.status == 1, ran.stdout + ran.stderr
    counts = json.loads(ran.errors[-1].removeprefix("PARSED "))
    # A parse of neolith's with no file name would be one this count cannot
    # place.
    assert "<unknown>" not in counts, counts
    parsed = {
        Path(name).resolve().relative_to(every_row.root.resolve()).as_posix(): times
        for name, times in counts.items()
    }
    # Every file of the hosted apps is read, and every file the families
    # read, the chain's helper and the undeclared app's included, is parsed
    # once, however many families judge it.
    hosted = {
        path.relative_to(every_row.root).as_posix()
        for app in every_row.hosted
        for path in (every_row.root / app).rglob("*.py")
    }
    assert hosted <= set(parsed), sorted(hosted - set(parsed))
    assert {"helpers.py", "legacy/views.py", "urls.py"} <= set(parsed)
    assert {name: times for name, times in parsed.items() if times != 1} == {}


@pytest.mark.spec("APP-71")
def test_every_row_with_every_failing_app_report_only(every_row: Host) -> None:
    every_row.pyproject(
        "[tool.neolith]\nboundary-report-only = ['desk', 'post', 'shop']\n"
    )
    script = every_row.entry("neolith check")
    command = every_row.entry("manage.py checkapps")
    assert script.stdout == command.stdout
    assert script.errors == command.errors == []
    assert script.status == command.status == 0
    located, placeless = _parsed(script)
    rows = _sorted(_every_row(every_row))
    assert located == [
        (path, line, f"neolith.{kind}", kind.startswith("E"))
        for path, line, kind in rows
    ], script.stdout
    assert placeless == []
    errors = sum(1 for _, _, kind in rows if kind.startswith("E"))
    warnings = len(rows) - errors
    assert _counts(script) == (0, errors, warnings)
    assert every_row.no_database_file()


@pytest.mark.spec("APP-71")
@pytest.mark.parametrize("entry", _ENTRIES)
def test_a_listed_app_whose_only_findings_are_warnings_is_a_stale_entry(
    tmp_path: Path, entry: str
) -> None:
    project = Host(tmp_path)
    project.write("desk/calls.py", "from neolith import connect\n\nconnect(NAME)\n")
    project.write("desk/setup_host.py", _WSGI)
    project.write("desk/old.py", "x = 1\n")
    py_compile.compile(
        str(project.root / "desk" / "old.py"),
        cfile=str(project.root / "desk" / "old.pyc"),
        doraise=True,
    )
    (project.root / "desk" / "old.py").unlink()
    project.write("legacy/__init__.py")
    project.write("legacy/apps.py", _LEGACY_APP)
    project.write("legacy/views.py", "import desk.calls\n")
    project.undeclared = ["legacy"]
    project.pyproject("[tool.neolith]\nboundary-report-only = ['desk']\n")
    project.write_hosts()
    ran = project.entry(entry)
    located, placeless = _parsed(ran)
    assert located == [
        ("desk/calls.py", 3, "neolith.W101", False),
        ("desk/old.pyc", 1, "neolith.W104", False),
        ("desk/setup_host.py", 3, "neolith.W103", False),
        ("legacy/views.py", 1, "neolith.W105", False),
    ], ran.stdout + ran.stderr
    # Each warning names desk, and none is an error of desk's.
    assert all(" app desk: " in line for line in ran.findings[:-1])
    assert placeless == ["neolith.E113"]
    assert ran.findings[-1].startswith("neolith.E113 app desk: ")
    assert _counts(ran) == (1, 0, 4)
    assert ran.status == 1


# The contract package's edges that the guide states (docs/django-host.md).

_PING: Final = """\
from typing import Protocol

from neolith import action, contract
from pydantic import BaseModel


class Ping(BaseModel):
    n: int


@contract("{name}")
class Contract(Protocol):
    @action
    def ping(self, request: Ping) -> Ping: ...
"""

_PONG: Final = """\
from {package}.contracts import Ping


class Service:
    def ping(self, request: Ping) -> Ping:
        return request
"""


def _provider(project: Project, name: str, label: str, contract: str) -> None:
    """A hosted app ``name`` that provides ``contract`` from its
    ``contracts.py``, implemented in ``services.py``.
    """
    base = name.replace(".", "/")
    project.write(f"{base}/contracts.py", _PING.format(name=contract))
    project.write(f"{base}/services.py", _PONG.format(package=name))
    project.app(
        name,
        label=label,
        imports=(
            f"from neolith import Provided\nfrom {name}.contracts import Contract\n"
        ),
        config=f"""\
        provides = (
            Provided(
                Contract,
                implementation="{name}.services.Service",
                principal_from=(),
            ),
        )
        """,
    )


def _through_an_undeclared_app(project: Project) -> list[str]:
    # A chain through an undeclared app's code: it is code in no hosted
    # app, for E103 and for E104 alike.
    _provider(project, "shop", "shop", "shop.v1")
    project.write(
        "shop/contracts.py", "import legacy.util\n" + _PING.format(name="shop.v1")
    )
    project.write("shop/helpers.py", "Y = 1\n")
    project.app("desk")
    project.write("desk/views.py", "import legacy.util\n")
    project.write("legacy/__init__.py")
    project.write("legacy/apps.py", _LEGACY_APP)
    project.write("legacy/util.py", "import shop.helpers\n")
    project.deployer(
        "shop",
        "desk",
        neolith={
            "DATABASES": project.databases("shop", "desk"),
            "UNDECLARED_APPS": ["legacy"],
        },
        below=_SILENCED,
    )
    return ["shop", "desk"]


def _through_a_package_in_no_app(project: Project) -> list[str]:
    # A package __init__.py in no app, above a contract module, is code in
    # no app: a chain goes through it.
    project.write("org/__init__.py", "def later():\n    import billing.services\n")
    _provider(project, "org.shop", "shop", "shop.v1")
    project.app("billing")
    project.write("billing/services.py", "X = 1\n")
    project.app("desk")
    project.write("desk/views.py", "import helpers\n")
    project.write("helpers.py", "import org.shop.contracts\n")
    project.deployer("shop", "billing", "desk", below=_SILENCED)
    return ["org.shop", "billing", "desk"]


def _above_a_nested_app(project: Project) -> list[str]:
    # An app with no contract package of its own: its __init__.py above a
    # nested app's contract module is part of the nested app's, and its
    # other modules are part of none.
    project.app("shop")
    project.write("shop/__init__.py", "X = 1\n")
    _provider(project, "shop.desk", "desk", "desk.v1")
    project.app("billing")
    project.write("billing/views.py", "from shop import X\nimport shop.models\n")
    project.deployer("shop", "desk", "billing", below=_SILENCED)
    return ["shop", "shop.desk", "billing"]


_EDGES: Final = {
    "through an undeclared app": (
        _through_an_undeclared_app,
        [
            ("desk/views.py", 1, "neolith.E103", False),
            ("legacy/util.py", 1, "neolith.W105", False),
            ("shop/contracts.py", 1, "neolith.E104", False),
        ],
    ),
    "through a package in no app": (
        _through_a_package_in_no_app,
        [
            ("desk/views.py", 1, "neolith.E103", False),
            ("org/shop/__init__.py", 1, "neolith.E103", False),
        ],
    ),
    "above a nested app": (
        _above_a_nested_app,
        [("billing/views.py", 2, "neolith.E101", False)],
    ),
}


@pytest.mark.spec("APP-66")
@pytest.mark.parametrize("case", sorted(_EDGES))
def test_the_contract_package_s_edges_the_guide_states(
    tmp_path: Path, case: str
) -> None:
    write, expected = _EDGES[case]
    project = Project(tmp_path)
    hosted = write(project)
    ran = project.check(*hosted)
    located, placeless = _parsed(ran)
    assert located == expected, ran.stdout + ran.stderr
    assert placeless == []
    assert ran.status == 1
