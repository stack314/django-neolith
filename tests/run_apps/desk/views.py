"""The desk app's views.

Two views send a signal to the thread that serves them, as the kernel may
deliver a signal meant for the process to any of its threads.

A test holds a request in flight through two named pipes in the directory
that ``NEOLITH_RUN_PIPES`` names: the view writes a line to
``<name>.started`` once it runs, then reads one from ``<name>.release``
before it goes on. Each response but the streamed one states its length, so
the server can keep its connection alive.
"""

import os
import signal
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

from django.http import HttpRequest, HttpResponse, StreamingHttpResponse

from neolith import connect
from tests.run_apps.contracts import Tally

_DESK = connect("desk.v1")


def _sized(text: str) -> HttpResponse:
    response = HttpResponse(text)
    response["Content-Length"] = str(len(response.content))
    return response


def _held(name: str) -> None:
    pipes = Path(os.environ["NEOLITH_RUN_PIPES"])
    with (pipes / f"{name}.started").open("w", encoding="utf-8") as started:
        started.write("started\n")
    with (pipes / f"{name}.release").open(encoding="utf-8") as release:
        release.readline()


def _notes() -> str:
    tally = _DESK.count(Tally(notes=0))
    assert isinstance(tally, Tally)
    return str(tally.notes)


def ping(request: HttpRequest) -> HttpResponse:
    return _sized("pong")


def count(request: HttpRequest) -> HttpResponse:
    return _sized(_notes())


def hold(request: HttpRequest, name: str) -> HttpResponse:
    _held(name)
    return _sized(f"{name} {_notes()}")


def stream(request: HttpRequest, name: str) -> StreamingHttpResponse:
    def body() -> Iterator[str]:
        yield f"{name} "
        _held(name)
        yield _notes()

    return StreamingHttpResponse(body())


def signal_here(request: HttpRequest) -> HttpResponse:
    signal.pthread_kill(threading.get_ident(), signal.SIGTERM)
    return _sized("sent")


def signal_after(request: HttpRequest, name: str) -> HttpResponse:
    _held(name)
    signal.pthread_kill(threading.get_ident(), signal.SIGINT)
    return _sized(name)


def flood(request: HttpRequest, name: str) -> HttpResponse:
    """Write to standard error without end, so a cut request holds the
    stream's lock when the host stops."""
    pipes = Path(os.environ["NEOLITH_RUN_PIPES"])
    with (pipes / f"{name}.started").open("w", encoding="utf-8") as started:
        started.write("started\n")
    while True:
        sys.stderr.write("x" * 200 + "\n")
