"""A request root's principal: the backends, the search and ``Vary`` (ADR 0023).

The deployer lists backends in ``NEOLITH["PRINCIPAL_BACKENDS"]``. These are
the plain functions of Decision 2, which take what they work on as
arguments and hold no state of the host:

- ``shape_problems`` judges the setting's shape and imports nothing; the
  builder and set-up (``neolith.E001``) run it;
- ``build`` imports and constructs each backend of a checked setting, once
  per set-up, and returns every refusal (``neolith.E019``);
- ``search`` and ``asearch`` ask a list of built backends, in order, about
  one request (Decision 5);
- ``vary_of`` is the union of the backends' request inputs (Decision 6), and
  ``cache_problems`` the rule for Django's cache middleware
  (``neolith.E020``).

``Resolution`` is a request root's one outcome: the principal's sent form,
none, or a failure, written once behind its own lock, which is never held
across a search. The principal middleware gives a bound root its
``Resolution`` and runs the response phase; ``neolith.middleware`` holds
that wiring.
"""

import asyncio
import contextlib
import contextvars
import inspect
import keyword
import logging
import re
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Final, final

from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest
from django.utils.module_loading import import_string

from neolith import _naming
from neolith._context import Principal, principal_form
from neolith._frozen import once
from neolith.principals import PrincipalBackend

__all__ = [
    "KEY",
    "PRINCIPAL_MIDDLEWARE",
    "Built",
    "Failed",
    "Resolution",
    "asearch",
    "build",
    "cache_problems",
    "search",
    "shape_problems",
    "vary_of",
]

_log = logging.getLogger("neolith.host")

KEY: Final = "PRINCIPAL_BACKENDS"
SETTING: Final = f'NEOLITH["{KEY}"]'
PRINCIPAL_MIDDLEWARE: Final = "neolith.middleware.PrincipalMiddleware"
_BASE: Final = "neolith.principals.PrincipalBackend"
_ENTRY_KEYS: Final = frozenset({"BACKEND", "OPTIONS"})
# A request header name in lower case: "segment" of spec 01 with "-" for "_".
_HEADER: Final = re.compile(r"[a-z][a-z0-9-]*")
_ANY: Final = ("*",)
_SHOWN: Final = 120
_CACHE: Final = "django.middleware.cache.UpdateCacheMiddleware"


def _type(value: object) -> str:
    return _naming.type_name(value)


def _shown(value: object) -> str:
    """A value for a message: ``repr`` of a ``str``, bounded, or its type."""
    if type(value) is not str:
        return f"a {_type(value)}"
    text = repr(value)
    return text if len(text) <= _SHOWN else f"{text[: _SHOWN - 3]}..."


def _cause(error: BaseException, *, bounded: bool = True) -> str:
    """An error's class and message, bounded unless ``bounded`` is false,
    never raising.
    """
    try:
        text = f"{_type(error)}: {error}"
    except Exception:
        text = _type(error)
    if not bounded or len(text) <= 2 * _SHOWN:
        return text
    return f"{text[: 2 * _SHOWN - 3]}..."


def _dotted(path: str) -> bool:
    """Whether ``path`` is two or more ASCII identifiers joined by dots."""
    parts = path.split(".")
    return len(parts) >= 2 and all(
        part.isascii() and part.isidentifier() and not keyword.iskeyword(part)
        for part in parts
    )


