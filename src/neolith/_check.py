"""The check before deployment: ``manage.py checkapps`` and ``neolith check``
(spec 05, *The check before deployment*; ADR 0030).

One command, ``checkapps``, with two entries (ADR 0030 Decision 7).
``manage.py checkapps`` checks the host that Django's settings module holds;
``neolith check <app> [<app> ...] --settings <module>`` builds a closed host
from the deployer's module, as ``neolith run`` does, and runs the same
command. Its phases, in order:

1. the shape of ``[tool.neolith]`` in the nearest ``pyproject.toml`` from
   the working directory upward (``read_config``), status 2 on a mistake;
2. the builder's Declare, Resolve and Configure, and
3. Django's loading of the apps, which sets the host up; then each label of
   ``boundary-report-only`` is a hosted app, status 2 otherwise;
4. Boot's checks that read no database (``neolith._boot.static_messages``),
   whose refusals and warnings print as the host prints them;
5. the boundary pass over the registry's models (``neolith._check_models``)
   and the hosted apps' source (``neolith._check_source``), whose findings
   print one per line on standard output, sorted, then the summary line.

A refusal in phases 2 and 3 ends the run with status 1 in the form the
entry gives it. Phases 4 and 5 both run and are reported together. The exit
status is 0, 1 when the host refuses or an enforced app has an error finding
or a stale ``boundary-report-only`` entry is found (``neolith.E113``), and 2
for a mistake in the arguments or in ``[tool.neolith]``, or for an error the
check did not foresee, printed as ``neolith: check: internal error: ...``. A
signal to stop, such as ``KeyboardInterrupt``, is not such an error and goes
on (ADR 0030, open point 6 of #14).

State the module owns (CONTRIBUTING K6), behind one lock: while any run of
the command is under way, Django's ``BaseDatabaseWrapper.connect`` is
replaced on the class, for the whole process, by a function that raises
``neolith.UsageError``, so the command opens no connection (APP-70); and
the host's log handler is attached to the ``neolith.host`` logger unless the
``neolith`` script attached it. Runs share both, counted, and the last to
end puts them back, however it ends. The command is meant for a process of
its own: other threads' new connections are refused while it runs.
"""

import contextlib
import contextvars
import dataclasses
import logging
import os
import stat
import sys
import threading
import tomllib
import traceback
import unicodedata
from collections.abc import Callable, Iterator, Sequence
from io import TextIOBase
from typing import Final, TextIO

from django.apps import apps as global_apps
from django.core import checks

from neolith import _naming
from neolith._context import UsageError
from neolith._declaration import declaration_of

__all__ = [
    "Finding",
    "command",
    "deployer",
    "guarded",
    "label_problems",
    "place",
    "read_config",
    "say",
    "shown",
    "stream_writer",
]

_log = logging.getLogger("neolith.host")
_LOCK: Final = threading.Lock()
# The commands that hold the guard and the host's log handler now, and
# what the first of them replaced or attached (CONTRIBUTING K6).
_guards = 0
_connect: object = None
_logs = 0
_handler: logging.Handler | None = None
_SHOWN: Final = 300
# The deployer's module that ``neolith check`` built the settings from;
# Django's own SETTINGS_MODULE is unset after settings.configure().
_DEPLOYER: Final[contextvars.ContextVar[str | None]] = contextvars.ContextVar(
    "neolith_check_deployer", default=None
)
_PYPROJECT: Final = "pyproject.toml"
_TABLE: Final = "[tool.neolith]"
_KEY: Final = "boundary-report-only"
_LISTED: Final = f"{_TABLE} {_KEY}"
_GUARD: Final = (
    "check: database {alias}: a connection was opened while the host was set "
    "up or checked, and the check runs without a database"
)
# Characters printed escaped: those that end a line for str.splitlines or a
# terminal (Cc, Zl, Zp), invisible format characters (Cf), and lone
# surrogates, which no UTF-8 stream can write.
_ESCAPED: Final = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


