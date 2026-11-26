"""The check's rules on imports: another hosted app's internals
(``neolith.E101``), an undeclared app's models (``neolith.E102``), a contract
package that does not stand alone (``neolith.E104``), a module left out of
it (``neolith.E105``) and an undeclared app's reach into a hosted app
(``neolith.W105``) (spec 05, APP-66; ADR 0030 Decisions 1 to 3). Chains
(``neolith.E103``) are in ``tests/test_check_chains.py``.

The rules are one function over a host. Each project is written under a
temporary directory and the rules run on it in a child process that sets
the host up from its settings module, with every database connection
refused (``tests/checking.py``). A host with undeclared apps, contrib apps
among them, has a real ``default``.
"""

import ast
from pathlib import Path

import pytest

from neolith._check_imports import _defined
from tests.checking import Project

pytestmark = pytest.mark.spec("APP-66")

INTERNAL_HINT = (
    "import only that app's contract package, and reach the rest through a "
    "contract it provides"
)
MODELS_HINT = (
    "keep that app's models out of this app's code, and reach its records "
    "through a contract"
)
ALONE_HINT = (
    "a contract package stands alone: move what it needs into the contract "
    "package or into code in no app"
)
_LEFT_OUT_HINT = (
    "move the models and the implementation into modules of their own, outside "
    "the contract package"
)
_UNDECLARED_HINT = (
    "until that app has a declaration and reaches this one through its "
    "contract package, the two cannot run apart"
)

# Run in the child: set the host up, then run the import rules over the
# hosted apps' source with every new database connection refused, and print
# each finding as the command prints it, sorted.
_SCRIPT = """\
import django

django.setup()

from django.apps import apps

from neolith import _check, _check_imports
from neolith._check_source import Source
from neolith._declaration import declaration_of

hosted = {
    config.label: config
    for config in apps.get_app_configs()
    if declaration_of(type(config)) is not None
}
with _check.guarded():
    source = Source(hosted, _check._owner, settings_module="settings")
    found = _check_imports.findings(source)
for finding in sorted(found, key=_check._order):
    print(_check._line(finding, report_only=False))
"""


def rules(project: Project, env: dict[str, str] | None = None) -> list[str]:
    """The import rules' findings on ``project``, as the command prints
    them, sorted.
    """
    ran = project.run(
        "-c", _SCRIPT, env={"DJANGO_SETTINGS_MODULE": "settings", **(env or {})}
    )
    assert ran.status == 0, ran.stdout + ran.stderr
    assert ran.stderr == "", ran.stderr
    return ran.findings


def line_of(project: Project, path: str, text: str) -> int:
    """The first line of ``path`` in ``project`` that starts with ``text``
    once indentation is taken off.
    """
    lines = (project.root / path).read_text(encoding="utf-8").splitlines()
    return next(n for n, each in enumerate(lines, 1) if each.strip().startswith(text))


def provider(
    project: Project,
    app: str,
    contracts: str,
    name: str,
    implementation: str,
    models: str = "",
) -> None:
    """Write hosted app ``app``, which provides the contract ``name`` of the
    module ``contracts`` with ``implementation``.
    """
    project.app(
        app,
        models=models,
        imports=f"from neolith import Provided\nfrom {contracts} import {name}\n",
        config=f"""\
        provides = (
            Provided({name}, implementation={implementation!r}, principal_from=()),
        )
        """,
    )


def contract(identity: str, records: str, name: str = "Plain") -> str:
    """A contract module whose one action takes and gives ``records.Ping``."""
    return f"""\
from typing import Protocol

from neolith import action, contract
from {records} import Ping


@contract({identity!r})
class {name}(Protocol):
    @action
    def ping(self, request: Ping) -> Ping: ...
"""


RECORDS = """\
from pydantic import BaseModel


class Ping(BaseModel):
    n: int
"""


def service(records: str) -> str:
    return f"""\
from {records} import Ping


class Service:
    def ping(self, request: Ping) -> Ping:
        return request
"""


_SHOP_MODELS = """\
from django.db import models


class Item(models.Model):
    name = models.CharField(max_length=10)


class Tag(models.Model):
    group = models.ForeignKey("auth.Group", on_delete=models.CASCADE, related_name="+")
    thing = models.ForeignKey(
        "legacy.Thing", on_delete=models.CASCADE, related_name="+"
    )
"""