def shape_problems(value: object) -> list[str]:
    """Judge the shape of ``NEOLITH["PRINCIPAL_BACKENDS"]``, importing nothing.

    The value is a ``list`` of one or more entries, each a ``dict`` with
    exactly the keys ``BACKEND``, a dotted path, and ``OPTIONS``, a ``dict``
    whose keys are identifiers (ADR 0023, the admitted set). Every problem
    found is returned.
    """
    if type(value) is not list:
        return [f"{SETTING}: expected a list of backends, got {_type(value)}"]
    entries: list[object] = value
    if not entries:
        return [f"{SETTING}: is empty; leave the key out for no backend"]
    problems: list[str] = []
    for position, entry in enumerate(entries):
        where = f"{SETTING}[{position}]"
        if type(entry) is not dict:
            problems.append(
                f"{where}: expected a dict with exactly the keys BACKEND and "
                f"OPTIONS, got {_type(entry)}"
            )
            continue
        # Items are read, never looked up, so no key's own hooks run.
        items: dict[object, object] = entry
        keys = list(items)
        if len(keys) != len(_ENTRY_KEYS) or any(
            type(k) is not str or k not in _ENTRY_KEYS for k in keys
        ):
            found = ", ".join(sorted(_shown(k) for k in keys)) or "none"
            problems.append(
                f"{where}: expected exactly the keys BACKEND and OPTIONS, got {found}"
            )
            continue
        backend, options = items["BACKEND"], items["OPTIONS"]
        if type(backend) is not str or not _dotted(backend):
            problems.append(
                f"{where}: BACKEND {_shown(backend)} is not a dotted path, such as "
                "'neolith.principals.DjangoUserBackend'"
            )
        if type(options) is not dict:
            problems.append(f"{where}: OPTIONS: expected a dict, got {_type(options)}")
            continue
        given: dict[object, object] = options
        problems.extend(
            f"{where}: OPTIONS: the key {_shown(name)} is not an identifier"
            for name in given
            if type(name) is not str or not name.isidentifier()
        )
    return problems


@final
@once
@dataclass(frozen=True, slots=True)
class Built:
    """One backend that set-up built: its place, its path and its inputs."""

    position: int
    path: str
    backend: PrincipalBackend
    inputs: tuple[str, ...]

    @property
    def where(self) -> str:
        """How refusals and logs name the entry: its position and path."""
        return f"{SETTING}[{self.position}] ({self.path})"


def _inputs_problems(value: object) -> list[str]:
    """Why a backend's ``request_inputs`` is refused: one line for each
    name that is not a header name, and one for the names given more than
    once, so no refusal hides another. Empty when it is admitted.
    """
    if type(value) is not tuple:
        return [f"request_inputs is a {_type(value)}, not a tuple of header names"]
    names: tuple[object, ...] = value
    if names == _ANY and type(names[0]) is str:
        return []
    problems: list[str] = []
    for name in names:
        if type(name) is not str or not _HEADER.fullmatch(name):
            extra = " ('*' stands alone)" if name == "*" else ""
            problem = (
                f"request_inputs holds {_shown(name)}, which is not a header name "
                f"in lower case{extra}; the form is ('authorization', 'cookie'), "
                "() or ('*',)"
            )
            if problem not in problems:
                problems.append(problem)
    seen: set[str] = set()
    twice: list[str] = []
    for name in names:
        if type(name) is str:
            if name in seen and name not in twice:
                twice.append(name)
            seen.add(name)
    if twice:
        shown = ", ".join(_shown(name) for name in twice)
        problems.append(f"request_inputs names a header twice: {shown}")
    return problems


