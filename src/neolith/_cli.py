"""The ``neolith`` command line: ``neolith run`` and ``neolith migrate``
(spec 05; ADR 0022 Decisions 9 to 12).

::

    neolith run <app> [<app> ...] --settings <module> [--bind <host>:<port>]
    neolith migrate <app> [<app> ...] --settings <module>

Each ``<app>`` is the import path of an app package. ``--settings`` names
the deployer's module: it holds the deployer's names, ``NEOLITH`` among
them, and neither calls ``build_settings`` nor reads a setting.
``DJANGO_SETTINGS_MODULE`` is not read: it names a whole settings module,
which this module is not. The host is closed: it has no ``default``
database, so it hosts declared apps only, and a battery with tables is
refused.

The phases run in order, and a phase that refuses stops the host before the
next: the module is imported; the builder runs Declare, Resolve and
Configure; Django's settings are configured and its apps loaded, which sets
the host up; ``neolith run`` builds a handler, which boots, and only then
binds its address and serves until a signal, then drains;
``neolith migrate`` runs the ``migrateapps`` command. Each refusal is
printed as ``neolith: <line>`` on standard error, with status 1. A mistake
in the arguments prints usage with status 2.
"""

import argparse
import importlib
import ipaddress
import logging
import os
import re
import socket
import sys
import traceback
from collections.abc import Sequence
from typing import Any, Final

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import CommandError

from neolith import _naming

__all__ = ["main", "parse_bind"]

_DEFAULT_BIND: Final = "127.0.0.1:8000"
_PORT: Final = re.compile(r"0|[1-9][0-9]{0,4}")
_LABEL: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
# A label that a URL parser or the C resolver reads as a number (WHATWG
# "ends in a number"), such as 1 or 0x7f.
_NUMBER: Final = re.compile(r"[0-9]+|0[xX][0-9A-Fa-f]*")
_SETTINGS: Final = "DJANGO_SETTINGS_MODULE"
_UNSET: Final = "neolith._unset"
_SHOWN: Final = 300


def parse_bind(text: str) -> tuple[str, int, bool]:
    """Read ``--bind``: the host, the port, and whether the host is IPv6.

    The host is an IPv4 address in dotted decimal, a host name of RFC 1123
    labels whose last label is not a number and which the C resolver does
    not read as an IPv4 address in another spelling (``0x7f000001``), or an
    IPv6 address in brackets with no zone.
    The port is 0 to 65535 in decimal with no leading zero; 0 lets the
    system choose.

    Raises:
        ValueError: ``text`` is outside that form.
    """
    refused = ValueError(
        f"--bind: expected <host>:<port>, such as {_DEFAULT_BIND} or [::1]:8000, "
        f"got {_shown(text)!r}"
    )
    if text.startswith("["):
        host, closing, rest = text[1:].partition("]")
        if not closing or not rest.startswith(":"):
            raise refused
        port, ipv6 = rest[1:], True
        if "%" in host:
            raise refused
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise refused from None
    else:
        host, colon, port = text.rpartition(":")
        ipv6 = False
        if not colon or not _host_name_or_ipv4(host):
            raise refused
    if not _PORT.fullmatch(port) or int(port) > 65535:
        raise refused
    return host, int(port), ipv6


def _host_name_or_ipv4(host: str) -> bool:
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        pass
    else:
        return True
    labels = host.split(".")
    if (
        len(host) > 253
        or not all(_LABEL.fullmatch(label) for label in labels)
        or _NUMBER.fullmatch(labels[-1])
    ):
        return False
    try:
        socket.inet_aton(host)
    except OSError:
        return True
    return False


def _shown(text: str) -> str:
    return text if len(text) <= _SHOWN else f"{text[:_SHOWN]}..."


