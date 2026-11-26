"""Chains of imports through first-party code in no hosted app
(``neolith.E103``; spec 05, APP-66; ADR 0030 Decisions 2 and 3).

The shortest chain is checked against a breadth-first search over
generated graphs. The rule itself runs on a project in a child process
(``tests/test_check_imports.py``).
"""

import itertools
import os
import subprocess
import sys
from collections import deque
from collections.abc import Sequence
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from neolith._check_imports import _Chains, _Hit
from tests.checking import Project
from tests.test_check_imports import (
    INTERNAL_HINT,
    MODELS_HINT,
    RECORDS,
    contract,
    line_of,
    provider,
    rules,
    service,
)

pytestmark = pytest.mark.spec("APP-66")

_CHAIN_HINT = (
    "move what this app needs from that code into the app, or take the import "
    "of the other app out of it"
)


# The shortest chain, over generated graphs of modules in no hosted app.


class _Graph:
    """A stand-in for the rules: modules ``m0`` to ``m<n>`` in no hosted
    app, each importing others, and some importing a module the rules
    report.
    """

    def __init__(self, edges: dict[str, list[str]], hits: set[str]) -> None:
        self.edges = edges
        self.hits = hits
        self.read: list[str] = []

    def node(self, name: str) -> list[tuple[str, int]]:
        self.read.append(name)
        found = [(each, 1) for each in self.edges.get(name, [])]
        return [*found, ("other.internal", 2)] if name in self.hits else found

    def hit(self, app: str, name: str) -> _Hit | None:
        return _Hit("internal", name, "other") if name == "other.internal" else None

    def is_node(self, name: str) -> bool:
        return name.startswith("m")

    def runs(self, target: str, importer: str) -> list[str]:
        return [target]


@st.composite
def _graphs(draw: st.DrawFn) -> tuple[dict[str, list[str]], set[str], list[str]]:
    size = draw(st.integers(1, 8))
    names = [f"m{n}" for n in range(size)]
    edges = {
        name: draw(st.lists(st.sampled_from(names), max_size=3, unique=True))
        for name in names
    }
    hits = set(draw(st.lists(st.sampled_from(names), max_size=2)))
    starts = draw(st.lists(st.sampled_from(names), min_size=1, max_size=3, unique=True))
    return edges, hits, starts


def _distance(
    edges: dict[str, list[str]], hits: set[str], starts: Sequence[str]
) -> int | None:
    """The fewest steps from one of ``starts`` to a module in ``hits``."""
    seen = set(starts)
    queue = deque((each, 0) for each in starts)
    while queue:
        name, steps = queue.popleft()
        if name in hits:
            return steps
        for each in edges.get(name, []):
            if each not in seen:
                seen.add(each)
                queue.append((each, steps + 1))
    return None


@settings(max_examples=300, deadline=None)
@given(_graphs())
def test_the_chain_named_is_a_shortest_one(
    graph: tuple[dict[str, list[str]], set[str], list[str]],
) -> None:
    edges, hits, starts = graph
    rules = _Graph(edges, hits)
    chains = _Chains(rules, "app")  # type: ignore[arg-type]
    found = chains.shortest(starts)
    expected = _distance(edges, hits, starts)
    if expected is None:
        assert found is None
        return
    assert found is not None
    through, hit = found
    assert through[0] in starts
    assert all(b in edges[a] for a, b in itertools.pairwise(through))
    assert through[-1] in hits
    assert len(through) - 1 == expected
    assert hit == _Hit("internal", "other.internal", "other")
    # Asked again, from the same starts, the same chain; each module is read
    # once.
    assert chains.shortest(starts) == found
    assert len(rules.read) == len(set(rules.read))


# The rule, on a project.

_HELPERS = {
    # Two ways to billing's internals: the shorter is named.
    "helpers/__init__.py": "",
    "helpers/a.py": "import helpers.long1\nimport helpers.c\n",
    "helpers/long1.py": "import helpers.long2\n",
    "helpers/long2.py": "from billing import services\n",
    "helpers/c.py": "from billing.services import Service\n",
    # A cycle that reaches nothing.
    "helpers/x.py": "import helpers.y\n",
    "helpers/y.py": "import helpers.x\n",
    # Into the importing app itself, whose own import is judged there.
    "helpers/own.py": "import shop.internal\n",
    # Into another app's contract package, which is judged for that app.
    "helpers/public.py": "import billing.contracts\n",
    # To a contrib app's models module.
    "helpers/users.py": "from django.contrib.auth.models import User\n",
    # A file that cannot be compiled ends its chain.
    "helpers/broken.py": "import billing.services\nx = (\n",
    # A package that imports the internals runs before its module.
    "helpers/pkg/__init__.py": "import billing.services\n",
    "helpers/pkg/mod.py": "",
    # Through a module of an undeclared first-party app.
    "legacy/util.py": "import billing.services\n",
}

_SHOP_VIEWS = """\
import helpers.a
import helpers.x
import helpers.own
import helpers.public
import helpers.users
import helpers.broken
import helpers.pkg.mod
import legacy.util
import proj.store.contracts
import vendorlib.bridge
import shop.contracts
"""

_LEGACY_APP = """\
from django.apps import AppConfig


class LegacyConfig(AppConfig):
    name = "legacy"
    default_auto_field = "django.db.models.AutoField"
"""