def build(value: list[dict[str, Any]]) -> tuple[tuple[Built, ...], list[str]]:
    """Import and construct every backend of a checked setting, in order.

    Returns the backends built and every refusal (ADR 0023 Decision 10),
    each naming the entry's position and path. A backend is built only when
    its class passes every check; one entry's refusal stops no other entry.
    """
    built: list[Built] = []
    problems: list[str] = []
    for position, entry in enumerate(value):
        path: str = entry["BACKEND"]
        options: dict[str, object] = entry["OPTIONS"]
        where = f"{SETTING}[{position}] ({path})"
        try:
            found = import_string(path)
        except Exception as error:
            problems.append(f"{where}: does not import: {_cause(error)}")
            continue
        if not issubclass(type(found), type):
            problems.append(
                f"{where}: is a {_type(found)}, not a class; a backend is a "
                f"subclass of {_BASE}"
            )
            continue
        if found is PrincipalBackend or not issubclass(found, PrincipalBackend):
            problems.append(
                f"{where}: is not a subclass of {_BASE}; a backend subclasses it"
            )
            continue
        own: list[str] = []
        if inspect.isabstract(found):
            missing = ", ".join(sorted(getattr(found, "__abstractmethods__", ())))
            own.append(f"{where}: is abstract; it does not define {missing}")
        if inspect.iscoroutinefunction(getattr(found, "resolve", None)):
            own.append(f"{where}: resolve is a coroutine function; it is a plain one")
        if not inspect.iscoroutinefunction(getattr(found, "aresolve", None)):
            own.append(f"{where}: aresolve is not a coroutine function")
        if own:
            problems.extend(own)
            continue
        try:
            backend = found(**options)
        except Exception as error:
            names = ", ".join(sorted(options)) or "none"
            # A backend's own refusal lists every problem it found (ADR 0023
            # Decision 10), so it is kept whole; any other cause is bounded.
            cause = _cause(error, bounded=not isinstance(error, ImproperlyConfigured))
            problems.append(
                f"{where}: the constructor raised, with the options {names}: {cause}"
            )
            continue
        try:
            inputs = getattr(backend, "request_inputs")  # noqa: B009
        except AttributeError:
            problems.append(
                f"{where}: has no request_inputs; the attribute has no default, "
                "so a backend declares the request headers it reads"
            )
            continue
        except Exception as error:
            problems.append(f"{where}: request_inputs raised: {_cause(error)}")
            continue
        refused = _inputs_problems(inputs)
        if refused:
            problems += [f"{where}: {problem}" for problem in refused]
            continue
        built.append(Built(position, path, backend, tuple(str(n) for n in inputs)))
    return tuple(built), problems


def vary_of(backends: Sequence[Built]) -> tuple[str, ...]:
    """The union of the backends' request inputs, in order: ``("*",)`` when
    one of them reads beyond the URL and the headers.
    """
    names: dict[str, None] = {}
    for built in backends:
        if built.inputs == _ANY:
            return _ANY
        names.update(dict.fromkeys(built.inputs))
    return tuple(names)


def _classes(entry: object) -> type | None:
    """The class a ``MIDDLEWARE`` entry imports as, or ``None``."""
    if type(entry) is not str:
        return None
    try:
        found = import_string(entry)
    except Exception:  # Django reports it when it builds the handler
        return None
    return found if issubclass(type(found), type) else None


def first_index(middleware: Sequence[object], base: type) -> int | None:
    """The position of the first entry that is ``base`` or a subclass."""
    for index, entry in enumerate(middleware):
        found = _classes(entry)
        if found is not None and issubclass(found, base):
            return index
    return None


def cache_problems(middleware: object) -> list[str]:
    """With a backend configured: Django's ``UpdateCacheMiddleware``, or a
    subclass, is not listed after the principal middleware, so the cache
    stores a response only once ``Vary`` is patched (ADR 0023 Decision 6).
    """
    from django.middleware.cache import UpdateCacheMiddleware

    from neolith.middleware import PrincipalMiddleware

    if type(middleware) is not list:
        return []
    entries: list[object] = middleware
    principal = first_index(entries, PrincipalMiddleware)
    if principal is None:
        return []
    return [
        f"MIDDLEWARE: entry {index} ({_shown(entry)}) is {_CACHE} or a subclass, "
        f"listed after {PRINCIPAL_MIDDLEWARE}; list it before, so the cache "
        "stores a response after its Vary names the principal's request inputs"
        for index, entry in enumerate(entries)
        if index > principal
        and (found := _classes(entry)) is not None
        and issubclass(found, UpdateCacheMiddleware)
    ]


class Failed(Exception):
    """A failure to resolve a request's principal; its message is logged."""


def _asked(built: Built, method: str, found: object) -> Principal | None:
    """What ``method`` of a backend answered, or ``Failed``."""
    if found is None:
        return None
    if type(found) is Principal:
        return found
    if inspect.iscoroutine(found):
        found.close()  # never awaited; closed so it does not warn
    raise Failed(
        f"{built.where}: {method} returned a {_type(found)}, not a "
        "neolith.Principal or None"
    )


def _never() -> bool:
    return False