_LEGACY_APP = """\
from django.apps import AppConfig


class LegacyConfig(AppConfig):
    name = "legacy"
    default_auto_field = "django.db.models.AutoField"
"""

# A models package that re-exports its models, one of them from a module
# outside it, which its __module__ names.
_LEGACY_MODELS = """\
from legacy.models.thing import Thing
from legacy.odd import Odd

__all__ = ["Odd", "Thing"]
"""

_LEGACY_THING = """\
from django.contrib.contenttypes.fields import GenericRelation
from django.db import models
from shop.models import Item


class Thing(models.Model):
    item = models.ForeignKey("shop.Item", on_delete=models.CASCADE, related_name="+")
    one = models.OneToOneField(Item, on_delete=models.CASCADE, related_name="+")
    items = models.ManyToManyField("shop.Item", related_name="+")
    groups = models.ManyToManyField("auth.Group", through="shop.Tag", related_name="+")
    generic = GenericRelation(Item, related_query_name="+")
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="+")
    users = models.ManyToManyField("auth.User", related_name="+")

    class Meta:
        app_label = "legacy"


class ItemChild(Item):
    class Meta:
        app_label = "legacy"
"""

_LEGACY_ODD = """\
from django.db import models


class Odd(models.Model):
    class Meta:
        app_label = "legacy"
"""

# Every kind of target, imported by a hosted app.
_DESK_VIEWS = """\
import importlib
from typing import TYPE_CHECKING

import legacy.helpers
import legacy.models
import legacy.odd
import shop
import shop.contracts
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import AnonymousUser
from legacy.models import Thing
from legacy.models.thing import Thing as Same
from shop import contracts, models
from shop.models import Item
from shop.records import Ping
from shop.services import Service

if TYPE_CHECKING:
    from shop.apps import Config


def later():
    from shop import services

    return importlib.import_module("shop.services"), services
"""

_SHOP_VIEWS = """\
from desk import views
"""

_LEGACY_VIEWS = """\
from desk.views import later
from shop.contracts import Plain
from shop.models import Item
from shop.services import Service
from django.contrib.auth.models import User
from legacy.models import Thing
"""

# The matrix: each importer imports each kind of target.
_TARGETS = {
    "contract": "shop.contracts",
    "internals": "shop.services",
    "hosted models": "shop.models",
    "undeclared models": "legacy.models",
    "contrib models": "django.contrib.auth.models",
}
_IMPORTERS = {
    "hosted app": "desk",
    "undeclared first-party app": "legacy",
    "code in no app": "nowhere",
}


@pytest.fixture(scope="module")
def host(tmp_path_factory: pytest.TempPathFactory) -> tuple[Project, list[str]]:
    """Two hosted apps, ``shop``, which provides a contract, and ``desk``,
    which provides none, beside an undeclared first-party app ``legacy``,
    code in no app ``nowhere`` and the contrib apps ``auth`` and
    ``contenttypes``, on a real ``default``; and the rules' findings.
    """
    project = Project(tmp_path_factory.mktemp("host"))
    project.write("shop/records.py", RECORDS)
    project.write("shop/contracts.py", contract("shop.v1", "shop.records"))
    project.write("shop/services.py", service("shop.records"))
    project.write("shop/views.py", _SHOP_VIEWS)
    provider(
        project,
        "shop",
        "shop.contracts",
        "Plain",
        "shop.services.Service",
        models=_SHOP_MODELS,
    )
    project.app("desk")
    project.write("desk/views.py", _DESK_VIEWS)
    project.write("legacy/__init__.py")
    project.write("legacy/apps.py", _LEGACY_APP)
    project.write("legacy/models/__init__.py", _LEGACY_MODELS)
    project.write("legacy/models/thing.py", _LEGACY_THING)
    project.write("legacy/odd.py", _LEGACY_ODD)
    project.write("legacy/helpers.py", "import json\n")
    project.write("legacy/views.py", _LEGACY_VIEWS)
    project.write("nowhere/__init__.py")
    for importer in _IMPORTERS.values():
        for kind, target in _TARGETS.items():
            project.write(
                f"{importer}/m_{kind.replace(' ', '_')}.py", f"import {target}\n"
            )
    project.settings(
        ["shop", "desk"],
        neolith={
            "DATABASES": project.databases("default", "shop", "desk"),
            "UNDECLARED_APPS": [
                "django.contrib.contenttypes",
                "django.contrib.auth",
                "legacy",
            ],
        },
        below='SILENCED_SYSTEM_CHECKS = ["neolith.W004"]\n',
    )
    return project, rules(project)