def shown(text: str, limit: int = _SHOWN) -> str:
    """``text`` as one printable line: each control, format or line-ending
    character, and each lone surrogate, written as its Python escape, and
    the whole cut to ``limit`` characters with ``...`` after it.
    """
    escaped = "".join(
        char.encode("unicode_escape").decode("ascii")
        if unicodedata.category(char) in _ESCAPED
        else char
        for char in text[: limit + 1]
    )
    return escaped if len(text) <= limit else f"{escaped[:limit]}..."


@dataclasses.dataclass(slots=True, eq=False)
class Finding:
    """One finding: Django's check message, never registered (ADR 0030
    Decision 8), the hosted app it names, and its place in source, if any.
    """

    message: checks.CheckMessage
    app: str | None
    path: str | None = None
    line: int = 0


def read_config() -> tuple[list[str], list[str]]:
    """Phase 1: the labels of ``[tool.neolith] boundary-report-only`` in the
    nearest ``pyproject.toml`` from the working directory upward, and every
    refusal of its shape, each a line without the ``neolith:`` prefix.

    Only that one file is read. It may be absent, may have no
    ``[tool.neolith]`` table, or the table's one key may be
    ``boundary-report-only``, a list of one or more ``str``, none twice.
    Whether each label is a hosted app is judged once the apps are loaded
    (``label_problems``).
    """
    try:
        here = os.getcwd()
    except OSError as error:
        return [], [f"{_PYPROJECT}: cannot be read: {_cause(error)}"]
    path = _nearest(here)
    if path is None:
        return [], []
    try:
        data = _load(path)
    except (OSError, ValueError, RecursionError, MemoryError) as error:
        return [], [f"{_PYPROJECT}: cannot be read: {_cause(error)}"]
    tool = data.get("tool")
    if type(tool) is not dict or "neolith" not in tool:
        return [], []
    table = tool["neolith"]
    if type(table) is not dict:
        return [], [f"{_TABLE}: expected a table, got {_naming.type_name(table)}"]
    problems = [
        f"{_TABLE}: {shown(key)} is not a key the check reads"
        for key in table
        if key != _KEY
    ]
    if _KEY not in table:
        return [], problems
    value = table[_KEY]
    if type(value) is not list or any(type(label) is not str for label in value):
        return [], [*problems, f"{_LISTED}: expected a list of str"]
    if not value:
        return [], [*problems, f"{_LISTED}: is empty; leave the key out"]
    labels: list[str] = list(dict.fromkeys(value))
    problems += [
        f"{_LISTED}: {shown(label)} listed twice"
        for label in labels
        if value.count(label) > 1
    ]
    return labels, problems


def _nearest(here: str) -> str | None:
    directory = here
    while True:
        candidate = os.path.join(directory, _PYPROJECT)
        if os.path.lexists(candidate):
            return candidate
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def _load(path: str) -> dict[str, object]:
    """Read ``path`` as TOML. Only a regular file is read, so a pipe or a
    device under that name never holds the check up.
    """
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        regular = stat.S_ISREG(os.fstat(descriptor).st_mode)
    except BaseException:
        os.close(descriptor)
        raise
    if not regular:
        os.close(descriptor)
        raise OSError(f"{path} is not a regular file")
    with os.fdopen(descriptor, "rb") as file:
        return tomllib.load(file)


def label_problems(labels: Sequence[str], hosted: Sequence[str]) -> list[str]:
    """Each label of ``boundary-report-only`` that is not a hosted app."""
    return [
        f"{_LISTED}: {shown(label)} is not a hosted app"
        for label in labels
        if label not in hosted
    ]


def _refused(self: object) -> None:
    """``BaseDatabaseWrapper.connect`` while a guard is held."""
    alias = getattr(self, "alias", None)
    raise UsageError(_GUARD.format(alias=shown(str(alias))))


