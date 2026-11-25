"""Run a check on a 1 MB thread stack, in a process of its own.

Alpine's Python builds give a thread 1 MB of stack, where a reader that
recurses too deep ends the process rather than raising (issue #125). A
check run there in the test process would end the whole test run on a
regression; in a process of its own, it fails the one test that ran it.
"""

import os
import subprocess
import sys
from collections.abc import Callable

# Logging is off, so that stderr holds only what went wrong: a traceback.
_RUN = """
import importlib, logging, sys, threading
logging.disable()
import django
django.setup()
from neolith import testing
testing.boot()
check = getattr(importlib.import_module(sys.argv[1]), sys.argv[2])
said = []
threading.stack_size(1024 * 1024)
thread = threading.Thread(target=lambda: said.extend(check()))
thread.start()
thread.join()
print("\\n".join(said))
"""


def on_a_small_stack(check: Callable[[], list[str]]) -> tuple[int, str, list[str]]:
    """Run ``check``, a module-level function, on a 1 MB thread stack.

    Returns the process's exit status, the end of what it wrote to stderr
    and the lines ``check`` returned, so that one assertion shows what went
    wrong: a crash fails the test with a negative status.
    """
    ran = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-X",
            "faulthandler",
            "-c",
            _RUN,
            check.__module__,
            check.__qualname__,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env={
            "PYTHONPATH": os.pathsep.join(sys.path),
            "DJANGO_SETTINGS_MODULE": "tests.settings",
        },
    )
    return ran.returncode, ran.stderr[-600:], ran.stdout.splitlines()