def search(
    backends: Sequence[Built],
    request: HttpRequest,
    settled: Callable[[], bool] = _never,
) -> Principal | None:
    """Ask each backend's ``resolve``, in order (ADR 0023 Decision 5).

    The first principal decides, ``None`` abstains, and when every backend
    abstains there is no principal. A backend that raises an ``Exception``,
    or returns anything else, ends the search with ``Failed``; any other
    ``BaseException`` goes on unchanged. Once ``settled`` is true after a
    backend answers, the outcome is fixed elsewhere and no later backend is
    asked.
    """
    for built in backends:
        try:
            found = built.backend.resolve(request)
        except Exception as error:
            raise Failed(f"{built.where}: resolve raised {_cause(error)}") from error
        principal = _asked(built, "resolve", found)
        if principal is not None or settled():
            return principal
    return None


async def asearch(
    backends: Sequence[Built],
    request: HttpRequest,
    settled: Callable[[], bool] = _never,
) -> Principal | None:
    """``search`` with each backend's ``aresolve``, awaited."""
    for built in backends:
        try:
            found = await built.backend.aresolve(request)
        except Exception as error:
            raise Failed(f"{built.where}: aresolve raised {_cause(error)}") from error
        principal = _asked(built, "aresolve", found)
        if principal is not None or settled():
            return principal
    return None


type _Outcome = tuple[bytes | None, str | None]

_FAILED: Final = (
    "the request's principal could not be resolved; the host logged the cause "
    "under trace {}"
)
_LATE: Final = (
    "a call came after the principal middleware's response phase while no "
    "search had resolved the request's principal, so it is not resolved for "
    "it; code that calls from a "
    "middleware outside the principal middleware opens its own scope with "
    "neolith.command_scope"
)
_REENTERED: Final = (
    "a call was made from the request root while its principal was being "
    "resolved, from a principal backend or code it runs; a backend that calls "
    "opens its own scope with neolith.command_scope"
)

# The resolutions whose search runs in this context. A call from a root
# whose search is running here comes from inside that search, from a backend
# or code it runs: a thread or task the backend starts copies the context.
_SEARCHING: contextvars.ContextVar[tuple["Resolution", ...]] = contextvars.ContextVar(
    "neolith_principal_searching", default=()
)


@contextlib.contextmanager
def _searching(resolution: "Resolution") -> Iterator[None]:
    token = _SEARCHING.set((*_SEARCHING.get(), resolution))
    try:
        yield
    finally:
        _SEARCHING.reset(token)