@pytest.fixture(scope="module")
def chains(tmp_path_factory: pytest.TempPathFactory) -> tuple[Project, list[str]]:
    """Hosted apps ``shop``, ``billing`` and ``proj.store``, with first-party
    code in no app between them, an undeclared first-party app ``legacy``,
    and a library module in the user site directory; and the rules'
    findings.
    """
    base = tmp_path_factory.mktemp("chains")
    user = base / "userbase"
    probe = subprocess.run(
        [sys.executable, "-c", "import site; print(site.getusersitepackages())"],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONUSERBASE": str(user)},
    )
    library = Path(probe.stdout.strip())
    (library / "vendorlib").mkdir(parents=True)
    (library / "vendorlib" / "__init__.py").write_text("")
    (library / "vendorlib" / "bridge.py").write_text("import billing.services\n")
    project = Project(base / "project")
    project.write("billing/records.py", RECORDS)
    project.write("billing/contracts.py", contract("billing.v1", "billing.records"))
    project.write("billing/services.py", service("billing.records"))
    provider(
        project, "billing", "billing.contracts", "Plain", "billing.services.Service"
    )
    project.write("shop/records.py", RECORDS)
    project.write("shop/contracts.py", contract("shop.v1", "shop.records"))
    project.write("shop/services.py", service("shop.records"))
    project.write("shop/internal.py", "import billing.services\n")
    project.write("shop/views.py", _SHOP_VIEWS)
    provider(project, "shop", "shop.contracts", "Plain", "shop.services.Service")
    # A package in no app above a contract module, which imports the app's
    # internals.
    project.write("proj/__init__.py", "from proj.store import services\n")
    project.write("proj/store/records.py", RECORDS)
    project.write("proj/store/contracts.py", contract("store.v1", "proj.store.records"))
    project.write("proj/store/services.py", service("proj.store.records"))
    provider(
        project,
        "proj.store",
        "proj.store.contracts",
        "Plain",
        "proj.store.services.Service",
    )
    # A hosted app inside that package: Python runs it before every module
    # of the app, so the app's imports of its own modules start no chain.
    project.app("proj.side")
    project.write("proj/side/views.py", "import proj.side.helpers\n")
    project.write("proj/side/helpers.py")
    for path, text in _HELPERS.items():
        project.write(path, text)
    project.write("legacy/__init__.py")
    project.write("legacy/apps.py", _LEGACY_APP)
    project.settings(
        ["shop", "billing", "proj.store", "proj.side"],
        neolith={
            "DATABASES": project.databases(
                "default", "shop", "billing", "store", "side"
            ),
            "UNDECLARED_APPS": [
                "django.contrib.contenttypes",
                "django.contrib.auth",
                "legacy",
            ],
        },
        below='SILENCED_SYSTEM_CHECKS = ["neolith.W004"]\n',
    )
    path = os.pathsep.join([str(project.root), str(library), *sys.path])
    return project, rules(
        project, env={"PYTHONUSERBASE": str(user), "PYTHONPATH": path}
    )


def _e103(project: Project, imported: str, through: str, reaches: str) -> str:
    line = line_of(project, "shop/views.py", f"import {imported}")
    return (
        f"shop/views.py:{line}: neolith.E103 app shop: module shop.views: imports "
        f"{imported}; through first-party code in no hosted app ({through}), it "
        f"reaches {reaches}; {_CHAIN_HINT}"
    )


_SERVICES = "billing.services, a module of app billing outside its contract package"


def test_a_chain_through_first_party_code_in_no_hosted_app_is_e103(
    chains: tuple[Project, list[str]],
) -> None:
    project, found = chains
    e103 = [line for line in found if " neolith.E103 " in line]
    assert sorted(e103) == sorted(
        [
            # The shortest of two chains.
            _e103(project, "helpers.a", "helpers.a -> helpers.c", _SERVICES),
            _e103(
                project,
                "helpers.users",
                "helpers.users",
                "django.contrib.auth.models, a models module of app auth, which "
                "has no declaration",
            ),
            # A package above the module imported runs first.
            _e103(project, "helpers.pkg.mod", "helpers.pkg", _SERVICES),
            # A module of an undeclared first-party app is first-party code in
            # no hosted app.
            _e103(project, "legacy.util", "legacy.util", _SERVICES),
            # A package module in no app above a contract module.
            _e103(
                project,
                "proj.store.contracts",
                "proj",
                "proj.store.services, a module of app store outside its contract "
                "package",
            ),
        ]
    ), "\n".join(found)


def test_a_chain_is_not_followed_into_a_hosted_app_or_a_library(
    chains: tuple[Project, list[str]],
) -> None:
    # A cycle that reaches nothing, a chain into the app itself or into
    # another app's contract package, a file that cannot be compiled, and a
    # library module, give no E103; the app's own module and the contract
    # module are judged for their own apps.
    project, found = chains
    for imported in [
        "helpers.x",
        "helpers.own",
        "helpers.public",
        "helpers.broken",
        "vendorlib.bridge",
        "shop.contracts",
    ]:
        line = line_of(project, "shop/views.py", f"import {imported}")
        assert not any(each.startswith(f"shop/views.py:{line}:") for each in found)
    assert (
        "shop/internal.py:1: neolith.E101 app shop: module shop.internal: imports "
        f"billing.services, a module of app billing outside its contract package; "
        f"{INTERNAL_HINT}"
    ) in found
    # The modules of store and side run proj too, as a package above them,
    # before they import anything: no chain.
    assert not any(each.startswith("proj/") for each in found), "\n".join(found)
    assert MODELS_HINT not in "\n".join(found)