def _e101(module: str, path: str, line: int, target: str) -> str:
    app = module.partition(".")[0]
    owner = target.partition(".")[0]
    note = ", and app desk provides no contract" if owner == "desk" else ""
    return (
        f"{path}:{line}: neolith.E101 app {app}: module {module}: imports {target}, "
        f"a module of app {owner} outside its contract package{note}; "
        f"{INTERNAL_HINT}"
    )


def _e102(module: str, path: str, line: int, target: str, app: str) -> str:
    owner = "desk" if module.startswith("desk") else "shop"
    return (
        f"{path}:{line}: neolith.E102 app {owner}: module {module}: imports "
        f"{target}, a models module of app {app}, which has no declaration; "
        f"{MODELS_HINT}"
    )


def _w105(module: str, path: str, line: int, target: str) -> str:
    app = target.partition(".")[0]
    return (
        f"{path}:{line}: neolith.W105 app {app}: module {module} of app legacy, "
        f"which has no declaration: imports {target}, a module of app {app} "
        f"outside its contract package; {_UNDECLARED_HINT}"
    )


def _of(found: list[str], *ids: str) -> list[str]:
    return [line for line in found if any(f" {each} " in line for each in ids)]


def test_another_hosted_app_s_internals_are_e101_and_its_contract_package_is_not(
    host: tuple[Project, list[str]],
) -> None:
    project, found = host
    path = "desk/views.py"

    def at(text: str) -> int:
        return line_of(project, path, text)

    expected = [
        _e101("desk.views", path, at("from shop import contracts"), "shop.models"),
        _e101("desk.views", path, at("from shop.models"), "shop.models"),
        _e101("desk.views", path, at("from shop.services"), "shop.services"),
        _e101("desk.views", path, at("from shop.apps"), "shop.apps"),
        _e101("desk.views", path, at("from shop import services"), "shop.services"),
        _e101("desk.views", path, at("return importlib"), "shop.services"),
        _e101("desk.m_hosted_models", "desk/m_hosted_models.py", 1, "shop.models"),
        _e101("desk.m_internals", "desk/m_internals.py", 1, "shop.services"),
        # An app that provides no contract has no contract package.
        _e101("shop.views", "shop/views.py", 1, "desk.views"),
    ]
    assert sorted(_of(found, "neolith.E101")) == sorted(expected), "\n".join(found)


def test_an_undeclared_app_s_models_module_is_e102_and_its_other_modules_are_not(
    host: tuple[Project, list[str]],
) -> None:
    project, found = host
    path = "desk/views.py"

    def at(text: str) -> int:
        return line_of(project, path, text)

    auth = "django.contrib.auth.models"
    assert sorted(_of(found, "neolith.E102")) == sorted(
        [
            _e102(
                "desk.views",
                path,
                at("import legacy.models"),
                "legacy.models",
                "legacy",
            ),
            # A module outside the models package that a model names.
            _e102("desk.views", path, at("import legacy.odd"), "legacy.odd", "legacy"),
            # An import is of a module: a name that is no model still loads it.
            _e102(
                "desk.views", path, at("from django.contrib.auth.models"), auth, "auth"
            ),
            # A models package that re-exports its models.
            _e102(
                "desk.views",
                path,
                at("from legacy.models import"),
                "legacy.models",
                "legacy",
            ),
            _e102(
                "desk.views",
                path,
                at("from legacy.models.thing"),
                "legacy.models.thing",
                "legacy",
            ),
            _e102(
                "desk.m_undeclared_models",
                "desk/m_undeclared_models.py",
                1,
                "legacy.models",
                "legacy",
            ),
            _e102("desk.m_contrib_models", "desk/m_contrib_models.py", 1, auth, "auth"),
        ]
    ), "\n".join(found)
    # django.contrib.auth.decorators, django.contrib.auth and legacy.helpers
    # are not models modules.
    for text in ["from django.contrib.auth", "import legacy.helpers"]:
        assert not any(f"{path}:{at(text)}:" in line for line in found), text


