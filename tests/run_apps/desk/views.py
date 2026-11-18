"""The desk app's views.

A test holds a request in flight through two named pipes in the directory
that ``NEOLITH_RUN_PIPES`` names: the view writes a line to
``<name>.started`` once it runs, then reads one from ``<name>.release``
before it goes on. Each response but the streamed one states its length, so
the server can keep its connection alive.
"""

import os
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
    return str(_DESK.count(Tally(notes=0)).notes)


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
