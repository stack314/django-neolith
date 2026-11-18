"""The host's checks of its settings before it serves (spec 05, Before Serve).

``NEOLITH`` and ``MIDDLEWARE`` each have one admitted form, judged by a
plain function, which set-up runs as a system check (``neolith.E001``,
``neolith.E006``) and the root middleware's constructor runs on the list its
handler is built from (ADR 0022 Decisions 3, 5 and 6). Set-up raises every
error in one ``ImproperlyConfigured``. ``neolith.W001`` is in
``test_request_admin.py``, since it needs Django's admin.
"""

import contextlib
import decimal
import math
from collections import OrderedDict, UserList
from collections.abc import Iterator
from datetime import timedelta

import pytest
from django.conf import settings
from django.core import checks
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from hypothesis import given
from hypothesis import strategies as st

from neolith import _checks
from neolith._checks import middleware_problems, neolith_problems, request_deadline
from tests import host_apps
from tests.hosting import NEOLITH_KEYS

ROOT = "neolith.middleware.RootMiddleware"
PRINCIPAL = "neolith.middleware.PrincipalMiddleware"
OTHER = "django.middleware.common.CommonMiddleware"
DEADLINE = 'NEOLITH["REQUEST_DEADLINE"]'
RANGE = "is outside 0.001 to 9999999.999 seconds"
KEYS = NEOLITH_KEYS


class MyStr(str):
    __slots__ = ()


class MyList(list[str]):
    pass


@pytest.mark.spec("APP-16")
@pytest.mark.parametrize(
    ("value", "problems"),
    [
        ({}, []),
        ({"REQUEST_DEADLINE": None}, []),
        ({"REQUEST_DEADLINE": 30}, []),
        ({"REQUEST_DEADLINE": 1.5}, []),
        ({"REQUEST_DEADLINE": 0.001}, []),
        ({"REQUEST_DEADLINE": 9999999.999}, []),
        ({"REQUEST_DEADLINE": 9999999}, []),
        ([], ["NEOLITH: expected a dict, got list"]),
        (OrderedDict(), ["NEOLITH: expected a dict, got OrderedDict"]),
        (None, ["NEOLITH: expected a dict, got NoneType"]),
        ({"REQUEST_DEADLIN": 30}, [f"NEOLITH: unknown key 'REQUEST_DEADLIN'; {KEYS}"]),
        ({"DATABASES": {}}, ['NEOLITH["DATABASES"]: is empty; leave the key out']),
        (
            {"PRINCIPAL_BACKENDS": []},
            [
                'NEOLITH["PRINCIPAL_BACKENDS"]: is empty; leave the key out for no '
                "backend"
            ],
        ),
        ({1: 30}, [f"NEOLITH: unknown key of type int; {KEYS}"]),
        (
            {MyStr("REQUEST_DEADLINE"): 30},
            [f"NEOLITH: unknown key of type MyStr; {KEYS}"],
        ),
        (
            {"REQUEST_DEADLINE": True},
            [f"{DEADLINE}: expected a number of seconds or None, got bool"],
        ),
        (
            {"REQUEST_DEADLINE": "30"},
            [f"{DEADLINE}: expected a number of seconds or None, got str"],
        ),
        (
            {"REQUEST_DEADLINE": decimal.Decimal(30)},
            [f"{DEADLINE}: expected a number of seconds or None, got Decimal"],
        ),
        (
            {"REQUEST_DEADLINE": timedelta(seconds=30)},
            [f"{DEADLINE}: expected a number of seconds or None, got timedelta"],
        ),
        ({"REQUEST_DEADLINE": math.nan}, [f"{DEADLINE}: nan is not finite"]),
        ({"REQUEST_DEADLINE": math.inf}, [f"{DEADLINE}: inf is not finite"]),
        ({"REQUEST_DEADLINE": -math.inf}, [f"{DEADLINE}: -inf is not finite"]),
        ({"REQUEST_DEADLINE": 0}, [f"{DEADLINE}: 0 {RANGE}"]),
        ({"REQUEST_DEADLINE": 0.0009}, [f"{DEADLINE}: 0.0009 {RANGE}"]),
        ({"REQUEST_DEADLINE": -1}, [f"{DEADLINE}: -1 {RANGE}"]),
        ({"REQUEST_DEADLINE": 10000000}, [f"{DEADLINE}: 10000000 {RANGE}"]),
        (
            {"REQUEST_DEADLINE": 10**400},
            [f"{DEADLINE}: an int of 1329 bits {RANGE}"],
        ),
        (
            {"OTHER": 1, "REQUEST_DEADLINE": "a"},
            [
                f"NEOLITH: unknown key 'OTHER'; {KEYS}",
                f"{DEADLINE}: expected a number of seconds or None, got str",
            ],
        ),
    ],
)
def test_neolith_has_one_form(value: object, problems: list[str]) -> None:
    assert neolith_problems(value) == problems