def test_an_undeclared_first_party_app_reaching_a_hosted_app_is_w105(
    host: tuple[Project, list[str]],
) -> None:
    project, found = host
    path = "legacy/views.py"

    def at(text: str) -> int:
        return line_of(project, path, text)

    thing = "legacy/models/thing.py"

    def related(field: str, target: str = "shop.Item", model: str = "Thing") -> str:
        at = line_of(project, thing, f"class {model}(")
        return (
            f"{thing}:{at}: neolith.W105 app shop: model legacy.{model} of app "
            f"legacy, which has no declaration: field {field} is a relation to "
            f"{target}, a model of app shop; {_UNDECLARED_HINT}"
        )

    assert sorted(_of(found, "neolith.W105")) == sorted(
        [
            _w105("legacy.views", path, at("from desk.views"), "desk.views"),
            _w105("legacy.views", path, at("from shop.models"), "shop.models"),
            _w105("legacy.views", path, at("from shop.services"), "shop.services"),
            _w105(
                "legacy.m_hosted_models", "legacy/m_hosted_models.py", 1, "shop.models"
            ),
            _w105("legacy.m_internals", "legacy/m_internals.py", 1, "shop.services"),
            # Each relation to a hosted app's model, through one included;
            # one to auth's is admitted.
            _w105("legacy.models.thing", thing, 3, "shop.models"),
            related("item"),
            related("one"),
            related("items"),
            related("groups", "shop.Tag"),
            related("generic"),
            related("item_ptr", model="ItemChild"),
        ]
    ), "\n".join(found)


@pytest.mark.parametrize("importer", sorted(_IMPORTERS))
@pytest.mark.parametrize("target", sorted(_TARGETS))
def test_each_importer_and_target(
    host: tuple[Project, list[str]], importer: str, target: str
) -> None:
    _, found = host
    expected = {
        ("hosted app", "internals"): "neolith.E101",
        ("hosted app", "hosted models"): "neolith.E101",
        ("hosted app", "undeclared models"): "neolith.E102",
        ("hosted app", "contrib models"): "neolith.E102",
        ("undeclared first-party app", "internals"): "neolith.W105",
        ("undeclared first-party app", "hosted models"): "neolith.W105",
    }.get((importer, target))
    path = f"{_IMPORTERS[importer]}/m_{target.replace(' ', '_')}.py:"
    ids = [line.split(" ")[1] for line in found if line.startswith(path)]
    assert ids == ([expected] if expected else []), "\n".join(found)


def test_nothing_else_is_found(host: tuple[Project, list[str]]) -> None:
    _, found = host
    ids = {"neolith.E101", "neolith.E102", "neolith.W105"}
    assert _of(found, *ids) == found, "\n".join(found)


# The contract package stands alone (E104), and a module left out of it is
# reported (E105).

_PK_API = """\
from pk import services
"""

_PK_CONTRACTS = """\
import json

import nowhere.util
from pk import helpers
from pk.api.records import Ping
from shop.contracts import Plain
import pk.internal.deep
from typing import Protocol

from neolith import action, contract


@contract("pk.v1")
class Pk(Protocol):
    @action
    def ping(self, request: Ping) -> Ping: ...
"""

_PM_API = """\
from pm.kinds import Ping


class Service:
    def ping(self, request: Ping) -> Ping:
        return request
"""

_PM_CONTRACTS = """\
from typing import Protocol

from neolith import action, contract
from pm.kinds import Ping


@contract("pm.v1")
class Pm(Protocol):
    @action
    def ping(self, request: Ping) -> Ping: ...
"""

_PM_KINDS = """\
from pydantic import BaseModel


class Ping(BaseModel):
    n: int


def later():
    from pm.api import contracts

    return contracts
"""

_JOINED = """\
from typing import Protocol

from pydantic import BaseModel

from neolith import action, contract


class Ping(BaseModel):
    n: int


@contract("joined.v1")
class Joined(Protocol):
    @action
    def ping(self, request: Ping) -> Ping: ...


class Service:
    def ping(self, request: Ping) -> Ping:
        return request
"""

# A record module whose model is built in a function, which the models
# module calls: a module that defines a model while apps load cannot be
# imported before them.
_MM_RECORDS = """\
from pydantic import BaseModel


class Ping(BaseModel):
    n: int


class Holder:
    class Thing:
        pass


def things():
    from django.db import models

    class Thing(models.Model):
        name = models.CharField(max_length=10)

        class Meta:
            app_label = "mm"

    class Other(models.Model):
        name = models.CharField(max_length=10)

        class Meta:
            app_label = "mm"

    return Thing, Other
"""

