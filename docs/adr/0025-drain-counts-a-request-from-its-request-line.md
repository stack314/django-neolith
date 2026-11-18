# 0025. Drain counts a request from its request line

- **Status:** Proposed
- **Date:** 2026-11-18

## Context

[0022](0022-the-app-host-builds-settings-and-checks-before-it-serves.md) Decision 10 says how `neolith run` counts the requests that Drain waits for: "The host wraps the WSGI callable it serves: the count goes up on entry and comes down in a `finally`, when the callable raises or when the server closes the response."

Django's development server reads a request's line and headers, and builds its environ, before it calls the WSGI callable (`core/servers/basehttp.py`, `WSGIRequestHandler.handle_one_request`, the same at 5.2.19 and 6.1.3). A request in that window has begun, so it is in flight in spec 05's sense (APP-41), and Drain must not end while it runs. A count that starts when the callable is entered is still 0 for such a request. Drain can then end, and the process stop, before the request is served, although it began before Drain did.

Decision 10 also gives the request handler subclass each connection's state ("knows whether its connection is between requests"), which Drain needs in order to close idle connections.

## Options

1. **Count at the callable, as 0022 says.** The window stays open: a request that began before Drain can be dropped unserved.
2. **Count in the request handler, from the request line to the handler's return, under the lock that holds each connection's state.** There is one count, and it covers the window.
3. **Both.** Two counts of one thing.

## Decision

Option 2. This narrows one sentence of 0022 Decision 10: "The host wraps the WSGI callable it serves: the count goes up on entry and comes down in a `finally`, when the callable raises or when the server closes the response."

In its place: the host's request handler counts a request as in flight from the moment its request line is read until the handler returns. That is after the server has closed the response, or after the request ended without one. The count is kept under the one lock that holds each connection's state, so a request line is either counted before Drain begins or refused after it. The rest of Decision 10 is unchanged, including that the count does not rest on Django's signal pair.

## Consequences

- Ending Drain never drops a request that began before it. A request whose line is read after Drain has begun is not served (spec 05, APP-40 and APP-41).
- The count rests on the request handler's `setup`, `parse_request`, `handle_one_request` and `finish`. 0022 Decision 10 already expects the host to lean on these internals to close connections, and the CI matrix runs both ends of [0012](0012-supported-python-and-django-versions.md).
