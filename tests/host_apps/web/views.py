"""The web app's views, for the request-root tests: each calls the couriers
contract through ``tests.request_site.ask`` and answers with what came of it.
"""

import asyncio
import io
from collections.abc import AsyncIterator, Iterator

from django.http import (
    FileResponse,
    Http404,
    HttpRequest,
    HttpResponse,
    StreamingHttpResponse,
)
from django.views import View

from tests import request_site as site


def page(request: HttpRequest) -> HttpResponse:
    site.hold()
    return HttpResponse(site.ask())


async def apage(request: HttpRequest) -> HttpResponse:
    site.hold()
    return HttpResponse(site.ask())


class PageView(View):
    def get(self, request: HttpRequest) -> HttpResponse:
        return HttpResponse(site.ask())


class Endpoint:
    """A view that is an instance of a class of this app."""

    def __call__(self, request: HttpRequest) -> HttpResponse:
        return HttpResponse(site.ask())


def missing(request: HttpRequest) -> HttpResponse:
    raise Http404("nothing here")


def not_found(request: HttpRequest, exception: Exception) -> HttpResponse:
    return HttpResponse(site.ask(), status=404)


def _body() -> Iterator[bytes]:
    site.hold()
    yield b"<"
    yield site.ask().encode()
    yield b">"


async def _abody() -> AsyncIterator[bytes]:
    site.hold()
    yield b"<"
    yield site.ask().encode()
    yield b">"


def _streamed(request: HttpRequest, body: str) -> StreamingHttpResponse:
    content = _body() if body == "sync" else _abody()
    status = int(request.GET.get("status", "200"))
    return StreamingHttpResponse(content, status=status)


def stream(request: HttpRequest, body: str) -> StreamingHttpResponse:
    return _streamed(request, body)


async def astream(request: HttpRequest, body: str) -> StreamingHttpResponse:
    return _streamed(request, body)


def file(request: HttpRequest) -> FileResponse:
    site.hold()
    return FileResponse(io.BytesIO(b"a file"))


async def afile(request: HttpRequest) -> FileResponse:
    site.hold()
    return FileResponse(io.BytesIO(b"a file"))


async def _hanging() -> AsyncIterator[bytes]:
    site.hold()
    yield b"<"
    await asyncio.Event().wait()
    yield site.ask().encode()


def hang(request: HttpRequest) -> StreamingHttpResponse:
    return StreamingHttpResponse(_hanging())


async def ahang(request: HttpRequest) -> StreamingHttpResponse:
    return StreamingHttpResponse(_hanging())


def _failing() -> Iterator[bytes]:
    site.hold()
    yield b"<"
    raise RuntimeError("the body failed")


def fail(request: HttpRequest) -> StreamingHttpResponse:
    return StreamingHttpResponse(_failing())


class Pages:
    """Views that are bound methods of a class of this app."""

    def page(self, request: HttpRequest) -> HttpResponse:
        return HttpResponse(site.ask())


class AppReader:
    """A file-like object of this app whose ``read`` runs its code, as a
    storage backend's file may."""

    def __init__(self) -> None:
        self.done = False

    def read(self, size: int = -1) -> bytes:
        if self.done:
            return b""
        self.done = True
        return site.ask().encode()


def appfile(request: HttpRequest) -> FileResponse:
    site.hold()
    return FileResponse(AppReader())


async def aappfile(request: HttpRequest) -> FileResponse:
    site.hold()
    return FileResponse(AppReader())


def realfile(request: HttpRequest) -> FileResponse:
    site.hold()
    return FileResponse(open(__file__, "rb"))  # closed by Django


def _cleaned() -> Iterator[bytes]:
    try:
        yield b"<"
        yield b">"
    finally:
        site.ask()


async def _acleaned() -> AsyncIterator[bytes]:
    site.hold()
    try:
        yield b"<"
        await asyncio.Event().wait()
        yield b">"
    finally:
        site.ask()


def cleaned(request: HttpRequest) -> StreamingHttpResponse:
    return StreamingHttpResponse(_cleaned())


def acleaned(request: HttpRequest) -> StreamingHttpResponse:
    return StreamingHttpResponse(_acleaned())


async def _afailing() -> AsyncIterator[bytes]:
    site.hold()
    yield b"<"
    raise RuntimeError("the body failed")


async def afail(request: HttpRequest) -> StreamingHttpResponse:
    return StreamingHttpResponse(_afailing())