# A module that holds the implementation in a block at module scope: in an
# if statement, before a model; in a try statement.
_IFS = """\
from typing import Protocol

from pydantic import BaseModel

from neolith import action, contract


class Ping(BaseModel):
    n: int


@contract("ifs.v1")
class Ifs(Protocol):
    @action
    def ping(self, request: Ping) -> Ping: ...


if Ping:

    class Service:
        def ping(self, request: Ping) -> Ping:
            return request


def thing():
    from django.db import models

    class Thing(models.Model):
        name = models.CharField(max_length=10)

        class Meta:
            app_label = "ifs"

    return Thing
"""

_TRY = """\
from typing import Protocol

from pydantic import BaseModel

from neolith import action, contract


class Ping(BaseModel):
    n: int


@contract("tr.v1")
class Tr(Protocol):
    @action
    def ping(self, request: Ping) -> Ping: ...


try:
    import json

    class Service:
        def ping(self, request: Ping) -> Ping:
            return request

except ImportError:
    json = None
"""

_PP_API = """\
from pp.api.records import Ping


class Service:
    def ping(self, request: Ping) -> Ping:
        return request
"""

_OUTER_INIT = """\
from outer import secret
"""

_INNER_INIT = """\
from outer.inner import services
"""


@pytest.fixture(scope="module")
def packages(tmp_path_factory: pytest.TempPathFactory) -> tuple[Project, list[str]]:
    """Hosted apps whose contract packages do not stand alone, or leave a
    module out; and the rules' findings.
    """
    project = Project(tmp_path_factory.mktemp("packages"))
    project.write("shop/records.py", RECORDS)
    project.write("shop/contracts.py", contract("shop.v1", "shop.records"))
    project.write("shop/services.py", service("shop.records"))
    provider(project, "shop", "shop.contracts", "Plain", "shop.services.Service")
    project.write("nowhere/__init__.py")
    project.write("nowhere/util.py", "import json\n")
    # A package module above a contract module and the contract module
    # itself import the app's internals.
    project.write("pk/api/__init__.py", _PK_API)
    project.write("pk/api/contracts.py", _PK_CONTRACTS)
    project.write("pk/api/records.py", RECORDS)
    project.write("pk/internal/__init__.py")
    project.write("pk/internal/deep.py")
    project.write("pk/services.py", service("pk.api.records"))
    project.write("pk/helpers.py")
    provider(project, "pk", "pk.api.contracts", "Pk", "pk.services.Service")
    # A package above a contract module holds the implementation, and a
    # record module imports the contract module below it.
    project.write("pm/api/__init__.py", _PM_API)
    project.write("pm/api/contracts.py", _PM_CONTRACTS)
    project.write("pm/kinds.py", _PM_KINDS)
    provider(project, "pm", "pm.api.contracts", "Pm", "pm.api.Service")
    # The contract module holds the implementation.
    project.write("joined/contracts.py", _JOINED)
    provider(
        project, "joined", "joined.contracts", "Joined", "joined.contracts.Service"
    )
    # A record module defines a model.
    project.write("mm/records.py", _MM_RECORDS)
    project.write("mm/contracts.py", contract("mm.v1", "mm.records"))
    project.write("mm/services.py", service("mm.records"))
    provider(
        project,
        "mm",
        "mm.contracts",
        "Plain",
        "mm.services.Service",
        models="from mm.records import things\n\nThing, Other = things()\n",
    )
    project.write("ifs/contracts.py", _IFS)
    provider(
        project,
        "ifs",
        "ifs.contracts",
        "Ifs",
        "ifs.contracts.Service",
        models="from ifs.contracts import thing\n\nThing = thing()\n",
    )
    project.write("tr/contracts.py", _TRY)
    provider(project, "tr", "tr.contracts", "Tr", "tr.contracts.Service")
    # A package above two modules of the contract package holds the
    # implementation.
    project.write("pp/api/__init__.py", _PP_API)
    project.write("pp/api/contracts.py", contract("pp.v1", "pp.api.records"))
    project.write("pp/api/records.py", RECORDS)
    provider(project, "pp", "pp.api.contracts", "Plain", "pp.api.Service")
    # A hosted app nested in another: the package modules above its
    # contract module, its own and the outer app's, import internals.
    project.app("outer")
    project.write("outer/__init__.py", _OUTER_INIT)
    project.write("outer/secret.py")
    project.write("outer/inner/__init__.py", _INNER_INIT)
    project.write("outer/inner/records.py", RECORDS)
    project.write(
        "outer/inner/contracts.py", contract("inner.v1", "outer.inner.records")
    )
    project.write("outer/inner/services.py", service("outer.inner.records"))
    provider(
        project,
        "outer.inner",
        "outer.inner.contracts",
        "Plain",
        "outer.inner.services.Service",
    )
    # A second nested app, whose contract package also holds the outer
    # app's __init__.py: E104 names the first app in sorted order.
    project.write("outer/inner2/records.py", RECORDS)
    project.write(
        "outer/inner2/contracts.py", contract("inner2.v1", "outer.inner2.records")
    )
    project.write("outer/inner2/services.py", service("outer.inner2.records"))
    provider(
        project,
        "outer.inner2",
        "outer.inner2.contracts",
        "Plain",
        "outer.inner2.services.Service",
    )
    hosted = [
        "shop",
        "pk",
        "pm",
        "joined",
        "mm",
        "ifs",
        "tr",
        "pp",
        "outer",
        "outer.inner",
        "outer.inner2",
    ]
    labels = [name.rpartition(".")[2] for name in hosted]
    project.settings(
        hosted,
        neolith={"DATABASES": project.databases(*labels)},
        below='SILENCED_SYSTEM_CHECKS = ["neolith.W004"]\n',
    )
    return project, rules(project)