@pytest.mark.spec("APP-16")
def test_neolith_is_required() -> None:
    assert neolith_problems() == [
        "settings: NEOLITH is required; it is {} when nothing is set"
    ]


@pytest.mark.spec("APP-16", "TRN-59")
@given(
    st.one_of(
        st.floats(allow_nan=True, allow_infinity=True),
        st.integers(),
        st.floats(min_value=0.0005, max_value=0.002),
        st.floats(min_value=9999999.99, max_value=10000000.01),
    )
)
def test_a_deadline_is_admitted_exactly_within_its_bounds(value: float) -> None:
    """Any finite number from 1 ms to 9999999999 ms is admitted and read as
    that many seconds; every other number is refused by name."""
    admitted = math.isfinite(value) and 0.001 <= value <= 9999999.999
    found = neolith_problems({"REQUEST_DEADLINE": value})
    assert (found == []) is admitted
    if admitted:
        assert request_deadline({"REQUEST_DEADLINE": value}) == float(value)
    else:
        assert len(found) == 1
        assert found[0].startswith(f"{DEADLINE}: ")


@pytest.mark.spec("TRN-59")
def test_the_deadline_read_from_neolith() -> None:
    assert request_deadline({}) == 30.0
    assert request_deadline({"REQUEST_DEADLINE": None}) is None
    assert request_deadline({"REQUEST_DEADLINE": 2}) == 2.0


@pytest.mark.spec("APP-16")
@pytest.mark.parametrize(
    ("value", "problems"),
    [
        ([ROOT], []),
        ([ROOT, OTHER], []),
        ([], [f"MIDDLEWARE: {ROOT} must be the first entry"]),
        ([OTHER], [f"MIDDLEWARE: {ROOT} must be the first entry"]),
        ([OTHER, ROOT], [f"MIDDLEWARE: {ROOT} must be the first entry"]),
        ([f"{ROOT} "], [f"MIDDLEWARE: {ROOT} must be the first entry"]),
        ([ROOT, ROOT], [f"MIDDLEWARE: {ROOT} is listed 2 times"]),
        ([ROOT, OTHER, ROOT, ROOT], [f"MIDDLEWARE: {ROOT} is listed 3 times"]),
        (
            [OTHER, ROOT, ROOT],
            [
                f"MIDDLEWARE: {ROOT} must be the first entry",
                f"MIDDLEWARE: {ROOT} is listed 2 times",
            ],
        ),
        ((ROOT,), ["MIDDLEWARE: expected a list, got tuple"]),
        (UserList([ROOT]), ["MIDDLEWARE: expected a list, got UserList"]),
        (MyList([ROOT]), ["MIDDLEWARE: expected a list, got MyList"]),
        (ROOT, ["MIDDLEWARE: expected a list, got str"]),
        (None, ["MIDDLEWARE: expected a list, got NoneType"]),
        ([ROOT, 1], ["MIDDLEWARE: entry 1: expected a str, got int"]),
        # Judged by class: another path, or a subclass, is the same class.
        ([ROOT, "tests.request_site.Alias"], [f"MIDDLEWARE: {ROOT} is listed 2 times"]),
        (
            [ROOT, "tests.request_site.SubRoot"],
            [f"MIDDLEWARE: {ROOT} is listed 2 times"],
        ),
        (
            [ROOT, "tests.request_site.Alias", ROOT],
            [f"MIDDLEWARE: {ROOT} is listed 3 times"],
        ),
        (
            ["tests.request_site.Alias"],
            [f"MIDDLEWARE: {ROOT} must be the first entry"],
        ),
        # Django reports an entry that does not import when it builds the
        # handler.
        ([ROOT, "tests.no_such_module.Middleware"], []),
        ([ROOT, "tests.request_site.NoSuchName"], []),
        ([ROOT, "tests.broken_middleware.Middleware"], []),
        (
            [MyStr(ROOT)],
            [
                "MIDDLEWARE: entry 0: expected a str, got MyStr",
                f"MIDDLEWARE: {ROOT} must be the first entry",
            ],
        ),
    ],
)
def test_middleware_has_the_root_first_once(value: object, problems: list[str]) -> None:
    """The root middleware's rules, with the principal middleware listed
    once in every list (its own rules are in ``test_principal_backends``)."""
    if type(value) is list:
        value = [*value, PRINCIPAL]
    assert middleware_problems(value) == problems


