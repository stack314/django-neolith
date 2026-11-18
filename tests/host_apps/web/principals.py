"""The web app's views for the principal tests (ADR 0023): each answers with
what a call from it carried, through ``tests.request_site.whom``.
"""

import asyncio
import threading
from collections.abc import AsyncIterator, Iterator

from asgiref.sync import sync_to_async
from django.contrib.auth import alogin, alogout, get_user_model, login, logout
from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.views.decorators.cache import cache_page
from django.views.decorators.vary import vary_on_headers

from tests import request_site as site

# How many times each view below ran, by name.
RAN: dict[str, int] = {}


def _ran(name: str) -> None:
    RAN[name] = RAN.get(name, 0) + 1


def quiet(request: HttpRequest) -> HttpResponse:
    """Makes no call."""
    return HttpResponse(b"quiet")


async def aquiet(request: HttpRequest) -> HttpResponse:
    return HttpResponse(b"quiet")


def who(request: HttpRequest) -> HttpResponse:
    """``?do=login&as=<name>`` or ``?do=logout`` first, then one call."""
    do = request.GET.get("do", "")
    if do == "login":
        user = get_user_model().objects.get(username=request.GET["as"])
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    elif do == "logout":
        logout(request)
    return HttpResponse(site.whom())


async def awho(request: HttpRequest) -> HttpResponse:
    """``?do=alogin&as=<name>`` or ``?do=alogout`` first, then one call,
    made in the event loop, or with ``?via=thread`` through
    ``sync_to_async``."""
    do = request.GET.get("do", "")
    if do == "alogin":
        user = await get_user_model().objects.aget(username=request.GET["as"])
        await alogin(request, user, backend="django.contrib.auth.backends.ModelBackend")
    elif do == "alogout":
        await alogout(request)
    if request.GET.get("via") == "thread":
        return HttpResponse(await sync_to_async(site.whom)())
    return HttpResponse(site.whom())


def _body() -> Iterator[bytes]:
    yield b"<"
    yield site.whom().encode()


async def _abody() -> AsyncIterator[bytes]:
    yield b"<"
    yield (await sync_to_async(site.whom)()).encode()


def stream(request: HttpRequest) -> StreamingHttpResponse:
    """A body whose first call is made while the server iterates it."""
    return StreamingHttpResponse(_body())


async def astream(request: HttpRequest) -> StreamingHttpResponse:
    return StreamingHttpResponse(_abody())


def bearer(request: HttpRequest) -> HttpResponse:
    """Puts the user an ``Authorization: Bearer <name>`` names on the
    request, as Django REST framework's ``Request.user`` setter does."""
    name = request.headers["Authorization"].removeprefix("Bearer ")
    request.user = get_user_model().objects.get(username=name)
    return HttpResponse(site.whom())


class Stranger:
    """An authenticated user that is not an instance of the user model,
    such as a stateless token's user."""

    is_authenticated = True
    pk = 7


def stranger(request: HttpRequest) -> HttpResponse:
    request.user = Stranger()  # type: ignore[assignment]
    return HttpResponse(site.whom())


def cached(request: HttpRequest) -> HttpResponse:
    """A page Django's cache middleware may store."""
    _ran("cached")
    return HttpResponse(site.whom())


@cache_page(60)
@vary_on_headers("Authorization", "Cookie")
def page_cached(request: HttpRequest) -> HttpResponse:
    """A page ``cache_page`` stores at the view, varied on the Django-user
    backend's inputs as the host notes say (docs/django-host.md)."""
    _ran("page_cached")
    return HttpResponse(site.whom())


def pair(request: HttpRequest) -> HttpResponse:
    """Two first calls at once, each from a thread of its own that copies
    the request's context, as an executor given a copy does."""
    import contextvars

    start = threading.Barrier(2, timeout=30)

    def one() -> None:
        start.wait()
        site.whom()

    threads = [
        threading.Thread(target=contextvars.copy_context().run, args=(one,))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return HttpResponse(b"pair")


async def apair(request: HttpRequest) -> HttpResponse:
    """Two first calls at once, each on a thread of its own."""
    first = sync_to_async(site.whom, thread_sensitive=False)
    second = sync_to_async(site.whom, thread_sensitive=False)
    await asyncio.gather(first(), second())
    return HttpResponse(b"pair")


async def alogout_stream(request: HttpRequest) -> StreamingHttpResponse:
    """Logs out, then returns a body whose call is made while the server
    iterates it, so the response phase resolves the principal: with
    ``aresolve`` when Django runs the principal middleware in async mode."""
    await alogout(request)
    return StreamingHttpResponse(_abody())