def _e104(
    path: str,
    line: int,
    module: str,
    app: str,
    target: str,
    runs: str = "",
    of: str | None = None,
) -> str:
    runs = f", which runs {runs}" if runs else ""
    return (
        f"{path}:{line}: neolith.E104 app {app}: module {module}, in the contract "
        f"package of app {of or app}: imports {target}{runs}, a module of app "
        f"{app} outside its contract package; {ALONE_HINT}"
    )


def test_a_contract_package_that_imports_its_app_s_internals_is_e104(
    packages: tuple[Project, list[str]],
) -> None:
    project, found = packages
    contracts = "pk/api/contracts.py"
    assert sorted(_of(found, "neolith.E104")) == sorted(
        [
            # A package module above a contract module.
            _e104("pk/api/__init__.py", 1, "pk.api", "pk", "pk.services"),
            # The contract module, by a module and by a package below one.
            _e104(
                contracts,
                line_of(project, contracts, "from pk import helpers"),
                "pk.api.contracts",
                "pk",
                "pk.helpers",
            ),
            _e104(
                contracts,
                line_of(project, contracts, "import pk.internal.deep"),
                "pk.api.contracts",
                "pk",
                "pk.internal.deep",
            ),
            # A record module that imports a contract module below a package
            # left out: Python runs that package too.
            _e104(
                "pm/kinds.py",
                line_of(project, "pm/kinds.py", "from pm.api import"),
                "pm.kinds",
                "pm",
                "pm.api.contracts",
                runs="pm.api",
            ),
            # A contract module that imports a record module left out.
            _e104(
                "mm/contracts.py",
                line_of(project, "mm/contracts.py", "from mm.records"),
                "mm.contracts",
                "mm",
                "mm.records",
            ),
            # A nested hosted app's __init__.py, and the outer app's above it,
            # each held to its own app's internals.
            _e104(
                "outer/inner/__init__.py",
                1,
                "outer.inner",
                "inner",
                "outer.inner.services",
            ),
            _e104("outer/__init__.py", 1, "outer", "outer", "outer.secret", of="inner"),
        ]
    ), "\n".join(found)