@final
class Resolution:
    """A request root's principal: one outcome, written once (ADR 0023
    Decision 5).

    The outcome is the principal's sent form, or ``None`` for no principal,
    or a failure. It is written by the first search to complete, under the
    lock, which is held only to read and write the outcome and the marks,
    never across a search. No caller ever waits for another caller's search
    but an async caller on the loop where one is in flight.

    Args:
        backends: The host's built backends, in order.
        vary: Their union of request inputs, which the response phase adds.
        request: The request the backends are given; the root gets a
            resolution only once it is bound to an app the host holds.
        trace_id: The root's trace, which the log names.
    """

    __slots__ = (
        "_lock",
        "_marked",
        "_outcome",
        "_running",
        "_started",
        "backends",
        "request",
        "trace_id",
        "vary",
    )

    def __init__(
        self,
        backends: tuple[Built, ...],
        vary: tuple[str, ...],
        request: HttpRequest,
        trace_id: str,
    ) -> None:
        self.backends = backends
        self.vary = vary
        # Released once the root is marked, since no search starts after.
        self.request: HttpRequest | None = request
        self.trace_id = trace_id
        # Re-entrant: app code that a collection runs, such as a body's
        # finally, can make a call while this thread holds it.
        self._lock = threading.RLock()
        self._outcome: _Outcome | None = None
        self._started = False
        self._marked = False
        self._running: dict[asyncio.AbstractEventLoop, asyncio.Future[None]] = {}

    def _record(self, outcome: _Outcome, cause: BaseException | None) -> _Outcome:
        """Write ``outcome`` unless one is written; return the one written.
        Hold the lock. A failure is logged when it is written.
        """
        if self._outcome is None:
            self._outcome = outcome
            if outcome[1] is not None:
                with contextlib.suppress(Exception):
                    _log.error(
                        "request root (trace %s): %s",
                        self.trace_id,
                        outcome[1],
                        exc_info=cause,
                        stack_info=cause is None,
                    )
        return self._outcome

    def _begin(self) -> tuple[_Outcome | None, HttpRequest | None]:
        """The outcome, if one is fixed; else mark a search as started and
        return the request to search with. Hold the lock.

        A call after the response phase fixes a failure, whether or not
        another thread's search is still running, whose result is then
        dropped (spec 05, APP-59 before APP-56). So does a call from inside
        one of the root's own searches, so a backend that calls neither
        recurses nor waits for itself.
        """
        if self._outcome is not None:
            return self._outcome, None
        if any(running is self for running in _SEARCHING.get()):
            return self._record((None, _REENTERED), None), None
        if self._marked or self.request is None:  # the request goes at the mark
            return self._record((None, _LATE), None), None
        self._started = True
        return None, self.request

    def _settle(self, found: Principal | None) -> _Outcome:
        """The outcome a completed search found, as written."""
        cause: BaseException | None = None
        try:
            outcome: _Outcome = (
                None if found is None else principal_form(found),
                None,
            )
        except ValueError as error:
            outcome, cause = (None, f"the principal is not valid: {error}"), error
        with self._lock:
            return self._record(outcome, cause)

    def _failed(self, error: Failed) -> _Outcome:
        with self._lock:
            return self._record((None, str(error)), error)

    def _read(self, outcome: _Outcome) -> bytes | None:
        form, problem = outcome
        if problem is not None:
            raise ValueError(_FAILED.format(self.trace_id))
        return form

    def form(self) -> bytes | None:
        """The principal's sent form, for a synchronous caller.

        Runs the search on this thread with each backend's ``resolve`` when
        no outcome is fixed, and returns the outcome written.

        Raises:
            ValueError: When a failure to resolve is fixed for the root.
        """
        with self._lock:
            outcome, request = self._begin()
        if outcome is None:
            try:
                with _searching(self):
                    found = search(self.backends, request, self._fixed)  # type: ignore[arg-type]
            except Failed as error:
                outcome = self._failed(error)
            else:
                outcome = self._settle(found)
        return self._read(outcome)

    async def aform(self) -> bytes | None:
        """The principal's sent form, for an asynchronous caller.

        Callers on one event loop share one search in flight, run with each
        backend's ``aresolve`` by the first of them; a caller on another loop
        runs its own. A search cut short by a ``BaseException`` that is not
        an ``Exception``, such as a cancellation, records nothing: the
        callers awaiting it are not cancelled, and one of them runs it again.

        Raises:
            ValueError: When a failure to resolve is fixed for the root.
        """
        loop = asyncio.get_running_loop()
        while True:
            with self._lock:
                outcome, request = self._begin()
                running = None if outcome is not None else self._running.get(loop)
                mine = outcome is None and running is None
                if mine:
                    running = loop.create_future()
                    self._running[loop] = running
            if outcome is not None:
                return self._read(outcome)
            if not mine:
                assert running is not None  # noqa: S101  # set above
                # Shielded: a waiter that is cancelled leaves the search alone.
                await asyncio.shield(running)
                continue
            try:
                with _searching(self):
                    found = await asearch(self.backends, request, self._fixed)  # type: ignore[arg-type]
            except Failed as error:
                outcome = self._failed(error)
            else:
                outcome = self._settle(found)
            finally:
                with self._lock:
                    if self._running.get(loop) is running:
                        del self._running[loop]
                if running is not None and not running.done():
                    running.set_result(None)
            return self._read(outcome)

    @property
    def fixed(self) -> bool:
        """Whether an outcome is written."""
        return self._fixed()

    def _fixed(self) -> bool:
        with self._lock:
            return self._outcome is not None

    @property
    def open(self) -> bool:
        """Whether a search could start now: no outcome is written, the
        root is not marked, and there is a request to give the backends.
        """
        with self._lock:
            return (
                self._outcome is None and not self._marked and self.request is not None
            )

    def finish(self) -> bool:
        """Mark the root, so no search starts for it afterwards; return
        whether a search had started.
        """
        with self._lock:
            self._marked = True
            self.request = None
            return self._started