def _bind(text: str) -> tuple[str, int, bool]:
    try:
        return parse_bind(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


class _Once(argparse.Action):
    """An option that may be given once."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,  # noqa: ANN401
        option_string: str | None = None,
    ) -> None:
        if getattr(namespace, self.dest, None) is not None:
            parser.error(f"{option_string} is given twice")
        setattr(namespace, self.dest, values)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="neolith",
        description="Host neolith apps from their declarations.",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    run = commands.add_parser(
        "run",
        help="boot the named apps in one host and serve them",
        description=(
            "Boot the named apps in one host and serve them over HTTP with "
            "Django's development server, which is not for production. On "
            "SIGINT or SIGTERM, stop taking requests and give each request in "
            "flight until the request deadline, or 30 seconds when there is "
            "none; a second signal stops at once."
        ),
        allow_abbrev=False,
    )
    migrate = commands.add_parser(
        "migrate",
        help="apply each named app's migrations to its own database",
        description="Apply each named app's migrations to the app's own database.",
        allow_abbrev=False,
    )
    for command in (run, migrate):
        command.add_argument(
            "apps", nargs="+", metavar="app", help="the import path of an app package"
        )
        command.add_argument(
            "--settings",
            action=_Once,
            required=True,
            metavar="module",
            help="the module that holds the deployer's settings",
        )
    run.add_argument(
        "--bind",
        action=_Once,
        type=_bind,
        metavar="host:port",
        help=f"the address to serve on (default {_DEFAULT_BIND})",
    )
    return parser


class _HostLog(logging.Handler):
    """The host's log under the command line: each line printed as
    ``neolith: <line>``, a warning's as ``neolith: warning: <line>``, with a
    record's traceback after it. ``printed`` holds the lines of the errors
    printed, so a refusal the host logged before raising it is printed once.
    """

    def __init__(self) -> None:
        super().__init__()
        self.printed: set[str] = set()

    def emit(self, record: logging.LogRecord) -> None:
        from neolith import _server

        try:
            warning = record.levelno < logging.ERROR
            for line in record.getMessage().splitlines():
                if warning:
                    _server.say(sys.stderr, f"neolith: warning: {line}")
                else:
                    self.printed.add(line)
                    _server.say(sys.stderr, f"neolith: {line}")
            if record.exc_info:
                _server.say(
                    sys.stderr,
                    "".join(traceback.format_exception(*record.exc_info)).rstrip(),
                )
        except Exception:
            self.handleError(record)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``neolith`` command line; return the exit status."""
    arguments = _parser().parse_args(argv)
    # A standard stream that is closed is None in Python; nothing the host
    # prints may stop it.
    for stream in ("stdout", "stderr"):
        if getattr(sys, stream) is None:
            setattr(sys, stream, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115
    os.environ.pop(_SETTINGS, None)
    # The modules named on the command line are found from the working
    # directory, as python -m finds them, whichever way the script started.
    here = os.getcwd()
    if "" not in sys.path and here not in sys.path:
        sys.path.insert(0, here)
    log = _HostLog()
    logging.getLogger("neolith.host").addHandler(log)
    try:
        if arguments.command == "run":
            return _run(arguments)
        return _migrate(arguments)
    except (ImproperlyConfigured, CommandError) as refusal:
        _report(refusal, log.printed)
        return 1


def _report(refusal: BaseException, printed: set[str]) -> None:
    from neolith import _server

    for line in _naming.text(refusal, None).splitlines() or [
        _naming.type_name(refusal)
    ]:
        if line and line not in printed:
            _server.say(sys.stderr, f"neolith: {line}")


def _cause(error: BaseException) -> str:
    return _naming.cause(error, _SHOWN + 3)


def _set_up(arguments: argparse.Namespace, *, serving: bool) -> None:
    """Import the deployer's module, build the settings, configure Django
    and load its apps.
    """
    import django
    from django.conf import settings

    name: str = arguments.settings
    # Until settings exist, a read of a setting imports this module, which
    # raises: Django's own advice, to set DJANGO_SETTINGS_MODULE, does not
    # apply here.
    os.environ[_SETTINGS] = _UNSET
    try:
        built = _built(name, list(arguments.apps))
    finally:
        os.environ.pop(_SETTINGS, None)
    settings.configure(**built)
    django.setup(set_prefix=not serving)


def _built(name: str, apps: list[str]) -> dict[str, Any]:
    """Import the deployer's module ``name`` and build the settings."""
    from django.conf import settings

    from neolith import _configure, host

    with host._deployer_import(name):
        try:
            module = importlib.import_module(name)
        except _configure.EarlyRead as error:
            raise ImproperlyConfigured(
                f"settings: {_shown(name)} reads Django settings at import; it "
                "holds the deployer's names, which are read before settings exist"
            ) from error
        except ImproperlyConfigured as error:
            if error is host._refused:
                raise
            raise ImproperlyConfigured(
                f"settings: {_shown(name)} cannot be imported: {_cause(error)}"
            ) from error
        except Exception as error:
            raise ImproperlyConfigured(
                f"settings: {_shown(name)} cannot be imported: {_cause(error)}"
            ) from error
    if settings.configured:
        raise ImproperlyConfigured(
            f"settings: {_shown(name)} configured Django's settings while it was "
            "imported; it holds the deployer's names, which are read before "
            "settings exist"
        )
    names = _naming.module_dict(module)
    if names is None:
        raise ImproperlyConfigured(
            f"settings: {_shown(name)} is a {_naming.type_name(module)}, not a module"
        )
    return host._build(apps, names, closed=True)


def _migrate(arguments: argparse.Namespace) -> int:
    from django.core.management import call_command

    _set_up(arguments, serving=False)
    call_command("migrateapps", stdout=sys.stdout, stderr=sys.stderr)
    return 0


def _run(arguments: argparse.Namespace) -> int:
    from django.core.handlers.wsgi import WSGIHandler

    from neolith import _host, _server

    host, port, ipv6 = arguments.bind or parse_bind(_DEFAULT_BIND)
    _set_up(arguments, serving=True)
    handler = WSGIHandler()
    shown = f"[{host}]" if ipv6 else host
    try:
        server = _server.listen(host, port, ipv6=ipv6)
    except OSError as error:
        raise ImproperlyConfigured(
            f"--bind {shown}:{port}: cannot listen: {_cause(error)}"
        ) from error
    server.set_app(handler)
    current = _host._current()
    bound = _server.drain_bound(current.request_deadline if current else None)
    serving = (
        f"serving {', '.join(arguments.apps)} at http://{shown}:{server.server_port}/ "
        "with Django's development server, which is not for production"
    )
    return _server.serve(server, bound, sys.stdout, sys.stderr, serving)