def test_a_module_left_out_of_the_contract_package_is_e105(
    packages: tuple[Project, list[str]],
) -> None:
    project, found = packages
    joined = line_of(project, "joined/contracts.py", "class Service")
    pm = line_of(project, "pm/api/__init__.py", "class Service")
    mm = line_of(project, "mm/records.py", "class Thing(models.Model)")
    ifs = line_of(project, "ifs/contracts.py", "class Service")
    assert ifs < line_of(project, "ifs/contracts.py", "class Thing")
    pp = line_of(project, "pp/api/__init__.py", "class Service")
    assert sorted(_of(found, "neolith.E105")) == sorted(
        [
            f"joined/contracts.py:{joined}: "
            "neolith.E105 app joined: module joined.contracts defines Joined, Ping of "
            "the contract package and also holds the implementation "
            "joined.contracts.Service, so it is not part of the contract package; "
            f"{_LEFT_OUT_HINT}",
            # The first line of its models, where inspect finds the class,
            # not a class of the same name elsewhere.
            f"mm/records.py:{mm}: "
            "neolith.E105 app mm: module mm.records defines Ping of the contract "
            "package and also defines the models mm.Other, mm.Thing, so it is not "
            f"part of the contract package; {_LEFT_OUT_HINT}",
            # An implementation in a block at module scope, before a model.
            f"ifs/contracts.py:{ifs}: "
            "neolith.E105 app ifs: module ifs.contracts defines Ifs, Ping of the "
            "contract package and also defines the model ifs.Thing and holds the "
            "implementation ifs.contracts.Service, so it is not part of the "
            f"contract package; {_LEFT_OUT_HINT}",
            f"tr/contracts.py:{line_of(project, 'tr/contracts.py', 'class Service')}: "
            "neolith.E105 app tr: module tr.contracts defines Ping, Tr of the "
            "contract package and also holds the implementation "
            "tr.contracts.Service, so it is not part of the contract package; "
            f"{_LEFT_OUT_HINT}",
            f"pp/api/__init__.py:{pp}: "
            "neolith.E105 app pp: module pp.api, a package above the contract "
            "modules pp.api.contracts, pp.api.records, which Python runs before "
            "them, holds the implementation pp.api.Service, so it is not part of "
            f"the contract package; {_LEFT_OUT_HINT}",
            # A package module above that defines nothing of the contract
            # package names the contract module below it.
            f"pm/api/__init__.py:{pm}: "
            "neolith.E105 app pm: module pm.api, a package above the contract module "
            "pm.api.contracts, which Python runs before it, holds the implementation "
            "pm.api.Service, so it is not part of the contract package; "
            f"{_LEFT_OUT_HINT}",
        ]
    ), "\n".join(found)


def test_a_contract_package_that_stands_alone_gives_nothing(
    packages: tuple[Project, list[str]],
) -> None:
    # The contract package's own modules, another app's contract package,
    # code in no app and the standard library are admitted.
    project, found = packages
    contracts = "pk/api/contracts.py"
    for text in [
        "import json",
        "import nowhere.util",
        "from pk.api.records",
        "from shop.",
    ]:
        line = line_of(project, contracts, text)
        assert not any(f"{contracts}:{line}:" in each for each in found), text
    assert not any(
        each.startswith(("shop/", "outer/inner/contracts.py")) for each in found
    )
    assert _of(found, "neolith.E104", "neolith.E105") == found, "\n".join(found)


# Each block at module scope, with ``class Service`` at each ``@`` (or a
# ``def Service``) on the line marked ``#>``; inside a def or a class,
# nothing.
_BLOCKS = {
    "if": "if a:\n    pass\nelif b:\n    pass\nelse:\n    @\n",
    "try": "try:\n    @\nexcept ImportError:\n    pass\n",
    "except": "try:\n    pass\nexcept ImportError:\n    @\n",
    "try-else": "try:\n    pass\nexcept E:\n    pass\nelse:\n    @\n",
    "finally": "try:\n    pass\nfinally:\n    def Service(): ...  #>\n",
    "try-star": "try:\n    pass\nexcept* E:\n    @\n",
    "with": "with a:\n    async def Service(): ...  #>\n",
    "for": "for x in y:\n    pass\nelse:\n    @\n",
    "while": "while a:\n    @\n",
    "match": "match a:\n    case 1:\n        pass\n    case _:\n        @\n",
    "nested": "if a:\n    with b:\n        for c in d:\n            @\n",
    "in-def": "def f():\n    class Service: ...\n",
    "in-class": "class Holder:\n    class Service: ...\n",
    "other-name": "class Other: ...\nService = Other\nimport Service\n",
}


@pytest.mark.parametrize("form", sorted(_BLOCKS))
def test_an_implementation_is_found_in_every_block_at_module_scope(form: str) -> None:
    text = "import a\n\n" + _BLOCKS[form].replace("@", "class Service: ...  #>")
    expected = [n for n, each in enumerate(text.splitlines(), 1) if each.endswith("#>")]
    assert _defined(ast.parse(text), {"Service"}) == expected


_PERSON = """\
from django.contrib.auth.base_user import AbstractBaseUser
from django.db import models


class Person(AbstractBaseUser):
    name = models.CharField(max_length=10, unique=True)

    USERNAME_FIELD = "name"
"""


