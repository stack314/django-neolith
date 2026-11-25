"""``neolith`` started by a test as a process of its own, in a directory of
the test's choosing, with an environment of the test's choosing.

A process hosts one set of apps (ADR 0022 Decision 5), so a test that runs
a host, or several hosts that call each other, starts each with
``python -m neolith``. The helper waits on lines the host prints, never on
time (T3), and bounds every wait and every one-shot command at 120 seconds
(ADR 0032 Decision 11). A host's argument list holds only module names and
the bind address: a secret reaches it through the environment alone.
"""

import contextlib
import http.client
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import IO

import pytest

from neolith._cli import parse_bind

# ADR 0032 Decision 11: every subprocess has a bound of 120 seconds.
BOUND = 120.0
SERVING = "neolith: serving "
STOPPED = "neolith: stopped"
BIND = "127.0.0.1:0"

# A dotted Python module name: the only text, besides the bind address, that
# goes into a command line.
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_ONE_SHOT = ("migrate", "check")


def _modules(apps: Sequence[str], settings: str) -> list[str]:
    """``apps`` and ``settings`` checked to be module names, so that nothing
    else, a secret least of all, can be put in an argument list."""
    if isinstance(apps, str):
        raise TypeError("apps is a sequence of module names, not one string")
    if not apps:
        raise ValueError("a host needs at least one app")
    for name in [*apps, settings]:
        if not _MODULE.fullmatch(name):
            raise ValueError(f"not a module name: {name!r}")
    return [*apps, "--settings", settings]


def environment(env: Mapping[str, str]) -> dict[str, str]:
    """The child's whole environment: ``PATH``, a ``PYTHONPATH`` that finds
    what this process imports, and ``env`` over both. Nothing else of this
    process's environment is passed on."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.pathsep.join(entry for entry in sys.path if entry),
        **env,
    }


def command(name: str, apps: Sequence[str], settings: str, *more: str) -> list[str]:
    """The argument list of ``neolith <name>``, with warnings as errors."""
    return [
        sys.executable,
        "-W",
        "error",
        "-m",
        "neolith",
        name,
        *_modules(apps, settings),
        *more,
    ]


def once(
    name: str,
    apps: Sequence[str],
    *,
    settings: str,
    cwd: Path,
    env: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    """Run ``neolith migrate`` or ``neolith check`` to its end and return
    what it printed; a run that outlasts the bound fails the test."""
    if name not in _ONE_SHOT:
        raise ValueError(f"a one-shot command is one of {_ONE_SHOT}, not {name!r}")
    arguments = command(name, apps, settings)
    try:
        return subprocess.run(  # noqa: S603
            arguments,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            cwd=cwd,
            timeout=BOUND,
            env=environment(env),
        )
    except subprocess.TimeoutExpired as late:
        pytest.fail(
            f"neolith {name} ran past {BOUND:.0f} s; "
            f"stdout: {late.stdout!r}; stderr: {late.stderr!r}"
        )


class Host:
    """``neolith run`` in a process of its own, with every line it prints.

    ``lines`` holds what was read so far, as ``(stream, line)`` pairs with
    stream ``"out"`` or ``"err"``, in the order this process read them.
    """

    def __init__(
        self,
        apps: Sequence[str],
        *,
        settings: str,
        cwd: Path,
        env: Mapping[str, str],
        bind: str = BIND,
    ) -> None:
        # neolith's own reading of --bind, so that every address admitted
        # here is one the host listens on and request() connects to.
        self.address, _, _ = parse_bind(bind)
        self.arguments = command("run", apps, settings, "--bind", bind)
        self.cwd = cwd
        self.lines: list[tuple[str, str]] = []
        self.port = 0
        self._waited: set[int] = set()
        self._queue: queue.Queue[tuple[str, str | None]] = queue.Queue()
        self._ends = 0
        self.process = subprocess.Popen(  # noqa: S603
            self.arguments,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=cwd,
            env=environment(env),
        )
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        self._readers = [
            threading.Thread(target=self._read, args=(name, stream), daemon=True)
            for name, stream in (
                ("out", self.process.stdout),
                ("err", self.process.stderr),
            )
        ]
        for reader in self._readers:
            reader.start()

    def _read(self, name: str, stream: IO[str]) -> None:
        for line in stream:
            self._queue.put((name, line.rstrip("\n")))
        self._queue.put((name, None))

    def _take(self, deadline: float) -> bool:
        """Move one line, or one stream's end, from the readers into
        ``lines``; false once the deadline has passed or both streams ended."""
        left = deadline - time.monotonic()
        if self._ends == 2 or left <= 0:
            return False
        try:
            name, line = self._queue.get(timeout=left)
        except queue.Empty:
            return False
        if line is None:
            self._ends += 1
        else:
            self.lines.append((name, line))
        return True

    def wait_for(self, text: str) -> str:
        """The first line, on either stream and not yet waited for, that
        contains ``text``. Fails the test, with everything printed, if the
        host ends or the bound passes first."""
        deadline = time.monotonic() + BOUND
        start = 0
        while True:
            for index in range(start, len(self.lines)):
                if index not in self._waited and text in self.lines[index][1]:
                    self._waited.add(index)
                    return self.lines[index][1]
            start = len(self.lines)
            if not self._take(deadline):
                break
        ended = "the host ended" if self._ends == 2 else f"{BOUND:.0f} s passed"
        pytest.fail(f"no line with {text!r} before {ended}; printed: {self.lines}")

    def serving(self) -> int:
        """Wait for the ``serving`` line and return the port it names."""
        line = self.wait_for(SERVING)
        found = re.search(r" at http://(?:\[[^\]]*\]|[^:/]*):([0-9]+)/", line)
        assert found, f"no port in {line!r}"
        self.port = int(found[1])
        return self.port

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, bytes]:
        """One request to the address the host was bound to; the status and
        the body."""
        assert self.port, "the host has not said where it serves"
        connection = http.client.HTTPConnection(self.address, self.port, timeout=BOUND)
        try:
            connection.request(method, path, body=body, headers=dict(headers or {}))
            reply = connection.getresponse()
            return reply.status, reply.read()
        finally:
            connection.close()

    def stop(self) -> int:
        """Send SIGTERM, wait for ``neolith: stopped`` and the process's end,
        and return its exit status."""
        self.process.send_signal(signal.SIGTERM)
        self.wait_for(STOPPED)
        return self.ended()

    def ended(self) -> int:
        """Wait for the process to end and read the rest of what it printed;
        return its exit status."""
        deadline = time.monotonic() + BOUND
        try:
            status = self.process.wait(timeout=BOUND)
        except subprocess.TimeoutExpired:
            pytest.fail(f"the host ran past {BOUND:.0f} s; printed: {self.lines}")
        while self._take(deadline):
            pass
        return status

    def printed(self) -> str:
        """Everything read from both streams so far, one line each."""
        return "\n".join(line for _, line in self.lines)

    def close(self) -> None:
        """Kill the process if it still runs, and close its pipes."""
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=BOUND)
        for reader in self._readers:
            reader.join(timeout=BOUND)
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()


@contextlib.contextmanager
def served(
    apps: Sequence[str],
    *,
    settings: str,
    cwd: Path,
    env: Mapping[str, str],
    bind: str = BIND,
) -> Iterator[Host]:
    """A host that has said where it serves; killed on the way out if it
    still runs, whatever the test did."""
    host = Host(apps, settings=settings, cwd=cwd, env=env, bind=bind)
    try:
        host.serving()
        yield host
    finally:
        host.close()
