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
import os
import re
import sys
from collections.abc import Sequence
from typing import Any, Final

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import CommandError

__all__ = ["main", "parse_bind"]

_DEFAULT_BIND: Final = "127.0.0.1:8000"
_PORT: Final = re.compile(r"0|[1-9][0-9]{0,4}")
_LABEL: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_DIGITS: Final = re.compile(r"[0-9]+")
_SHOWN: Final = 300


def parse_bind(text: str) -> tuple[str, int, bool]:
    """Read ``--bind``: the host, the port, and whether the host is IPv6.

    The host is an IPv4 address, a host name of RFC 1123 labels whose last
    label is not all digits, or an IPv6 address in brackets with no zone.
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
    return (
        len(host) <= 253
        and all(_LABEL.fullmatch(label) for label in labels)
        and not _DIGITS.fullmatch(labels[-1])
    )


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


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``neolith`` command line; return the exit status."""
    arguments = _parser().parse_args(argv)
    os.environ.pop("DJANGO_SETTINGS_MODULE", None)
    try:
        if arguments.command == "run":
            return _run(arguments)
        return _migrate(arguments)
    except (ImproperlyConfigured, CommandError) as refusal:
        _report(refusal)
        return 1


def _report(refusal: BaseException) -> None:
    for line in str(refusal).splitlines() or [type(refusal).__qualname__]:
        if line:
            sys.stderr.write(f"neolith: {line}\n")
    sys.stderr.flush()


def _cause(error: BaseException) -> str:
    try:
        text = str(error)
    except Exception:
        text = "<the message cannot be read>"
    return f"{type(error).__qualname__}: {_shown(text)}"


def _set_up(arguments: argparse.Namespace, *, serving: bool) -> None:
    """Import the deployer's module, build the settings, configure Django
    and load its apps.
    """
    import django
    from django.conf import settings

    from neolith import host

    name: str = arguments.settings
    with host._deployer_import(name):
        try:
            module = importlib.import_module(name)
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
    built = host._build(list(arguments.apps), vars(module), closed=True)
    settings.configure(**built)
    django.setup(set_prefix=not serving)


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
    sys.stdout.write(
        f"neolith: serving {', '.join(arguments.apps)} at "
        f"http://{shown}:{server.server_port}/ with Django's development server, "
        "which is not for production\n"
    )
    sys.stdout.flush()
    return _server.serve(server, bound, sys.stdout, sys.stderr)
