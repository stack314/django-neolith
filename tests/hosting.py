"""Run code in a fresh process with Django set up on given apps.

A process hosts one set of apps (ADR 0022 Decision 5), so a test that sets
the host up on apps of its own runs in a process of its own.
"""

import json
import os
import subprocess
import sys
import textwrap
from collections.abc import Mapping, Sequence

# The tail of every unknown-key refusal of NEOLITH: one copy, so a new key in
# the host's table changes one line of the tests (ADR 0022 Decision 3).
NEOLITH_KEYS = (
    "the keys are CALLER_BACKENDS, DATABASES, PRINCIPAL_BACKENDS, REQUEST_DEADLINE, "
    "SIGNING_SECRETS, UNDECLARED_APPS"
)

_PREAMBLE = """\
import json
import sys

from django.conf import settings

settings.configure(
    **{
        "INSTALLED_APPS": json.loads(sys.argv[1]),
        "MIDDLEWARE": [
            "neolith.middleware.RootMiddleware",
            "neolith.middleware.PrincipalMiddleware",
        ],
        "NEOLITH": {},
        "DATABASES": {"default": {}},
        "DATABASE_ROUTERS": ["neolith.routers.AppRouter"],
        "USE_TZ": True,
        **json.loads(sys.argv[2]),
    }
)
"""

_BUILT = """\
import json
import sys

from django.conf import settings

from neolith.host import build_settings

settings.configure(
    **build_settings(
        json.loads(sys.argv[1]),
        {
            "MIDDLEWARE": [
            "neolith.middleware.RootMiddleware",
            "neolith.middleware.PrincipalMiddleware",
        ],
            "USE_TZ": True,
            **json.loads(sys.argv[2]),
        },
    )
)
"""


def in_fresh_process(
    body: str,
    installed: Sequence[str],
    settings: Mapping[str, object] | None = None,
    *,
    timeout: float | None = None,
) -> str:
    """Run ``body`` after configuring ``installed``, and ``settings`` if
    given, which must be JSON; return what it printed. A run that takes
    longer than ``timeout`` seconds fails with ``subprocess.TimeoutExpired``.

    The settings are written by hand, as the builder writes them for
    ``installed`` when every entry but ``neolith`` is a hosted app, so the
    first set-up's comparison (``neolith.E005``) passes; a host with
    undeclared apps or databases uses ``in_built_host``.

    ``body`` calls ``django.setup()`` itself, so it can catch what set-up
    raises. Warnings are errors, as in the suite (ADR 0012). A failing run
    fails the test with the end of its standard error.
    """
    return _run(_PREAMBLE, body, list(installed), settings, timeout)


def in_built_host(
    body: str,
    hosted: Sequence[str],
    deployer: Mapping[str, object],
    *,
    timeout: float | None = None,
) -> str:
    """Run ``body`` after configuring the settings that
    ``neolith.host.build_settings`` builds for ``hosted`` and ``deployer``,
    which must be JSON and holds ``NEOLITH``; return what it printed.
    """
    return _run(_BUILT, body, list(hosted), deployer, timeout)


def _run(
    preamble: str,
    body: str,
    first: list[str],
    settings: Mapping[str, object] | None,
    timeout: float | None,
) -> str:
    ran = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-W",
            "error",
            "-c",
            preamble + textwrap.dedent(body),
            json.dumps(first),
            json.dumps(dict(settings or {})),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env={"PYTHONPATH": os.pathsep.join(sys.path)},
    )
    assert ran.returncode == 0, ran.stderr[-3000:]
    return ran.stdout
