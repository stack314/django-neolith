"""The routing part's host: one process, configured through settings
(ADR 0032 Decision 8; ADR 0031 Decision 9).

``test_failover`` runs ``main`` once, in a process of its own, with nothing
in its ``argv`` but the code that calls it. The process:

1. binds every case's scripted providers on ``127.0.0.1`` and port 0, so no
   port is chosen ahead and none can be taken by another process;
2. writes its one calling app, ``routing_caller``, which requires one small
   test contract per case, to a directory of its own, and generates the
   app's signing secret, so no secret crosses ``argv``;
3. builds its settings in ADR 0031 Decision 2's form: a ``single`` case
   leaves its identity out of ``NEOLITH["ROUTING_POLICIES"]`` and lists one
   ``REMOTE_PROVIDERS`` entry, every other case names ``local-first`` and
   lists two; then sets the host up;
4. runs each case, with its own providers started and closed around it,
   and prints one JSON record per case: its outcome and the requests its
   providers recorded, hidden fields as positions only;
5. prints a last record with what the run would leak: the position of any
   recorded secret, principal or key, or the signing secret, found in a
   printed record or in a line the host logged; and the ``argv`` it was
   given.

Each test contract has two actions, ``get`` (idempotent) and ``put`` (not
idempotent); the contract of ``routing_stays_on_declared_kind`` also
declares the kind ``gone`` on ``get``. The suite's clock, patched at
``_context.now`` and ``_http._now``, reads the machine's clock, apart from
the cases that freeze it: it then starts at a whole number of seconds and
moves only when U moves it, from inside, before it answers (``Clock``).
"""

import contextlib
import faulthandler
import functools
import json
import logging
import math
import secrets
import sys
import tempfile
import textwrap
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from pydantic import BaseModel

from neolith import ContractError, Principal, action, contract
from tests.scripted_providers import Journal, Positions, Provider, started

APP: Final = "routing_caller"
PRINCIPAL: Final = Principal("https://id.example.com", "u-81f3", {"roles": ("clerk",)})
TRACESTATE: Final = "routing=case"
GONE: Final = "the scripted provider says gone"
# The one bound on the whole process, as on every subprocess of the suite
# (ADR 0032 Decision 11): past it, the process prints every thread's stack
# and exits.
PROCESS_BOUND: Final = 120


class Ask(BaseModel):
    n: int


class Answer(BaseModel):
    n: int


class Gone(BaseModel):
    reason: str


@dataclass(frozen=True)
class Case:
    """One row of ADR 0031's Decision 9 table.

    ``entries`` are the scripted providers' letters, in the order listed;
    ``kind`` is E's kind; ``timeout`` the root's, in seconds, ``None`` for
    no deadline; ``calls`` how many calls of ``action`` the case makes; and
    ``move``, for a case that freezes the clock, how far U moves it.
    """

    name: str
    entries: tuple[str, ...]
    action: str = "get"
    kind: str | None = None
    timeout: float | None = 10
    calls: int = 1
    move: float | None = None

    @property
    def identity(self) -> str:
        return f"{self.name}.v1"

    @property
    def single(self) -> bool:
        return len(self.entries) == 1


CASES: Final = (
    Case("routing_single_one_attempt", ("U",)),
    Case("routing_single_refused", ("R",)),
    Case("routing_moves_on_503", ("U", "B"), move=0.005),
    Case("routing_moves_on_refused_connection", ("R", "B")),
    Case("routing_moves_on_unavailable_body", ("N", "B")),
    Case("routing_moves_on_reset_after_read", ("X", "B")),
    Case("routing_put_stays_after_503", ("U", "B"), action="put"),
    Case("routing_put_stays_after_refused_connection", ("R", "B"), action="put"),
    Case("routing_put_stays_after_reset_after_read", ("X", "B"), action="put"),
    Case("routing_stays_on_timeout", ("E", "B"), kind="timeout"),
    Case("routing_stays_on_unauthenticated", ("E", "B"), kind="unauthenticated"),
    Case("routing_stays_on_permission_denied", ("E", "B"), kind="permission_denied"),
    Case("routing_stays_on_conflict", ("E", "B"), kind="conflict"),
    Case("routing_stays_on_internal", ("E", "B"), kind="internal"),
    Case("routing_stays_on_declared_kind", ("E", "B"), kind="gone"),
    Case("routing_invalid_reply_stays", ("V", "B")),
    Case("routing_silent_provider_times_out", ("S", "B"), timeout=0.5),
    # U moves the clock into the deadline's last millisecond: under 1 ms
    # left, and the deadline not passed.
    Case("routing_no_time_left_ends_unavailable", ("U", "B"), move=10 - 0.0005),
    Case("routing_last_entry_unavailable", ("U", "D")),
    Case("routing_moves_with_no_deadline", ("U", "B"), timeout=None),
    Case("routing_each_call_starts_first", ("U", "B"), calls=2),
)


