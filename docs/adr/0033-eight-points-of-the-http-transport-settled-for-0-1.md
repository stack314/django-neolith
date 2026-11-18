# 0033. Eight points the HTTP transport's two halves showed, settled for 0.1: network-only refusals, framing, a token's lifetime, the reply's size and what a refusal may print

- **Status:** Accepted
- **Accepted:** 2026-11-19
- **Date:** 2026-11-19
- **Narrows:** [0011](0011-the-host-serves-contract-endpoints-itself.md) (Decisions 3, 4, 5 and 8 here) and [0017](0017-call-context-explicit-at-the-action-ambient-for-propagation.md) (Decision 1 here). Each gains a `Narrowed by 0033` line under its status (D2).

## Context

The HTTP connector (#128) and the provider endpoints (#129) were run against each other on one tree before either merged, with real loopback calls under WSGI and ASGI. Calls, replies, every error kind, tokens, the principal, deadlines, the idempotency key and a burst of 20 concurrent calls agreed. What was left, #133 lists: eight places where the code follows its record faithfully but a record says more, or less, than the two halves do together. None blocked #128 or #129. Each is settled here, in one decision, and five of them narrow a sentence of an Accepted record, quoted below as D2 asks.

## Decision

### 1. A provider's host may refuse, for a reason of the network alone, a request the caller's check passed

[0017](0017-call-context-explicit-at-the-action-ambient-for-propagation.md) Decision 9, caller step 2, reads: "A failure is a usage error, so a neolith caller never receives `invalid_argument` for its own request." Over HTTP a provider's host can still answer `invalid_argument` to a request that passed that check: a body above its size limit (spec 03, HTTP-40, a MAY; [0011](0011-the-host-serves-contract-endpoints-itself.md) row 12), or plain HTTP where it requires HTTPS (0011 row 5). In-process neither applies, so the same call succeeds there.

**The narrowing.** That sentence of 0017 now reads: "A failure is a usage error, so a neolith caller never receives `invalid_argument` for a request that fails this check, which is never sent. Over HTTP, a provider's host may still refuse a request that passed it, with `invalid_argument`, for a reason of the network alone: a body above the host's size limit, or plain HTTP where the host requires HTTPS." Spec 02's TRN-14 already says it of a request that fails the check ("never receives `invalid_argument` for one"), so its text is unchanged. These are the two refusals with `invalid_argument` that only a provider's host over HTTP makes. The other outcomes the specification places on the HTTP transport alone are `unauthenticated`: a call from an app that holds no signing key, which the caller refuses (spec 03, HTTP-18), and a call from an app the provider's host holds no key for (0027). When spec 06 states the conformance suite's rule that both transports give a call the same outcome (step 6, #13), its list of exceptions names these.

Alternatives weighed:

- **One limit for both modes:** the in-process transport would refuse a request above the host's limit too. It covers the size only, since HTTPS has no in-process counterpart, and it would refuse in-process calls that succeed today, for a limit that guards a network server's memory.
- **Another kind for these refusals**, such as `unavailable`. They are refusals of the request as sent, which is what `invalid_argument` names (spec 01), and spec 03 and 0011 already fix the kind.

### 2. A token lives 60 seconds, and the time before authentication counts against it

A caller mints a token just before it writes the request, with `exp = iat + 60` (0027 Decision 4), and a provider's host accepts it while `now < exp + 60` on its own clock (spec 03, HTTP-15). So a request that waits more than about 120 seconds between leaving the caller and being authenticated, queued behind a proxy or a full server, or with its body still arriving under ASGI, fails with `unauthenticated`, and less when the provider's clock runs ahead. Only a call with no deadline, or one longer than that, can wait so long; a request deadline of 30 seconds, the default, ends the call first with `timeout`. The lifetime stays 60 seconds, and the guide states the case. This narrows nothing: 0027 Decision 4 fixes the lifetime and says nothing of the wait.

Alternative weighed: **a lifetime of 300 seconds**, spec 03's bound. It moves the case to about 6 minutes, and a token seen on the network stays replayable for up to 6 minutes instead of 2 to 3, which 0027 Decision 4 already weighed and refused.

### 3. The host states `Content-Length` on every response but a 204

0011 Decision 4 lists the headers the host sets on a contract response, and ends: "The host sets no other header." The endpoint also states `Content-Length` on every response with a status other than 204, as Django's `CommonMiddleware` does for responses that pass it, since no middleware of the deployer runs on an owned path. Django's development servers answer a response that has no `Content-Length` with `Connection: close` (`core/servers/basehttp.py`, `ServerHandler.cleanup_headers`), so without it no caller could keep a connection for its next call.

**The narrowing.** That sentence of 0011 now reads: "The host sets no other header, apart from `Content-Length`, which states the body's length on every response but a 204 (RFC 9110, section 8.6)."

Alternative weighed: **leave framing to the server.** A server that buffers the body could state the length itself, but none of the servers 0.1 serves with does, and every reply would cost the caller a new connection (#134 records the 204 case, which keeps that cost).

### 4. A repeated body header under ASGI reaches the host as the ASGI server passes it

0011, under *A repeated body header*, reads: "Under ASGI a `Content-Type`, `Content-Encoding` or `Content-Length` sent twice reaches the host as one value joined with `,`, as RFC 9110 section 5.3 allows, and the joined value fails row 9, 10 or 11." Django joins what the ASGI server hands it, but a server can act first: uvicorn, in the run reported on #133, merged two equal `Content-Length` fields into one and answered two differing ones with its own 400.

**The narrowing.** That sentence of 0011 now reads: "Under ASGI a `Content-Type`, `Content-Encoding` or `Content-Length` sent twice reaches the host as the ASGI server passes it: Django joins the fields it receives with `,`, as RFC 9110 section 5.3 allows, and the joined value fails row 9, 10 or 11; a server may instead merge equal values into one, which is read as one, or answer the request itself, which reaches a neolith caller as `unavailable`."

Either way no request is read with a length other than the one its sender gave, which is what the row guards. Alternative weighed: **refuse a merged value too.** The host cannot see that a server merged two equal fields, and two equal lengths are not ambiguous (RFC 9112, section 6.3).

### 5. A `HEAD` answer on a well-known path may leave out `Content-Length`

0011 says of a well-known path, under its table: "`HEAD` gets the status and headers of `GET` and no body (RFC 9110, section 9.1)", and in Decision 7: "`HEAD` on the path gets the same headers and no body." Django's WSGI servers delete a `Content-Length` of 0 from a `HEAD` answer (`core/servers/basehttp.py`, `ServerHandler.cleanup_headers`, in 5.2.19 and 6.1.3), so the health path's `HEAD` answer has none there, and spec 03's HTTP-31 says so.

**The narrowing.** Both sentences of 0011 now end: "except that the answer may leave out `Content-Length`, as RFC 9110 section 9.3.2 allows a server to (spec 03, HTTP-31)."

Alternative weighed: **a server handler of the host's own that keeps the field.** `neolith run` could have one (#134 builds its own handler for other reasons), but `runserver` and the test live server cannot, and RFC 9110 allows the omission.

### 6. A caller sets no bound on a reply's size in 0.1

The client reads a reply, and an error body, in full (0028 open question 1). It sets no bound in 0.1. The in-process transport hands on a reply of any size, so a bound over HTTP alone would add another outcome that only the HTTP transport has, beside those Decision 1 names. The provider is one the deployer configured, whose host the call authenticates to, and the call's deadline bounds how long the reading takes. A bound needs a kind: spec 01 has none for "too large", and `internal` or `unavailable` would each say something else. 0028's open question 1 stays open for a record with a measured need, as it asks.

Alternative weighed: **a bound such as the provider's `DATA_UPLOAD_MAX_MEMORY_SIZE`, failing with `internal`** (the caller cannot read the reply, TRN-18). It guards the caller's memory against a provider that is broken or taken over, and makes the same call succeed in-process and fail over HTTP.

### 7. One rule for what a settings refusal may print

A refusal of a setting names where the refused input is (the setting, its key or its index) and the rule it breaks. It shows the input itself only where the setting admits a name or a number: a key of a `dict`, a `BACKEND` path, an entry of `INSTALLED_APPS` or `UNDECLARED_APPS`, `ROOT_URLCONF`, `REQUEST_DEADLINE`; there it shows it within the bound its record sets. Where the setting admits data, the refusal never shows the input, in whole or in part: a url, a secret, any value in `OPTIONS`, any value in `DATABASES`. It names the input's place, the rule, and at most its type. A record may show less than this rule allows, as 0027 shows no key of `SIGNING_SECRETS` outside its form and the routing record of step 6 shows no policy value.

This is the rule the Accepted records already follow, written once: [0022](0022-the-app-host-builds-settings-and-checks-before-it-serves.md) shows keys, `ROOT_URLCONF` and the request deadline; [0023](0023-a-request-principal-comes-from-ordered-backends.md) and [0014](0014-pluggable-connector-transports.md) show a `BACKEND` and a key; 0014 never shows a url and 0027 never a secret. `neolith.E005` showed a `DATABASES` value (#132), which #136 removed. So it narrows nothing.

Alternatives weighed:

- **Never show a refused input.** Safe, but a deployer who mistyped a `BACKEND` path or a key no longer sees what they wrote, and every table of 0022, 0023 and 0014 would change.
- **Decide by the value's form**, showing what looks like a name. A credential typed into the wrong place can look like a name; the place, which the setting fixes, cannot.

### 8. `Content-Length` is 1 to 19 digits

0011's row 11 admits `Content-Length` when it is "present, one field, `1*DIGIT` (RFC 9110, section 8.6)", refused with "the request body is sent with one Content-Length of digits". A longer value can reach Python's `int()` past its limit of 4300 digits for converting a string to an integer, which raises, and the call then ended as a 500 (#133).

**The narrowing.** Row 11 now admits `Content-Length` when it is "present, one field, 1 to 19 ASCII digits", refused with "the request body is sent with one Content-Length of 1 to 19 digits", as spec 03's HTTP-38 says. 19 digits hold every length a signed 64-bit integer can, the type HTTP implementations keep a length in.

Alternative weighed: **any number of digits below Python's limit**, compared as text. It admits lengths no server or client can hold, for no request a neolith caller sends.

## Consequences

- 0011 gains `Narrowed by 0033 (Decisions 3, 4, 5 and 8)` and 0017 gains `Narrowed by 0033 (Decision 1)` under their status lines. Nothing else in either changes.
- The guide (`docs/django-host.md`) states Decision 2's case beside the calls with no deadline. Decision 1's two refusals are named where the conformance suite lists the network-only exceptions to its parity rule, in spec 06, when that rule is written (step 6, #13).
- No code changes with this record: the code already does what each decision says, and #129 numbered and tested HTTP-31 and HTTP-38. A reply's size stays unbounded, and 0028 open question 1 open.
- A later settings row follows Decision 7: its record says which of its places admit names and which admit data.

Follows principles 5 (*same semantics over same mechanics*), 6 (*visible failure over transparent calls*) and 12 (*small core over broad features*).
