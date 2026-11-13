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

_PREAMBLE = """\
import json
import sys

from django.conf import settings

settings.configure(
    **{
        "INSTALLED_APPS": json.loads(sys.argv[1]),
        "MIDDLEWARE": ["neolith.middleware.RootMiddleware"],
        "NEOLITH": {},
        "USE_TZ": True,
        **json.loads(sys.argv[2]),
    }
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

    ``body`` calls ``django.setup()`` itself, so it can catch what set-up
    raises. Warnings are errors, as in the suite (ADR 0012). A failing run
    fails the test with the end of its standard error.
    """
    ran = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-W",
            "error",
            "-c",
            _PREAMBLE + textwrap.dedent(body),
            json.dumps(list(installed)),
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