@contextlib.contextmanager
def guarded() -> Iterator[None]:
    """Refuse every new database connection in the process while the block
    runs, with ``neolith.UsageError``. Holders share one guard: the first
    puts it in place and the last to leave puts Django's method back,
    however each leaves and in whatever order.
    """
    global _guards, _connect
    from django.db.backends.base.base import BaseDatabaseWrapper

    with _LOCK:
        if _guards == 0:
            # safe read: Django's own class, never an author's.
            _connect = vars(BaseDatabaseWrapper)["connect"]
            BaseDatabaseWrapper.connect = _refused  # type: ignore[method-assign]
        _guards += 1
    try:
        yield
    finally:
        with _LOCK:
            _guards -= 1
            if _guards == 0:
                BaseDatabaseWrapper.connect = _connect  # type: ignore[method-assign,assignment]
                _connect = None


@contextlib.contextmanager
def _host_log() -> Iterator[None]:
    """Print the host's log as the ``neolith`` script prints it. Holders
    share one handler, attached by the first unless the script attached
    its own, and removed when the last leaves.
    """
    global _logs, _handler
    from neolith._cli import _HostLog

    with _LOCK:
        if _logs == 0 and not any(type(h) is _HostLog for h in _log.handlers):
            _handler = _HostLog()
            _log.addHandler(_handler)
        _logs += 1
    try:
        yield
    finally:
        with _LOCK:
            _logs -= 1
            if _logs == 0 and _handler is not None:
                _log.removeHandler(_handler)
                _handler = None


def command(write: Callable[[str], object], *, show_traceback: bool) -> int:
    """Run phases 4 and 5, after the labels of ``[tool.neolith]`` are judged
    against the hosted apps; write each finding and the summary with
    ``write``; return the exit status.
    """
    with _host_log(), guarded():
        labels, problems = read_config()
        if not problems:
            try:
                problems = label_problems(labels, _hosted())
            except Exception as error:
                return _internal(error, show_traceback=show_traceback)
        if problems:
            for problem in problems:
                say(sys.stderr, f"neolith: {problem}")
            return 2
        try:
            hosted = _hosted()
        except Exception as error:
            return _internal(error, show_traceback=show_traceback)
        try:
            refused = _boot_checks()
            findings = _boundary(hosted)
        except Exception as error:
            return _internal(error, show_traceback=show_traceback)
        return _report(write, findings, frozenset(labels), refused=refused)


def _internal(error: Exception, *, show_traceback: bool) -> int:
    say(sys.stderr, f"neolith: check: internal error: {_cause(error)}")
    if show_traceback:
        say(sys.stderr, "".join(traceback.format_exception(error)).rstrip())
    return 2


def _cause(error: BaseException) -> str:
    return shown(_naming.cause(error, None))


def _hosted() -> list[str]:
    """The labels of the hosted apps, in the order Django holds them."""
    return [
        config.label
        for config in global_apps.get_app_configs()
        if declaration_of(type(config)) is not None
    ]


def _boot_checks() -> bool:
    """Phase 4: print what Boot's checks that read no database refuse and
    warn about, in the form the host's log handler prints Boot's lines;
    return whether anything was refused. They are printed, not logged, so
    a project's ``LOGGING`` can neither hide nor repeat them.
    """
    from neolith import _boot

    problems, found = _boot.static_messages()
    errors = sorted(f"({m.id}) {m.msg}" for m in found if m.level >= checks.ERROR)
    for message in sorted(found, key=lambda m: (str(m.id), m.msg)):
        if checks.WARNING <= message.level < checks.ERROR:
            for line in f"({message.id}) {message.msg}".splitlines():
                say(sys.stderr, f"neolith: warning: {line}")
    lines = sorted(problems) + errors
    if lines:
        for line in "\n".join(
            ["host: Boot would refuse the host:", *lines]
        ).splitlines():
            say(sys.stderr, f"neolith: {line}")
    return bool(lines)


def _boundary(hosted: Sequence[str]) -> list[Finding]:
    """Phase 5: every finding of the boundary pass."""
    from neolith import _check_models, _check_source

    configs = {label: global_apps.get_app_config(label) for label in hosted}
    source = _check_source.Source(configs, _owner, settings_module=_settings_module())
    return [*_check_models.findings(configs), *_check_source.findings(source)]