def test_a_library_app_s_relation_to_a_hosted_app_is_not_w105(tmp_path: Path) -> None:
    # admin's LogEntry has a foreign key to AUTH_USER_MODEL, here a hosted
    # app's model; admin's code lies in a library directory, so W105 does
    # not read it, nor its models.
    project = Project(tmp_path)
    project.app("shop", models=_PERSON)
    project.settings(
        ["shop"],
        neolith={
            "DATABASES": project.databases("default", "shop"),
            "UNDECLARED_APPS": [
                "django.contrib.contenttypes",
                "django.contrib.auth",
                "django.contrib.admin",
            ],
        },
        below='AUTH_USER_MODEL = "shop.Person"\n',
    )
    assert rules(project) == []


# Corners: a module under a models package that no model names (E102), a
# contract module that imports another app's internals (E101, not E104), an
# undeclared app nested in another (W105 for the innermost), and a
# multi-table child of a model with a generic relation (W105 once).

_PARENT = """\
from django.contrib.contenttypes.fields import GenericRelation
from django.db import models


class Parent(models.Model):
    generic = GenericRelation("billing.Item", related_query_name="+")


class Child(Parent):
    pass
"""

_SUB_APP = """\
from django.apps import AppConfig


class SubConfig(AppConfig):
    name = "legacy.sub"
    label = "sub"
    default_auto_field = "django.db.models.AutoField"
"""

_ITEM = """\
from django.db import models


class Item(models.Model):
    name = models.CharField(max_length=10)
"""


@pytest.fixture(scope="module")
def corners(tmp_path_factory: pytest.TempPathFactory) -> tuple[Project, list[str]]:
    project = Project(tmp_path_factory.mktemp("corners"))
    project.write("billing/records.py", RECORDS)
    project.write("billing/contracts.py", contract("billing.v1", "billing.records"))
    project.write("billing/services.py", service("billing.records"))
    provider(
        project,
        "billing",
        "billing.contracts",
        "Plain",
        "billing.services.Service",
        models=_ITEM,
    )
    project.write("shop/records.py", RECORDS)
    project.write(
        "shop/contracts.py",
        contract("shop.v1", "shop.records")
        + "\n\nfrom billing.services import Service as _Other\n",
    )
    project.write("shop/services.py", service("shop.records"))
    project.write("shop/views.py", "import legacy.models.helpers\n")
    provider(project, "shop", "shop.contracts", "Plain", "shop.services.Service")
    project.write("legacy/__init__.py")
    project.write("legacy/apps.py", _LEGACY_APP)
    project.write("legacy/models/__init__.py", _PARENT)
    project.write("legacy/models/helpers.py", "import json\n")
    project.write("legacy/sub/__init__.py")
    project.write("legacy/sub/apps.py", _SUB_APP)
    project.write("legacy/sub/views.py", "import billing.services\n")
    project.settings(
        ["shop", "billing"],
        neolith={
            "DATABASES": project.databases("default", "shop", "billing"),
            "UNDECLARED_APPS": ["django.contrib.contenttypes", "legacy", "legacy.sub"],
        },
        below='SILENCED_SYSTEM_CHECKS = ["neolith.W004"]\n',
    )
    return project, rules(project)


def test_corners_of_e101_e102_and_w105(corners: tuple[Project, list[str]]) -> None:
    project, found = corners
    contracts = line_of(project, "shop/contracts.py", "from billing.services")
    parent = line_of(project, "legacy/models/__init__.py", "class Parent(")
    assert found == sorted(
        [
            # A contract module that imports another app's internals: E101,
            # and no E104, which is about its own app.
            _e101("shop.contracts", "shop/contracts.py", contracts, "billing.services"),
            # Every module under an undeclared app's models package.
            _e102("shop.views", "shop/views.py", 1, "legacy.models.helpers", "legacy"),
            # The innermost undeclared app holds the module.
            "legacy/sub/views.py:1: neolith.W105 app billing: module "
            "legacy.sub.views of app sub, which has no declaration: imports "
            "billing.services, a module of app billing outside its contract "
            f"package; {_UNDECLARED_HINT}",
            # The multi-table child inherits the relation, reported once.
            f"legacy/models/__init__.py:{parent}: neolith.W105 app billing: model "
            "legacy.Parent of app legacy, which has no declaration: field generic "
            "is a relation to billing.Item, a model of app billing; "
            f"{_UNDECLARED_HINT}",
        ],
        key=lambda line: (line.split(":")[0], int(line.split(":")[1])),
    ), "\n".join(found)