@pytest.fixture
def registered() -> None:
    """The checks are registered by the first set-up in the process."""
    _checks.register()


@contextlib.contextmanager
def _in_place(**changed: object) -> Iterator[None]:
    """Settings changed in place, with no ``setting_changed``, which an
    override of ``NEOLITH`` after set-up would refuse; restored on exit."""
    with override_settings():
        for name, value in changed.items():
            setattr(settings, name, value)
        yield


@pytest.mark.spec("APP-16")
def test_set_up_raises_every_error_at_once(registered: None) -> None:
    """Django loads an override's apps before it applies the override's
    settings, so the settings to judge are changed first."""
    with (
        _in_place(NEOLITH={"OTHER": 1}, MIDDLEWARE=[OTHER, ROOT, PRINCIPAL]),
        pytest.raises(ImproperlyConfigured) as raised,
        override_settings(INSTALLED_APPS=host_apps.INSTALLED),
    ):
        pass
    assert str(raised.value) == (
        "host: the settings are refused before the host serves:\n"
        f"(neolith.E001) NEOLITH: unknown key 'OTHER'; {KEYS}\n"
        f"(neolith.E006) MIDDLEWARE: {ROOT} must be the first entry"
    )


@pytest.mark.spec("APP-16")
def test_set_up_judges_middleware_by_class(registered: None) -> None:
    """Set-up runs the constructor's one rule: an alias of the root
    middleware stops it."""
    with (
        override_settings(
            MIDDLEWARE=[*host_apps.MIDDLEWARE, "tests.request_site.Alias"]
        ),
        pytest.raises(ImproperlyConfigured, match="is listed 2 times"),
        override_settings(INSTALLED_APPS=host_apps.INSTALLED),
    ):
        pass


@pytest.mark.spec("APP-16")
@pytest.mark.parametrize(
    "settings",
    [
        {"NEOLITH": {"REQUEST_DEADLINE": 0}},
        {"MIDDLEWARE": []},
    ],
)
def test_a_silenced_error_still_stops_set_up(
    registered: None, settings: dict[str, object]
) -> None:
    """Silencing hides a message from ``manage.py check``; a host that
    serves runs no command, so set-up raises it anyway."""
    silenced = ["neolith.E001", "neolith.E006"]
    with (
        _in_place(SILENCED_SYSTEM_CHECKS=silenced, **settings),
        pytest.raises(ImproperlyConfigured, match=r"\(neolith\.E00[16]\)"),
        override_settings(INSTALLED_APPS=host_apps.INSTALLED),
    ):
        pass


@pytest.mark.spec("APP-16")
def test_set_up_with_neolith_missing_is_refused(registered: None) -> None:
    with override_settings():
        from django.conf import settings

        del settings.NEOLITH
        with (
            pytest.raises(ImproperlyConfigured, match="NEOLITH is required"),
            override_settings(INSTALLED_APPS=host_apps.INSTALLED),
        ):
            pass


def test_the_checks_judge_only_a_process_that_hosts(registered: None) -> None:
    """Registered once, the checks stay registered; in a registry without
    neolith's app they find nothing to judge."""
    with override_settings(INSTALLED_APPS=[]), _in_place(MIDDLEWARE=[], NEOLITH=[]):
        assert checks.run_checks(tags=["neolith"]) == []


@pytest.mark.parametrize("silenced", [False, True])
def test_set_up_logs_a_warning_unless_it_is_silenced(
    silenced: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """``manage.py check`` drops a silenced message (ADR 0022 fact 1), and
    so does set-up's log; a warning never stops set-up."""
    from neolith import _host

    warning = checks.Warning("a warning", id="neolith.W001")
    names = ["neolith.W001"] if silenced else []
    with override_settings(SILENCED_SYSTEM_CHECKS=names):
        assert _host._judge([warning]) == 30.0
    logged = [r.getMessage() for r in caplog.records if r.name == "neolith.host"]
    assert logged == ([] if silenced else ["(neolith.W001) a warning"])