def _contract(case: Case) -> type:
    """The case's test contract."""
    if case.kind == "gone":

        @contract(case.identity)
        class Declaring(Protocol):
            @action(idempotent=True, errors={"gone": Gone})
            def get(self, request: Ask) -> Answer: ...

            @action
            def put(self, request: Ask) -> Answer: ...

        return Declaring

    @contract(case.identity)
    class Plain(Protocol):
        @action(idempotent=True)
        def get(self, request: Ask) -> Answer: ...

        @action
        def put(self, request: Ask) -> Answer: ...

    return Plain


CONTRACTS: Final = tuple(_contract(case) for case in CASES)

_APPS = """\
from neolith import NeolithAppConfig
from tests.conformance.routing.child import CONTRACTS


class RoutingCallerConfig(NeolithAppConfig):
    \"\"\"Calls every routing case's test contract.\"\"\"

    name = "routing_caller"
    label = "routing_caller"
    requires = CONTRACTS
"""


class Clock:
    """The suite's clock: the machine's, or frozen and moved by hand.

    Called, it is the clock the call reads: its deadline, its budget, the
    connector's checks (``_context.now``). ``waits`` is the clock the HTTP
    client's own socket waits read (``_http._now``): while frozen it stays
    at the start, and U's moves do not reach it. So a move into the
    deadline's last millisecond cannot shrink the client's wait for U's
    answer to half a millisecond of real time, which a slow thread would
    lose to a ``timeout``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._start: float | None = None
        self._at: float | None = None

    def __call__(self) -> float:
        with self._lock:
            return time.monotonic() if self._at is None else self._at

    def waits(self) -> float:
        with self._lock:
            return time.monotonic() if self._start is None else self._start

    @contextlib.contextmanager
    def frozen(self) -> Iterator[None]:
        """Stand still at a whole number of seconds while entered: budgets
        are read in binary floating point, and from a whole number a move of
        5 ms takes exactly 5 off.
        """
        with self._lock:
            self._start = self._at = float(math.ceil(time.monotonic()))
        try:
            yield
        finally:
            with self._lock:
                self._start = self._at = None

    def move(self, seconds: float) -> None:
        with self._lock:
            assert self._at is not None, "the clock moves only while frozen"
            self._at += seconds


@dataclass
class Bound:
    """A case with its providers bound, and the settings entries for them."""

    case: Case
    journal: Journal
    providers: tuple[Provider, ...]


def _bind(case: Case, positions: Positions, clock: Clock) -> Bound:
    journal = Journal(positions)
    providers = []
    for letter in case.entries:
        before: Callable[[], None] | None = None
        if letter == "U" and case.move is not None:
            before = _mover(clock, case.move)
        kind = case.kind if letter == "E" else None
        detail = {"reason": GONE} if kind == "gone" else None
        providers.append(
            Provider(letter, journal, kind=kind, detail=detail, before=before)
        )
    return Bound(case, journal, tuple(providers))


def _mover(clock: Clock, seconds: float) -> Callable[[], None]:
    def move() -> None:
        clock.move(seconds)

    return move


def _entry(provider: Provider) -> dict[str, object]:
    return {
        "BACKEND": "neolith.transports.HTTPTransport",
        "OPTIONS": {"url": provider.url},
    }


def _deployer(bound: list[Bound], secret: str) -> dict[str, object]:
    return {
        "SECRET_KEY": secrets.token_urlsafe(50),
        "USE_TZ": True,
        "MIDDLEWARE": [
            "neolith.middleware.RootMiddleware",
            "neolith.middleware.PrincipalMiddleware",
        ],
        "NEOLITH": {
            "ROUTING_POLICIES": {
                b.case.identity: "local-first" for b in bound if not b.case.single
            },
            "REMOTE_PROVIDERS": {
                b.case.identity: [_entry(p) for p in b.providers] for b in bound
            },
            "SIGNING_SECRETS": {APP: {"KEY_ID": f"{APP}-1", "SECRET": secret}},
        },
    }


@contextlib.contextmanager
def _root(timeout: float | None) -> Iterator[None]:
    """A root of ``routing_caller`` holding a principal and a ``tracestate``.

    A command root starts a trace with no ``tracestate``, and a request root
    takes its principal from a principal backend; this root holds both, as
    a request root that carried them would, so TRN-71 is seen on every
    field it names.
    """
    from neolith import _context

    fields = _context.root_fields(
        APP,
        requires=tuple(case.identity for case in CASES),
        principal=PRINCIPAL,
        timeout=timeout,
    )
    fields["tracestate"] = TRACESTATE
    with _context.opened(**fields):
        yield


def _outcome(call: Callable[[], object]) -> dict[str, object]:
    try:
        reply = call()
    except ContractError as error:
        data = error.data
        return {
            "kind": error.kind,
            "message": error.message,
            "detail": data.model_dump() if isinstance(data, BaseModel) else data,
        }
    assert isinstance(reply, BaseModel), reply
    return {"reply": reply.model_dump(), "type": type(reply).__name__}


def _run(bound: Bound, clock: Clock) -> dict[str, object]:
    import neolith

    case = bound.case
    frozen = clock.frozen() if case.move is not None else contextlib.nullcontext()
    outcomes = []
    with started(*bound.providers), frozen:
        for _ in range(case.calls):
            with _root(case.timeout):
                method = getattr(neolith.connect(case.identity), case.action)
                outcomes.append(_outcome(functools.partial(method, Ask(n=1))))
    providers = {p.name: p.url for p in bound.providers}
    return {
        "name": case.name,
        "outcomes": outcomes,
        "requests": [r.as_json() for r in bound.journal.requests],
        "providers": providers,
        "errors": bound.journal.errors,
    }


class _Kept(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


# Every line the host logs, at every level, kept for the leak check and
# never printed.
_KEPT: Final = _Kept()


def main() -> None:
    """Run every case and print one JSON record per case, then the leaks."""
    if len(sys.argv) > 1:
        raise SystemExit("child: takes no arguments")
    faulthandler.dump_traceback_later(PROCESS_BOUND, exit=True)
    positions = Positions()
    clock = Clock()
    bound = [_bind(case, positions, clock) for case in CASES]
    secret = secrets.token_urlsafe(32)
    with contextlib.ExitStack() as stack:
        # Every provider closes, whatever ends the run: R, whose socket is
        # held for the process's life, here; the others at the end of their
        # case too, and closing twice is harmless.
        for b in bound:
            for provider in b.providers:
                stack.callback(provider.close)
        records = _host(bound, secret, clock)
    lines = [json.dumps(record, sort_keys=True) for record in records]
    # The records as text, unescaped too, so a value outside ASCII is found.
    plain = [json.dumps(record, ensure_ascii=False) for record in records]
    said = "\n".join([*lines, *plain, *_KEPT.lines])
    leaks = positions.found_in(said)
    if secret in said:
        leaks.append("the signing secret")
    for line in lines:
        print(line)
    print(json.dumps({"leaks": leaks, "logged": len(_KEPT.lines), "argv": sys.argv}))


def _host(bound: list[Bound], secret: str, clock: Clock) -> list[dict[str, object]]:
    """Set the host up on the cases' settings, and run every case."""
    with tempfile.TemporaryDirectory(prefix="routing-caller-") as place:
        package = Path(place, APP)
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "apps.py").write_text(textwrap.dedent(_APPS))
        sys.path.insert(0, place)

        import django
        from django.conf import settings

        from neolith import _context, _http
        from neolith.host import build_settings

        settings.configure(**build_settings([APP], _deployer(bound, secret)))
        logging.getLogger().addHandler(_KEPT)
        logging.getLogger().setLevel(logging.DEBUG)
        django.setup()
        _context.now = clock
        _http._now = clock.waits
        return [_run(b, clock) for b in bound]