@contextlib.contextmanager
def deployer(name: str) -> Iterator[None]:
    """Name the deployer's module ``name`` as the settings module of the
    run inside the block (``neolith check``, ADR 0030 Decision 3).
    """
    token = _DEPLOYER.set(name)
    try:
        yield
    finally:
        _DEPLOYER.reset(token)


def _settings_module() -> str | None:
    """The settings module: the deployer's module under ``neolith check``,
    and the one Django loaded under ``manage.py checkapps``; ``None`` when
    settings were configured without one.
    """
    from django.conf import settings

    named = _DEPLOYER.get()
    if named is not None:
        return named
    name = getattr(settings, "SETTINGS_MODULE", None)
    return name if type(name) is str else None


def _owner(name: str) -> str | None:
    """The label of the installed app that holds the dotted name ``name``,
    as Django's registry gives it (``Apps.get_containing_app_config``).
    """
    config = global_apps.get_containing_app_config(name)
    return None if config is None else config.label


def _report(
    write: Callable[[str], object],
    findings: list[Finding],
    report_only: frozenset[str],
    *,
    refused: bool,
) -> int:
    """Write the findings, sorted and each once, then the summary line;
    return the exit status.
    """
    failing = {
        finding.app
        for finding in findings
        if finding.message.level >= checks.ERROR and finding.app is not None
    }
    stale = [
        Finding(
            checks.Error(
                f"app {shown(label)}: is listed in {_LISTED} and has no error finding",
                hint="remove it from the list, so that the app's next finding "
                "fails the check",
                id="neolith.E113",
            ),
            label,
        )
        for label in sorted(report_only - failing)
    ]
    errors = marked = warnings = 0
    printed: set[str] = set()
    for finding in sorted([*findings, *stale], key=_order):
        message = finding.message
        quiet = (
            message.level >= checks.ERROR
            and finding.app in report_only
            and message.id != "neolith.E113"
        )
        line = _line(finding, report_only=quiet)
        if line in printed:
            continue
        printed.add(line)
        write(line)
        if message.level < checks.ERROR:
            warnings += 1
        elif quiet:
            marked += 1
        else:
            errors += 1
    write(
        f"neolith check: {errors} errors, {marked} errors in report-only apps, "
        f"{warnings} warnings"
    )
    return 1 if refused or errors else 0


def _order(finding: Finding) -> tuple[int, str, int, str, str]:
    """Located findings by path, line and id; then those with no place, by
    id and app.
    """
    message = finding.message
    if finding.path is None:
        return 1, str(message.id), 0, finding.app or "", message.msg
    return 0, finding.path, finding.line, str(message.id), message.msg


def _line(finding: Finding, *, report_only: bool) -> str:
    message = finding.message
    text = f"{message.id} {message.msg}"
    if message.hint:
        text = f"{text}; {message.hint}"
    if report_only:
        text = f"{text} (report only)"
    if finding.path is None:
        return text
    return f"{finding.path}:{finding.line}: {text}"


def place(path: str | None) -> str | None:
    """``path`` as a finding prints it: relative to the working directory
    when the file lies under it, absolute otherwise, and escaped (``shown``,
    with no bound).
    """
    if path is None:
        return None
    whole = os.path.abspath(path)
    try:
        here = os.getcwd()
        inside = os.path.commonpath([here, whole]) == here
    except (OSError, ValueError):
        inside = False
    return shown(os.path.relpath(whole, here) if inside else whole, sys.maxsize)


def say(stream: TextIO | TextIOBase | None, line: str) -> None:
    """Write ``line`` to ``stream`` as ``neolith._server.say`` does, with
    each character the stream's encoding cannot write as its escape, so a
    line it cannot encode is printed rather than lost.
    """
    from neolith import _server

    encoding = getattr(stream, "encoding", None)
    if type(encoding) is str:
        with contextlib.suppress(LookupError):
            line = line.encode(encoding, "backslashreplace").decode(encoding)
    _server.say(stream, line)  # type: ignore[arg-type]


def stream_writer(stream: TextIO | TextIOBase) -> Callable[[str], None]:
    """A ``write`` for ``command`` that prints each line to ``stream``."""

    def write(line: str) -> None:
        say(stream, line)

    return write
