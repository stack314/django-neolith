# 0027. On an HTTP call, a caller backend names the calling app from a shared-secret token, and the principal header is read only after the allow-list admits that app

- **Status:** Accepted
- **Accepted:** 2026-11-18
- **Date:** 2026-11-18

## Context

Roadmap step 5 (#12) brings the HTTP transport: provider endpoints, OpenAPI at the well-known path, the connector over HTTP, "shared-secret tokens for the calling app, and the principal header". Two identities cross with each call ([0010](0010-pluggable-principal-resolution.md) Decision 1): the *calling app*, which the provider's host authenticates, and the optional *principal*, which the calling app asserts. Beside them travel the call-context fields that [0017](0017-call-context-explicit-at-the-action-ambient-for-propagation.md) makes ambient: the trace, the deadline and the idempotency key.

Much is already fixed, and this record builds on it rather than repeating it:

- **0010 (Accepted).** Backends combine in order, and the first that recognizes a call's credentials decides (Decision 6). Calls fail closed with `unauthenticated` (Decision 7). The shared-secret token identifies the calling app only, with `typ` `neolith+jwt`, a key id, one audience and a short expiry (Decision 5). The principal travels apart from the credential, and a provider accepts it only from calling apps on its allow-list (Decision 3).
- **0017 (Accepted).** One allow-list per provided contract, checked on every transport (Decision 6). The provider's order of checks: authenticate the calling app, then the allow-list, then the neolith headers, then the deadline, then the request (Decision 9). The forms of `Neolith-Timeout` and `Idempotency-Key` (Decision 3), and `traceparent` handling (Decision 4).
- **0021 (Accepted).** The allow-list is `Provided(..., principal_from=(...))`, a required tuple of app names with no wildcard (Decision 6). An app's name is its `label`, a `segment` of CON-10 (Decision 1).
- **0022 (Accepted).** The `NEOLITH` setting, the builder, set-up, and the rule that a key of `NEOLITH` may not change after set-up unless its record says so.
- **0023 (Accepted).** Request-side principal backends under `NEOLITH["PRINCIPAL_BACKENDS"]`. Its Open question 1 leaves to this step whether call-side backends are a second base class and whether they share that key. Its Open question 4 asks for one named exception for "a refused credential", to serve both sides.
- **Spec 03 (outline).** The headers, the principal object and its sent form, and the token's exact members and validity rules. Spec 02 has TRN-42 and TRN-43, which the in-process transport already meets (`src/neolith/_connector.py`, `InProcess._refusal`, at 0.1.0a2).

What is left, and what this record decides:

1. which part of the host authenticates a calling app, and under which setting (0023 Open question 1);
2. the one admitted form of every header the provider reads on a contract path, and what HTTP's own framing does to it on the servers Django runs under;
3. how a token is checked: constant-time comparison, the order of the checks, and no new runtime dependency;
4. where secrets live, how a secret is rotated with two valid keys, and how they are kept out of logs, error messages and Django's debug output;
5. how the principal header becomes a principal only after the calling app is admitted, so that an outside client can never name its own principal on a contract path;
6. which contract error each failure gives, with which message;
7. the named refusal exception (0023 Open question 4);
8. what a later record may add.

### What the code and libraries do

Django was read at 5.2.19 and 6.1.3, and the cited code is the same in both unless a line number differs. Paths are relative to the `django` package.

1. **Header names under ASGI.** `ASGIRequest.__init__` skips every header whose name holds `_` ("Prevent spoofing via ambiguity between underscores and hyphens"), maps the rest to `HTTP_` plus the upper-cased name with `-` as `_`, decodes values as Latin-1, and joins repeated fields with `,` (`core/handlers/asgi.py`, lines 88 to 103 in 5.2.19, 91 to 110 in 6.1.3).
2. **Header names under WSGI.** `runserver`'s handler deletes every header with `_` in its name before building the environ (`core/servers/basehttp.py`, `WSGIRequestHandler.get_environ`, line 215 in 5.2.19, 208 in 6.1.3), and wsgiref's `get_environ` beneath it joins a repeated `HTTP_` field with `,` (`wsgiref/simple_server.py`). gunicorn 27.0.1 does the same by default (`header_map = "drop"`, `gunicorn/config.py`, class `HeaderMap`, line 2879; `gunicorn/http/message.py`, line 285), and joins a repeated field into one `HTTP_` value with `,` (`gunicorn/http/wsgi.py`, lines 233 to 239). Other WSGI servers decide this for themselves. Apache's mod_wsgi does not pass `Authorization` to the app at all unless `WSGIPassAuthorization On` is set ([DRF's authentication guide](https://www.django-rest-framework.org/api-guide/authentication/) records the same), so behind it every call arrives with no credential. A platform or gateway in front of the host may also take `Authorization` for its own authentication: Cloud Run offers `X-Serverless-Authorization` to apps that use `Authorization` themselves, and Dapr carries its own token in `dapr-api-token`.
3. **Debug output.** `SafeExceptionReporterFilter.hidden_settings` is `API|AUTH|TOKEN|KEY|SECRET|PASS|SIGNATURE|HTTP_COOKIE`, case-insensitive. `cleanse_setting` replaces the value of any matching key with stars, and goes into dicts by key and into lists and tuples item by item (`views/debug.py`, lines 115 to 140 in 5.2.19, 119 to 144 in 6.1.3). `get_safe_request_meta` uses the same filter on `request.META`, so `HTTP_AUTHORIZATION` is hidden. `sensitive_variables` marks a function's locals, for sync and coroutine functions alike (`views/decorators/debug.py`), but the filter hides marked locals only when its `is_active(request)` is true, which by default is `settings.DEBUG is False` (`views/debug.py`, `is_active` at line 175 in 5.2.19 and 179 in 6.1.3). The technical 500 page is served only when `DEBUG` is true, so it lists them; its HTML (`get_traceback_html()`) shows each frame's locals, and its text (`get_traceback_text()`) shows none (probe, both versions). `cleanse_setting` and `get_safe_request_meta` apply whatever `DEBUG` says. `diffsettings` prints every setting with `repr`, with no filter (`core/management/commands/diffsettings.py`, line 6).
4. **Deploy checks.** `register(..., deploy=True)` adds a check that runs only under `check --deploy` (`core/checks/registry.py`), which is how Django's own security warnings are registered (`core/checks/security/base.py`, line 203 in 6.1.3).
5. **Response logging and error mail.** `BaseHandler` passes every response of 400 or more to `log_response`, which logs a 4xx at `WARNING` and a 5xx at `ERROR` on `django.request`, with the request in the record's `extra`, and skips a response whose `_has_been_logged` is true (`utils/log.py`, line 257 in 5.2.19, 291 in 6.1.3). `DEFAULT_LOGGING` sends the `django` logger to `console` and `mail_admins` (line 54 in 5.2.19, 56 in 6.1.3). `AdminEmailHandler` builds an `ExceptionReporter` from the request, whose report lists `request.META` through `get_safe_request_meta`, with the filter of point 3: `HTTP_AUTHORIZATION` and `HTTP_IDEMPOTENCY_KEY` are hidden, `HTTP_NEOLITH_PRINCIPAL` is not. The filter used is `request.exception_reporter_filter` when the request has one (`views/debug.py`, `get_exception_reporter_filter`, line 84 in 5.2.19, 88 in 6.1.3).

Python 3.13.5 (the standard library is the same on 3.12 for these points):

6. `hmac.compare_digest` compares without short-circuiting on content ([docs](https://docs.python.org/3/library/hmac.html#hmac.compare_digest)). `hmac.digest(key, msg, "sha256")` is the one-shot HMAC.
7. `base64.urlsafe_b64decode` accepts `+` and `/` as well as `-` and `_` (`b"AB+/"` and `b"AB-_"` decode alike), and ignores a newline inside the input (probe). It cannot be the strict decoder spec 03 asks for.
8. `json.loads` keeps the last of two equal member names, and admits `NaN`, a lone surrogate (`"\ud800"`) and an integer of any size (probe). `neolith._context.read_principal` already refuses all four: the first two through `object_pairs_hook` and `parse_constant`, the last two with its own checks after decoding.
9. `secrets.token_urlsafe(32)` returns 43 characters: 32 random bytes, base64url without padding (probe). Hexadecimal digits are all base64url characters, so a hex string is also base64url text: spec 03's example secret in hex, 64 characters, decodes as base64url to 48 other bytes with zero unused bits, and so does any `secrets.token_hex(32)` (probe). A hex string always has an even length.

PyJWT 2.10.1 was read as the obvious library for the token (MIT licence, `py.typed`, no required dependency, pure computation so sync or async does not arise, maintained). `base64url_decode` pads and calls `base64.urlsafe_b64decode`, so it inherits point 7 (`jwt/utils.py`, line 25). Header and claims are read with plain `json.loads`, so a duplicate member is taken silently (`jwt/api_jws.py`, line 280; `jwt/api_jwt.py`, line 188). `_validate_iat` reads `int(payload["iat"])`, so `true` and `1.9e9` pass as integers (`jwt/api_jwt.py`, line 320). It has no option to require an exact member set or a `typ`. Its time checks match spec 03's with a leeway of 60, except the bound `exp ≤ iat + 300`.

A probe (aarch64, Python 3.13.5) measured minting an HS256 token with the standard library at about 11 µs, and checking one at about 14 µs (20 000 runs each; `timeit`).

Forces:

- **One form per input** (the step's design gate, K2, APP-1): each header and setting in one form, everything else refused with a message that names it.
- **A wrong principal is worse than none** (0023). The principal carries the end user's authority down the call chain.
- **Secrets never leave the host's memory as text** (K9): not in logs, error bodies, refusals at startup, or Django's debug page.
- **Parity** (principle 5): the same refusals, kinds and messages in-process and over HTTP, where the transports allow.
- **No new runtime dependency without an Accepted record** (K5).

## Options

1. **Authenticate in the endpoint layer**, with its own authentication hook (django-ninja's `auth=` classes, or DRF's authentication classes). Familiar, but the order of 0017 Decision 9, the messages and the error body would then depend on that layer, and the in-process transport could not share any of it.
2. **A call-side backend seam owned by the host**, separate from the request-side one: a base class `CallerBackend`, a key `NEOLITH["CALLER_BACKENDS"]` in 0023's entry form, a shipped `SharedSecretBackend`, and host-owned steps for the allow-list and the headers that every backend shares. The endpoint layer calls these steps first.
3. **One backend class for both sides**, with a method for requests and a method for calls, under `PRINCIPAL_BACKENDS`. One word, one setting, as 0010 says "backend". The two jobs then share an order, and a backend that only knows one job must still implement or refuse the other.

## Decision

Option 2. Each decision below weighs its own alternatives.

### The admitted set

On the wire, as the provider's host reads it on a contract path, after the server's own handling (Context, points 1 and 2). Header names are compared ignoring case, as HTTP compares them (RFC 9110, section 5.1). `OWS` at either end of a value is not part of it and is removed (RFC 9110, section 5.5); nothing inside a value is changed. Every value is ASCII; a byte outside it fails the header's rule.

```
; the credential (step 1)
Authorization     = bearer 1*SP token                    ; recognized by the shared-secret backend only when
                                                         ; the first part's JSON has typ neolith+jwt (spec 03)
bearer            = "Bearer"                             ; any ASCII case (RFC 9110, section 11.1)
token             = part "." part "." part               ; at most 4096 bytes; spec 03's members and rules
part              = 1*b64char                            ; base64url, no padding, zero unused bits (spec 03)

; the principal (read at step 3, only after steps 1 and 2 pass)
Neolith-Principal = *b64char                             ; at most 4096 bytes; present when the field is present,
                                                         ; an empty value included (spec 03)

; the context (step 3)
Neolith-Timeout   = %x31-39 0*9DIGIT                     ; 0017 Decision 3
Idempotency-Key   = sf-string                            ; RFC 9651, 1 to 255 characters after unescaping,
                                                         ; no parameters (0017 Decision 3)
traceparent       = W3C Trace Context                    ; invalid or absent: a fresh trace (0017 Decision 4)
tracestate        = W3C list of 1 to 32 list-members     ; passed on at most 512 characters, longer cut by
                                                         ; whole entries (W3C 3.3.1.5); not a list: dropped;
                                                         ; never an error (Decision 6)

; nothing else
any other request header whose name starts with "neolith-"   ; ignored (Decision 6)
```

Django's settings (Decisions 3 and 4):

```
NEOLITH["CALLER_BACKENDS"]  = list of 1 or more entry    ; absent: no caller backend (Decision 1)
entry                       = dict with exactly "BACKEND" and "OPTIONS"   ; 0023 Decision 4's form
BACKEND                     = dotted path of a concrete subclass of neolith.callers.CallerBackend

; the shared-secret backend: BACKEND = "neolith.callers.SharedSecretBackend", listed at most once
OPTIONS                     = {"secrets": accepted}
accepted                    = dict of 1 or more app-name: keys      ; the calling apps this host accepts
keys                        = dict of 1 or 2 key-id: secret          ; 2 only while a key is rotated

NEOLITH["SIGNING_SECRETS"]  = dict of 1 or more label: signing      ; absent: this host signs for no app
label                       = a hosted app's label
signing                     = dict with exactly "KEY_ID": key-id and "SECRET": secret

app-name                    = segment of CON-10                      ; an app's name (0021 Decision 1)
key-id                      = 1*64 (ALPHA / DIGIT / "-" / "_" / ".")
secret                      = str of exactly 43 b64char, zero unused bits   ; 32 bytes, what
                                                                     ; secrets.token_urlsafe(32) gives
                              ; never spec 03's example secret, in its hex or base64url spelling;
                              ; one secret under at most one (app-name, key-id) in the host;
                              ; one (app-name, key-id) names at most one secret in the host
```

The code (Decisions 1 and 7):

```
CallerBackend.authenticate(self, request, contract)          -> exactly a str that is an app-name, or None
CallerBackend.aauthenticate(self, request, contract)         -> the same, awaited
                                                                ; request: the HttpRequest; contract: "<name>.v<N>"
raise neolith.CredentialRefused(message)                     ; "mine, and refused"; message is fixed text
```

### 1. Call-side backends are their own base class under their own key, and the host owns everything after them

`neolith.callers.CallerBackend` is an abstract base class with one job: name the calling app of an incoming contract call, or abstain. Its form follows 0023 Decision 2 where the jobs are alike:

- constructed once per set-up, with its `OPTIONS` as keyword arguments, shared by every thread;
- `authenticate(self, request, contract)`, a plain function, where `request` is Django's `HttpRequest` and `contract` the identity in the request path, such as `inventory.v1`;
- `aauthenticate`, which by default runs `authenticate` through asgiref's `sync_to_async`, as 0023's `aresolve` does. That default is thread-sensitive. Django opens one thread-sensitive context per ASGI request ([0011](0011-the-host-serves-contract-endpoints-itself.md), as amended for step 5), so the backend runs on the request's own thread: each call pays one thread hop, and calls of different requests do not wait for each other. The documentation says so: a backend with no I/O overrides `aauthenticate` to run inline, and a backend that does I/O overrides it with a native async version;
- it returns the calling app's name, or `None` to abstain; it raises `CredentialRefused` to refuse (Decision 7).

The host calls the backends of `NEOLITH["CALLER_BACKENDS"]` in order, and the first that does not abstain decides (0010 Decision 6). Everything after the calling app is named is the host's, not the backend's: the allow-list (Decision 5), the principal header, the context headers (Decision 6), the deadline and the request. So no backend can widen what a principal header admits, and every backend gets the same refusals.

**When a call arrives.** The host records the call's arrival before it calls the first backend, as `InProcess.call` takes `arrived = _context.now()` before `_refusal`, and counts the budget of `Neolith-Timeout` from that moment at step 4 (TRN-36; spec 03, "when the request arrives"). Time spent in a backend, or waiting for a thread to run it, comes off the call's budget rather than being added to it. 0011 enters the endpoint at that point (A1).

There is no `request_inputs` on a caller backend. 0023 needs it for `Vary`. A contract call is a `POST` whose response no shared cache stores without explicit freshness (RFC 9111, section 3), and the call's own scope, not a request root, holds its principal.

Alternatives weighed:

- **One class for both sides under `PRINCIPAL_BACKENDS`** (option 3). It keeps 0010's single word. But the two jobs end differently: on a request, nothing recognized means an anonymous visitor; on a call, it means `unauthenticated`. They read different inputs, a session or a user on one side and a credential header on the other, and an order chosen for one would bind the other. A project that adds an OIDC request backend would also, without meaning to, change who may call its contracts.
- **The endpoint layer's own authentication hook** (option 1). Least new code. The steps of 0017 Decision 9 and the messages of Decision 8 would live in a library's configuration, and a later change of endpoint layer (0011 is Proposed) would move them.
- **A narrowed view of the request in place of `HttpRequest`**, holding the headers and the identity. It would keep a backend from reading the body before the host has authenticated anything (spec 03). A later mutual-TLS backend needs the peer certificate, which Django exposes differently per server (an environ key under WSGI, the ASGI `tls` extension), or, where TLS ends at a proxy as it usually does, a header the proxy sets, such as Envoy's `x-forwarded-client-cert` (XFCC). The narrowed view would have to grow with every backend. `HttpRequest` is passed, and reading no body is a documented duty of a backend, as 0023 trusts `request_inputs`.
- **The backend also returns the principal.** 0010 and spec 03 say "a backend ... checks the principal". In 0.1 every backend would read the same bare assertion, so the host reads it once, after the allow-list. A later backend that carries a signed delegation token adds a method for it (Decision 10).

The cost: 0010's word "backend" now names two settings, `PRINCIPAL_BACKENDS` and `CALLER_BACKENDS`, which is the naming risk 0023 Open question 1 foresaw. The documentation of each key names the other.

### 2. The shared-secret backend is the standard library's HMAC under spec 03's rules, checked in a fixed order, with no new dependency

`SharedSecretBackend` implements spec 03's recognition and validity rules exactly, with `hmac`, `hashlib`, `base64` and `json` from the standard library, and the strict JSON reader the host already has for principals: `read_principal`'s hooks and the checks it makes after decoding (Context, point 8), so spec 03's whole I-JSON rule holds: no duplicate member name, no lone surrogate, no `NaN` or infinity, and integers within ±(2^53 − 1) with no fraction or exponent. Base64url is decoded by a function that first matches the text against `1*b64char`, then decodes and re-encodes to confirm zero unused bits, since `base64.urlsafe_b64decode` alone admits `+`, `/` and line breaks (Context, point 7).

**Recognition.** `Authorization` absent, another scheme, or a first part that does not decode, under spec 03's encoding rules and that I-JSON rule, to a JSON object whose `typ` is `neolith+jwt` or `application/neolith+jwt` (ignoring ASCII case): abstain (spec 03). So a first part with a duplicate `typ`, or a lone surrogate in `kid`, abstains, and a later backend can try the credential. A first part nested deeper than the host's JSON reader admits (RFC 8259, section 9, lets a parser limit nesting) counts as one that does not decode, so the backend abstains on it; this changes no valid token, whose first part is three string members with no nesting.

**The checks, in this order**, each failure a `CredentialRefused` with the text shown, so the caller sees `unauthenticated`:

| # | Check | Message |
|---|---|---|
| 1 | The whole value after the scheme is at most 4096 bytes and is three `part`s | `the token is not three base64url parts of at most 4096 bytes` |
| 2 | Header and claims decode to I-JSON objects (the whole rule above) with exactly spec 03's members, each of its type; `iat` and `exp` are JSON integers, not `true`, not `1.9e9` | `the token's header or claims do not have exactly spec 03's members and types` |
| 3 | `alg` is `HS256` | `the token's alg is not HS256` |
| 4 | `iss` is an app this host holds keys for | `the token's issuer is not an app this host accepts calls from` |
| 5 | `kid` names one of that app's keys | `the token's key id is not one this host holds for its issuer` |
| 6 | The signature part decodes to 32 bytes and equals `hmac.digest(secret, first-two-parts-as-sent, "sha256")` under `hmac.compare_digest` | `the token's signature does not verify` |
| 7 | `aud` is the identity in the request path | `the token is for another contract` |
| 8 | `iat < exp ≤ iat + 300` | `the token's lifetime is not between 1 and 300 seconds` |
| 9 | `iat ≤ now + 60` | `the token was issued in the future, beyond 60 seconds of clock difference` |
| 10 | `now < exp + 60` | `the token has expired` |

*now* is `int(time.time())` on the provider's host.

The order puts every claim the caller could only know from the secret (`aud`, the times) after the signature, so nothing about an unsigned token is judged except what finds its key. `iss` and `kid` select exactly one secret; the backend never tries each key in turn. A dictionary lookup can reveal whether an app or key id is known, which is not secret: app names cross hosts in allow-lists, and key ids are in every token. The comparison that is secret, the signature, is constant time. The length check in row 6 reveals only the length, which is fixed by `HS256`.

`aauthenticate` runs `authenticate` inline: the check is pure computation of about 14 µs (Context), shorter than a thread hop.

The check itself is a plain function of the header value, the identity, the key table and *now*, so tests give it any clock; the backend is the wiring around it, as 0023 Decision 2 arranges its own.

Alternatives weighed:

- **PyJWT 2.10.1** (Context). MIT, typed and without dependencies, so it meets K5's questions well, but K5 still asks for an Accepted record, and the library does not enforce spec 03: lenient base64url, last-wins duplicate members, `true` taken as an `iat`, no exact member set, no `typ` rule. Using it would mean checking the parts again around it, so the dependency would add a second reader of the same text with different rules.
- **Trying every key of the issuer** and accepting the first that verifies, as `SECRET_KEY_FALLBACKS` does for Django's signer. It needs no key id. Spec 03 makes `kid` required, and with it the trial would only add work and a second path.
- **One message for every token failure**, as OWASP's [Authentication Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html#authentication-and-error-messages) recommends and as Dapr answers a bad API token with one `invalid api token`. It reveals nothing at all to a prober. Rows 4 and 5 do reveal something to a caller with no secret: which app names a provider accepts calls from, and which key ids it holds for each. Both are configuration rather than secrets (app names cross hosts in allow-lists, and key ids are in every token sent), and the token proves nothing until row 6. This backend is for development (spec 03, 0010 Decision 5), where a precise message saves the most time, and that is why this record sets the common practice aside here. In 0.1 it is also the only backend shipped, so the trade is made for every adopter; a later production backend (Decision 10) may well choose one generic message, and its record decides.

### 3. Secrets live in `NEOLITH`, under names Django's debug filter hides, and are refused, never echoed, when malformed

**Provider side.** The calling apps a host accepts, with their keys, are the `secrets` option of the backend entry:

```python
NEOLITH = {
    "CALLER_BACKENDS": [
        {
            "BACKEND": "neolith.callers.SharedSecretBackend",
            "OPTIONS": {"secrets": {"orders": {"orders-2": env("ORDERS_KEY_2")}}},
        },
    ],
}
```

**Caller side.** Each hosted app that calls over HTTP signs with exactly one key:

```python
NEOLITH = {
    "SIGNING_SECRETS": {"orders": {"KEY_ID": "orders-2", "SECRET": env("ORDERS_KEY_2")}},
}
```

The deployer reads the values from wherever the project keeps secrets, as Django projects do for `SECRET_KEY`. Neolith reads no environment variable itself.

**One form for a secret:** a `str` of exactly 43 base64url characters with zero unused bits, which is 32 bytes. 32 is spec 03's minimum (RFC 7518, section 3.2) and the size of HMAC SHA-256's output; a longer key adds little, and above 64 bytes HMAC hashes it down to 32 first (RFC 2104, section 2). `secrets.token_urlsafe(32)` gives exactly this form, and every refusal message says so. A fixed length matters beyond tidiness: hex digits are base64url characters (Context, point 9), so a rule of "base64url decoding to 32 to 64 bytes" would admit a 64-character hex secret and read it as 48 other bytes, and an implementation that reads the same text as hex would then derive another key, with every call failing on its signature. A hex string has an even length, so it is never 43 characters, and a 64-character string of hex digits is refused with a message that says it looks like hex. `bytes`, padded text and any other length are refused too.

This is narrower than spec 03, which admits a secret of 32 bytes or more, and any number of keys (Decision 4). It is django-neolith's configuration, not the specification's (P2). The cost is between implementations: a key that another implementation of spec 03 holds at more than 32 bytes cannot be configured here, so the two sides agree on a 32-byte key.

Spec 03's published example secret is refused in both its spellings, the hex text the specification prints (`000102…1e1f`, 64 characters) and its base64url form (`AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8`, which has the admitted form), with a message that names it as the example, since anyone can read it.

**One secret, one key.** Within the host, one secret appears under at most one pair of app name and key id, and one pair names at most one secret. So the same secret under two app names, or under two key ids, is refused, since whoever holds it could sign as either; and the same app and key id with two different secrets is refused, since the host would sign tokens it then refuses. The same app, key id and secret appearing once in `SIGNING_SECRETS` and once in the backend's `secrets` is admitted: a host that signs as an app and also accepts that app's calls, as a test host calling its own live server does, or as one app attached in one host and detached in another would (roadmap step 8; 0016, Proposed), gives no one any power they did not have.

**Kept out of every output:**

- Both names contain `SECRET`, so Django's debug page and error reports show stars in place of their values (Context, point 3). `HTTP_AUTHORIZATION` is hidden by the same filter.
- A refusal at startup names the setting path, the app and the key id, and the rule, and never the value. This departs from 0022's habit of showing a refused `<value>`, for secrets only.
- After set-up the backend and the signer hold each secret's bytes in a private object whose `repr` is `<secret orders/orders-2>`, and read the bytes from it only as an argument of the `hmac.digest` call. The signer and the backend each handle secret bytes and token text inside one boundary: any failure there, in neolith's code or in a function of the standard library it hands them to (`hmac.digest`, the base64 and JSON readers), is raised again as a fixed error from outside the handler, in a frame that holds only the private secret object, the token wrapper, the claims or the request, never secret bytes or token text. The bytes and the text are handled only in functions that frame calls inside the `try`: the backend's boundary is `authenticate`, which takes the request, and the signer's takes the secret object and the claims and gets back the wrapper. So the error has no `__context__`, the frames that handled the bytes or the text are not on its traceback, and every frame that is, up to the deployer's view or the host's endpoint, holds none of them. A minted token is held in an object whose `repr` is `<token orders to inventory.v1>` until the HTTP transport writes the header, and the error a caller gets never carries the transport's exception or its frames (0028), which is the same boundary. None of this rests on `sensitive_variables`, whose marks Django honours only when `DEBUG` is false, while the technical 500 page that would show the locals is served only when it is true (Context, point 3).
- On a contract path, the host sets `request.exception_reporter_filter` to a subclass of Django's `SafeExceptionReporterFilter` that also hides every `HTTP_NEOLITH_` key and returns true from `is_active`, so that marked locals are hidden too whatever `DEBUG` says, before it calls the first backend. Django uses that filter for the technical 500 page and for the error mail that `mail_admins` sends with the request attached (Context, point 5), so the end user's principal never reaches `ADMINS`, whichever code logs with the request. 0011 also marks contract responses as logged, so `log_response` adds no line of its own (A1); the filter covers what that mark does not, such as an exception reported with the request by a deployer's own handler.
- No log line, error message or exception text written by the host or by `SharedSecretBackend` includes a secret, a token or any part of one, the `Authorization` value, or the `Neolith-Principal` value (the end user's data). Decision 8 lists what is logged. The host cannot make the same promise for a third-party `CallerBackend`: the text of its `CredentialRefused` goes to the caller as it is, and an unexpected exception from it is logged with its own text (Decision 7). The documentation states the duty of a backend's author: a refusal message is fixed text, and nothing the backend logs or raises holds a credential.
- `diffsettings` prints every setting unfiltered (Context, point 3), and so does any code that prints `settings.NEOLITH`. The host cannot change that; the documentation says so.

Neither key may change after set-up (0022's rule). Rotation is a deploy (Decision 4).

Alternatives weighed:

- **An environment variable named in the settings**, such as `"SECRET_ENV": "ORDERS_KEY"`, read by neolith. The value never sits in a settings object. Django keeps `SECRET_KEY` and database passwords as setting values, deployers already have tooling for that, and a second mechanism would be a second form.
- **Secrets as `bytes`.** Exact, but awkward to write in a settings module and to carry in an environment variable, and two forms once `str` is also wanted.
- **32 to 64 bytes of base64url.** Room for a longer key. It admits hex text as base64url (Context, point 9), and a longer key adds little under HMAC SHA-256.
- **A recognizable prefix**, such as `neolith_` followed by the 43 characters, as Stripe's `whsec_`, the Standard Webhooks `whsec_` and GitHub's token prefixes let secret scanners find a leaked key. Still one form, and it would rule out hex by construction. The value would then differ from the key's text that another implementation of spec 03 is configured with, for a backend that is for development only. The fixed length already rules out hex. A prefix is a candidate for the production backend's record (Decision 10).
- **The caller's key inside the backend entry.** One place for every secret. The caller's key is not a backend's input, a host that only calls would need a backend entry it does not use, and a host may hold several hosted apps that each sign.

### 4. A provider holds one or two keys per calling app; a caller signs with one key; tokens live 60 seconds

**Provider side:** one key per calling app, or two while that app's key is rotated. Three or more are refused with a message that says a rotation keeps two.

Two keys at the receiver, chosen by `kid`, is how JWKS key sets and Kubernetes' several service-account verification keys rotate ([Kubernetes](https://kubernetes.io/docs/reference/access-authn-authz/service-accounts-admin/)).

**Caller side:** one key per hosted app, used for every provider that app calls. The connector mints a token for every attempt it sends, just before it sends it, with `iss` the calling app, `aud` the identity, `iat = int(time.time())`, `exp = iat + 60`, and `kid` from `SIGNING_SECRETS`. It never reuses one. At about 11 µs a token (Context), a cache would save less than it would cost in state.

**Rotation**, from key A to key B, without downtime:

1. Add B beside A in the `secrets` of every provider that accepts the app, and deploy.
2. Change the app's `SIGNING_SECRETS` to B, and deploy.
3. When no process signs with A any more, wait 7 minutes, then remove A from every provider and deploy. 7 minutes is spec 03's bound: a host accepts a token for at most 420 seconds of its own clock, whatever lifetime a conforming caller chose.

Alternatives weighed:

- **Any number of keys**, as spec 03 permits and as Django's `SECRET_KEY_FALLBACKS` allows. More room for overlapping rotations. Each key kept is a secret still accepted, and a third key is usually one that was never removed.
- **Signing with two keys at the sender during a roll**, as Stripe does for webhook secrets for up to 7 days, sending one signature per secret ([Stripe](https://docs.stripe.com/webhooks)). A JWS compact token carries one signature, so this would mean two tokens or a second header. Two keys at the receiver need neither.
- **One signing key per calling app and remote provider**, with `SIGNING_SECRETS` keyed by app and by provider (or by contract identity, which is how 0014's remote provider configuration names a provider, A3). Spec 03 permits it: a calling app has one or more keys, and a provider checks only that `kid` names a key it holds for `iss`. Its gain is real: a provider that holds only the key `orders-p1` could no longer mint `orders` tokens that another provider accepts, so the trust cost spec 03 states ("any holder can mint tokens for that app") would stop at one provider. Its costs are a secret per pair of caller and provider, a rotation per pair, and a second level in the setting, whose key would have to name a provider that the remote provider configuration does not name by app (A3). For a backend that spec 03 and 0010 Decision 5 call development-only, one key per app is chosen, and per-provider keys are listed in Decision 10. Nothing on the provider side changes if a later record adds them.
- **A lifetime of 300 seconds**, spec 03's bound. Fewer clock-skew surprises, but a token seen on a network stays useful for up to 6 minutes. With 60, a provider whose clock runs up to 120 seconds ahead still accepts a fresh token. A token seen when it is minted stays usable for 2 minutes when the two clocks agree, and for up to 3 minutes when the provider's clock runs 60 seconds behind the caller's, the difference spec 03 allows (the provider accepts while `iat − 60 ≤ now < iat + 120` on its own clock). For comparison, Google ID tokens and Kubernetes service-account tokens are also unbound bearer tokens that rely on TLS, with lifetimes of an hour or more.
- **Reusing a token per (app, contract) while it is fresh**, as spec 03 allows. Fewer HMACs. Not needed at this cost, and it would add process-wide state (K6).

### 5. The principal header is read only after the calling app is named and the allow-list admits it

The provider's steps 1 to 3 of 0017 Decision 9, over HTTP:

1. **Calling app.** The backends run in order (Decision 1). No backend configured, all abstaining, or one refusing: `unauthenticated`. The calling app is the name returned, and nothing else: no header names it.
2. **Allow-list.** If the request has a `Neolith-Principal` field, with any value, the empty value included, and the calling app is not in the `principal_from` of the called contract's `Provided`, the call fails with `permission_denied`. The header's value is not read. This is the same function the in-process transport uses (`InProcess._refusal`), given the calling app, the identity and whether a principal is asserted, so the message is the same in both modes: `app <caller> may not assert a principal on a call to <identity>: the contract's allow-list does not name it`.
3. **Reading it.** Only now is the header decoded and read with `read_principal`, the check a caller already runs on its own sent form. A failure is `invalid_argument`. The principal that comes back is the one the action's context holds.

So an outside client meets one of three ends: with no token it is `unauthenticated` before any neolith header is read; with a token of an app that is not listed it is `permission_denied` whatever it asserts; and the principal of an allow-listed app is that app's assertion, as 0010 and 0017 intend.

Two more points concern other ways the header could reach a principal:

- **No request root on a contract path supplies the call's principal.** The action runs in the scope the transport opens from the headers (0017 Decision 4), and its calls derive from that scope. The request-side backends of 0023 are therefore not asked for a call's principal, and a session cookie on a contract path counts for nothing.
- **A request-side backend should not read a neolith header, and the host warns when one declares it.** A project's own `PrincipalBackend` that read `Neolith-Principal` on an ordinary page would turn it into a principal from anyone. 0023's admitted set lets `request_inputs` be any tuple of header names, and its Decision 3 says the host trusts the declaration, so this record does not refuse one: a refusal would narrow an Accepted decision, which needs D2's quoting and a `Narrowed by` line, and it would still catch only what a backend declares, not a backend that reads `request.headers` without declaring it or that declares `("*",)`. Instead, a set-up warning, in the host's `neolith.W` series, names a backend whose `request_inputs` lists a header starting with `neolith-` and says that the principal header is meant to be read only from a calling app the allow-list admits. The documentation of `PrincipalBackend` states the duty plainly: a backend never takes a principal from a `neolith-` header, as Django's documentation warns that `RemoteUserMiddleware` must not be used where a client can supply its header.

Alternatives weighed:

- **Read and validate the header first, then check the allow-list.** A malformed header would then fail with `invalid_argument` before a call from an unlisted app is refused, which is the order 0017 Decision 9 and TRN-43 ("whatever the principal holds") rule out, and which reads the end user's data from an app not trusted with it.
- **Drop an assertion from an unlisted app and run the call with no principal.** Gentler for a misconfigured caller. The action would then run for a different audience than the caller meant, silently (principle 6).
- **Refuse at set-up a `PrincipalBackend` whose `request_inputs` names a `neolith-` header.** Firmer for the case it sees. It adds a row to 0023 Decision 10's table of refusals, so it narrows 0023's admitted set and would need its own decision under D2, quoting 0023 Decision 3 and adding `Narrowed by` to 0023. Since the host trusts the declaration, it would also suggest a protection it cannot give. A warning and a documented duty say what is true.
- **Say nothing.** Smallest. The danger is concrete, and a warning costs one check.

### 6. Repeated fields, underscores and unknown neolith headers each have one outcome

- **A repeated field** reaches Django as one value joined with `,` (Context, points 1 and 2). No single-valued form above admits a `,` outside an sf-string, so a repeated `Neolith-Principal`, `Neolith-Timeout` or `traceparent` fails its own rule, and a repeated `Authorization` that the shared-secret backend recognizes fails row 1 of Decision 2. `tracestate` is a list and joins as one (W3C Trace Context, section 3.3.1.1).
- **A name spelled with `_`** is dropped by Django under ASGI, by `runserver`, and by gunicorn's default (Context, points 1 and 2), so the host never sees it there. A WSGI server that passes it gives Django the same `HTTP_` key as the hyphenated name, and the host cannot tell them apart. That is a known gap, not a second form: whatever arrives is still read only under Decision 5's order.
- **Any other request header whose name starts with `neolith-`** is ignored: the host reads nothing from it, passes it nowhere, and logs its name at `DEBUG` on `neolith.calls` (the name when it matches `neolith-` followed by `1*(ALPHA / DIGIT / "-")`, otherwise the word `malformed`). This is what RFC 9110, section 5.1, asks of a recipient that is not a proxy ("SHOULD ignore unrecognized header and trailer fields"), so that HTTP "can be extended without updating or removing deployed intermediaries". 0017 Decision 9's step 3 reads exactly the principal, the timeout and the idempotency key, and spec 03 refuses no other header. Since the host never passes an incoming header on, ignoring and stripping (as Envoy strips its internal `x-envoy-*` headers from untrusted requests) come to the same thing here. The `Neolith-` prefix stays reserved for spec 03 (spec change list).
- **`tracestate`** is passed on when it parses as W3C's list of 1 to 32 list-members (section 3.3.1.1). When the list is longer than 512 characters, the host shortens it as W3C section 3.3.1.5 asks: it removes whole entries only, those longer than 128 characters first, then entries from the end, until it is at most 512 characters. 512 is the size W3C asks a vendor to propagate at least, and the documentation states it as the host's maximum, as the same section asks. A value that is not such a list is dropped, which W3C permits (section 4.3), and never fails the call. It is dropped too when `traceparent` is invalid or absent (0017 Decision 4). Each input still has one outcome. Shortening and dropping narrow one sentence of 0017 Decision 4, which Decision 11 quotes.

Alternatives weighed:

- **Refuse unknown `neolith-` headers** with `invalid_argument`. A caller that sends a header it expects the provider to honour would learn that this provider does not (principle 6). But detached apps are deployed on their own (0002), so a 0.2 caller that adds any `Neolith-` header would fail every call to a 0.1 provider, whether or not the header mattered to that call; it departs from RFC 9110's SHOULD; and it would need a spec requirement and a version marker decided now. A later header whose being ignored would change a call's meaning can bring its own way of being refused by a provider that does not know it (Open question 1).
- **Drop a `tracestate` longer than 512 characters whole.** Simpler, also one outcome per input, and W3C permits dropping a value it cannot use. It throws away every vendor's entry for a list that W3C gives a rule to shorten, which section 3.3.1.5 calls the interoperable way.
- **Read raw ASGI headers to refuse an underscore spelling.** Possible under ASGI only, so the two server kinds would differ.

### 7. `neolith.CredentialRefused` names a refusal on both sides

`neolith.CredentialRefused(message)` is an `Exception` a backend raises when it recognizes credentials and refuses them. Its message is fixed text that shows no part of a credential.

- **Caller side of the seam (this record).** The call fails with `unauthenticated`, and the message is the exception's. The host logs it at `WARNING`, on the one line 0011 Decision 5 writes for each contract response.
- **Request side (0023).** Nothing changes for callers: every call from the root still fails with `internal`, as 0017 and 0023 Decision 5 fix. The host logs it at `WARNING` instead of `ERROR`, which is what 0023 Open question 4 asked for.
- **Any other `Exception` from a caller backend**, or a return value that is not exactly `None` or a `str` matching `app-name`, is a failure of the host: `internal`, with 0008's fixed message, logged at `ERROR`. Because spec 03 has the host authenticate before it reads `traceparent`, the trace identifier in that message and log line is a fresh one, generated for the purpose.

Alternatives weighed:

- **A return value for refusal**, as ASP.NET Core's `AuthenticateResult.Fail`. Typed, but 0023's backends already raise to refuse, and a raised exception carries its message without a wrapper type.
- **Use Django's `PermissionDenied`.** Familiar, but its name says "denied", which on this side is `permission_denied`, the other kind.

### 8. Every failure has one kind, one status and one message, and the log never holds a credential

| Step | Failure | Kind (status) | Message to the caller |
|---|---|---|---|
| 1 | The host has no caller backend | `unauthenticated` (401) | `this host has no caller backend, so it accepts no calls over HTTP` |
| 0 | The call is made from a scope of no app (caller side, Decision 9) | `unauthenticated`, not sent | the in-process text of TRN-42 (`InProcess._refusal`) |
| 1 | No backend recognizes the credentials, and the request has no `Authorization` | `unauthenticated` (401) | `the call carries no Authorization header; if the caller sent one, a server or gateway in front of this host may have removed it` |
| 1 | No backend recognizes the credentials, and the request has `Authorization` | `unauthenticated` (401) | `the call carries no credential this host recognizes` |
| 1 | A backend refuses (Decision 7), including each row of Decision 2 | `unauthenticated` (401) | the refusal's message |
| 1 | A backend fails otherwise (Decision 7) | `internal` (500) | 0008's fixed text with a fresh trace identifier |
| 2 | `Neolith-Principal` present, calling app not on the allow-list | `permission_denied` (403) | the in-process text (Decision 5) |
| 3 | `Neolith-Principal` not a principal object, encoded as spec 03 says | `invalid_argument` (400) | `Neolith-Principal: ` and `read_principal`'s rule, which never shows a value |
| 3 | `Neolith-Timeout` or `Idempotency-Key` outside its form | `invalid_argument` (400) | `<Name>: ` and the form |
| 3 | An unknown `neolith-` header | none | the call goes on, as Decision 6 says |
| 3 | `traceparent` or `tracestate` outside its form | none | the call goes on, as Decision 6 says |

The split between `unauthenticated` and `permission_denied` is gRPC's: `PERMISSION_DENIED` "must not be used if the caller can not be identified" ([gRPC status codes](https://grpc.io/docs/guides/status-codes/)). The message for a request with no `Authorization` names the usual cause on the provider's side, a server or gateway that removed it (Context, point 2), so that the hint is in the message and not only in the documentation.

Every 401 carries exactly `WWW-Authenticate: Bearer realm="neolith"`, with no `error` parameter, so the header has one form whichever case it is. RFC 6750, section 3, says the `Bearer` scheme "MUST be followed by one or more auth-param values", and its example for a request with no credential is `Bearer realm="example"` (section 3.1); spec 03 cites that section for its challenge. RFC 6750 makes `error="invalid_token"` optional, and section 3.1 asks for no error code when no credential was sent; the message in the body already says which case it is, so one form serves every case. 403 and 400 carry no challenge.

**What is logged.** 0011 Decision 5 writes one line on the logger `neolith.calls` for each contract response, with the identity. A refusal at steps 1 to 3 adds to that line the step, the rule, and, at step 1, the token's `iss` and `kid` only when they match `app-name` and `key-id`, otherwise the word `malformed`, so a crafted value cannot write into the log. Never: the `Authorization` value, any part of a token, a secret, or the `Neolith-Principal` value. 0011 marks that response logged, so Django's `log_response` adds no line, and with it no request for `mail_admins` (A1; Context, point 5); the reporter filter of Decision 3 covers any other report made with the request.

### 9. The caller sends each header in exactly one form

The connector's HTTP transport asks the host for the headers of one attempt and sends exactly these, with these spellings, in the order the HTTP client record (0028) fixes:

- `Authorization: Bearer <token>`, one space, a fresh token (Decision 4);
- `Neolith-Principal: <base64url of the sent form>`, only when the call has a principal;
- `traceparent: 00-<trace-id>-<span-id>-<flags>`, always version `00`;
- `tracestate: <value>`, only when it is not empty;
- `Neolith-Timeout: <ms>`, only when the call has a deadline;
- `Idempotency-Key: <sf-string>`, the key serialized as an RFC 9651 String (section 4.1.6).

Before it sends anything, the HTTP transport checks the calling app as the in-process transport does:

- **A call made from a scope of no app** (TRN-42, TRN-56) fails with `unauthenticated` and the in-process message, `a call to <identity> has no calling app; a call is made from a scope that belongs to an app`, from the same function `InProcess._refusal` uses. There is no app to sign for, and sending without a credential would give the provider's other message (Decision 8), so the two modes would differ for one call.
- **A calling app with no `SIGNING_SECRETS` entry** fails with `unauthenticated`, `app <caller> has no signing key in NEOLITH["SIGNING_SECRETS"], so it cannot call <identity> over HTTP`. The host does not send a call it knows will be refused.

The second case is also refused before Serve, where the host can see it (K2): for each contract identity in the remote provider configuration, every hosted app that requires it needs a `SIGNING_SECRETS` entry, and the refusal names the app and the identity. That configuration is `NEOLITH["REMOTE_PROVIDERS"]`, which 0014, as amended for step 5, defines (A3). The set-up refusal follows that key: if 0014 is accepted in another form, the row follows it, and until it is accepted the check at the first call above is the only one.

### 10. What a later record may add, without this record deciding it

Each of these is an addition beside what is decided here, not a change to it:

- **A mutual-TLS caller backend**, naming the calling app from a workload identity such as a SPIFFE ID in the client certificate, as Dapr takes a caller's app id from its mTLS certificate. It is one more `CallerBackend` in the list; the principal header still follows Decision 5. Where TLS ends at a proxy, the certificate reaches the app as a header the proxy sets, such as `x-forwarded-client-cert`. A backend that reads such a header carries the risk Django's documentation names for `RemoteUserMiddleware`, which "must not be deployed in configurations where a client can supply the header". It would need (a) a trusted-proxy statement in its options, (b) documentation that the edge must clean that header, as Envoy does by default and replaces under `SANITIZE_SET`, and (c) the underscore caveat of Decision 6, since `X-Forwarded_Client_Cert` and `X-Forwarded-Client-Cert` map to one `META` key on servers that pass both.
- **A signed delegation token** in `Neolith-Principal`, in the style of RFC 8693, in place of the bare assertion, as 0010 Decision 3 and spec 03 foresee. It would add an optional method to `CallerBackend` that reads the header for the backend that named the caller, called at step 3, with the bare reading as its default.
- **Binding the principal header, and possibly the body, to the credential.** Today `Neolith-Principal` is not covered by the token, so a token seen on the network can be replayed within its window with any principal and any body, the trade spec 03 accepts for the development backend. AWS SigV4 signs the method, path, a hash of the payload and the listed headers; Stripe signs `timestamp.body`; RFC 9421 HTTP Message Signatures and DPoP's `ath`, `htm` and `htu` claims (RFC 9449) bind headers and method. Any of these changes spec 03's exact member set or adds a header.
- **Asymmetric signatures** (such as EdDSA), so that a provider holds only a public key and can no longer mint tokens for the apps it accepts, which closes the trust gap spec 03 states.
- **One signing key per calling app and remote provider** (Decision 4's alternative), which limits that gap to one provider without changing the provider side.
- **OAuth 2.0 client credentials or OIDC** as caller backends.
- **A replay cache** and **a per-provider audience**. A replay cache needs a token identifier, such as a `jti` claim, which spec 03's exact member set does not have, and spec 03 says a provider does not refuse a token it has seen before; both would change.
- **A per-contract allow-list of calling apps**, denying by default, in the style of Dapr's access control lists (app id, operations, HTTP verb) or Istio's `AuthorizationPolicy`. It would be separate from `principal_from` and checked at step 2 before the principal check, so it adds a check without changing Decision 5. In 0.1 a key at a host admits calls to every contract that host provides (Consequences).
- **A challenge per backend.** In 0.1 every 401 carries the one challenge of Decision 8. DRF takes the challenge from the first authentication class, and RFC 9110, section 11.6.1, allows several challenges in one `WWW-Authenticate`. A later non-`Bearer` backend may contribute its own challenge, for example through an optional attribute on `CallerBackend`, so that the one-form rule holds per configured list of backends.
- **A secret with a recognizable prefix** for secret scanners (Decision 3's alternative).
- **A caller-side credential seam**, if a second credential needs a hook on the caller too.

### 11. A separate decision: 0017 Decision 4 is narrowed for `tracestate`

This decision can be taken or left apart from the rest. It changes what an Accepted record says, so it is made as that change (D2), not as a reading of 0017.

**What 0017 says.** Decision 4, on the provider side of each transport: "When `traceparent` is absent or invalid, it starts a fresh trace and drops `tracestate`; otherwise it passes `tracestate` on."

**The narrowing.** With this record Accepted, the second half reads: otherwise it passes `tracestate` on when the value is a W3C list of 1 to 32 list-members, shortened by whole entries to at most 512 characters as W3C Trace Context section 3.3.1.5 asks, and drops a value that is not such a list (Decision 6). The pull request that marks this record Accepted adds one line under 0017's status, `Narrowed by 0027 (Decision 11)`, with a link here. Nothing else in 0017 changes. Spec 03's matching sentence is outline and changes with the spec delta of step 5.

**What is traded.** 0017 as written passes on every value that comes with a valid `traceparent`, so no vendor's entry is ever lost. With the narrowing, a list longer than W3C asks a vendor to carry loses whole entries, and a value that is not a list is lost entirely. What is gained: every value the host passes on, and every value a neolith caller sends, has the one form the caller's admitted set names, and no header the host writes depends on bytes it could not read.

**What rests on it.** Decision 6's `tracestate` bullet, the `tracestate` line of the admitted set, and the client record's (0028) bound of 512 characters on what a caller sends. Without it, the host passes on any value with a valid `traceparent`, and the caller sends what it received.

Follows principles 5 (*same semantics over same mechanics*), 6 (*visible failure over transparent calls*), 10 (*Django-native over framework-new*) and 12 (*small core over broad features*).

## Assumptions about the other step 5 records

Three other records are proposed for step 5: [0011](0011-the-host-serves-contract-endpoints-itself.md) and [0014](0014-pluggable-connector-transports.md), both amended, and 0028, the HTTP client. None is Accepted. This record does not decide their questions; it assumes the following, and changes if they decide otherwise.

- **A1. Provider endpoints (0011).** The host's endpoint layer serves each contract path itself, runs the steps of Decision 5 before it parses or validates the body, whatever serves the path, and uses no library's own authentication hook. It records the call's arrival when it is entered, before the first backend runs (Decision 1). It renders every failure as spec 03's error body (0008 Decision 8). The deployer's middleware, CSRF's included, does not run on a contract path, which suits a credential that a browser never attaches on its own, as Stripe's Django guidance exempts its webhook view from CSRF; it sends no CORS headers. It logs each contract response itself and marks it logged, so `log_response` adds no line (Decision 8). The well-known paths (OpenAPI, health) are not contract paths, so they are not authenticated (0010 Decision 7).
- **A2. HTTP client (0028).** The client sends the headers of Decision 9 and none of its own credentials or cookies, and does not follow redirects on a contract call, so a token never reaches another origin, as Stripe treats a redirect as a failed delivery.
- **A3. Remote providers (0014).** The configuration that says which contract identities go over HTTP is `NEOLITH["REMOTE_PROVIDERS"]`, keyed by contract identity, as 0014 is amended for step 5. Its own A3 expects this record to configure signing keys per calling app and to own the refusal of a host that has a remote provider for an app with no key. Decision 9's set-up refusal follows that key, and falls back to the check before sending.

## Consequences

- An outside client cannot name its own principal on a contract path (Decision 5). On an ordinary page, whether a header can become a principal is up to the project's request-side backends: the host warns about a backend that declares a `neolith-` header, and catches only what a backend declares (Decision 5); the rest is a documented duty of the backend's author.
- In-process and HTTP share the allow-list function and its message, so a call refused for its principal is refused alike in both modes, and so is a call from a scope of no app (Decision 9). Over HTTP, the key table is also a list of who may call at all, which the in-process transport has no counterpart for: a hosted app may call any contract it requires, while a remote app without a key at the provider gets `unauthenticated`. In 0.1 a key at a host admits calls to every contract that host provides; only the principal is governed per contract (Decision 10 lists a per-contract allow-list of calling apps). This is a difference of configuration, not of the call's meaning, and the documentation says so.
- That difference has one case no startup check catches. When step 6 brings failover between an attached and a detached provider, the same call can succeed in-process and fail with `unauthenticated` over HTTP when the detached provider's host holds no key for the calling app, which is the kind of outcome 0017 Decision 6 kept the allow-list from having. Decision 9's set-up refusal checks only the caller's own signing key, never what a provider holds. The documentation names the case, and Open question 5 keeps a probe as a candidate.
- No new runtime dependency (K5). The token code is about one hundred lines, checked by the tests below instead of by a library's.
- The trust costs of 0010 and spec 03 remain and are restated in the documentation: every provider that accepts an app holds its secret and can mint tokens as that app (one key per app is chosen, Decision 4); the token is an unbound bearer token, as Google ID tokens and Kubernetes service-account tokens are, so one seen on the network is replayable, with any principal and any body, for 2 minutes when the clocks agree and up to 3 minutes at the 60-second difference spec 03 allows; an allow-listed app can assert any principal. The `check --deploy` series gains a warning when `SharedSecretBackend` is listed, saying that it is for development (Context, point 4). A remote provider at a plain `http` url other than loopback gets 0014's warning, `neolith.W002`, since RFC 6750, section 5.3, asks for TLS with bearer tokens.
- The documentation names the deployments that keep `Authorization` from the host: mod_wsgi without `WSGIPassAuthorization On`, and a platform, gateway or mesh that uses `Authorization` for its own authentication on the same port (Cloud Run IAM, an API gateway, an Istio `RequestAuthentication`). It tells deployers to keep the gateway's own credential off `Authorization` on contract paths, as Cloud Run's `X-Serverless-Authorization` allows. Spec 03 fixes `Authorization: Bearer` for the shared-secret backend, so this record cannot move the token (Open question 6).
- Under ASGI, a backend that does not override `aauthenticate` pays one hop to the request's own thread on each call (Decision 1). `SharedSecretBackend` overrides it.
- A host that serves contract paths and lists no caller backend starts, and every call over HTTP fails with `unauthenticated`. A set-up warning says so, since 0010 Decision 7 admits that host.
- Step 5's public surface gains `neolith.callers.CallerBackend`, `neolith.callers.SharedSecretBackend`, `neolith.CredentialRefused`, the `CALLER_BACKENDS` and `SIGNING_SECRETS` keys, and their check ids.
- 0023's Open questions 1 and 4 are settled here. This record narrows one sentence of 0017 Decision 4 (Decision 11), and no decision of 0023: request-side backends keep 0023's admitted set, and Decision 5 adds a warning, not a refusal. 0023's text is not edited, and the pull request that adds this record removes nothing from it, since its open questions are not decisions.
- **Startup refusals**, with ids from the `neolith.E` series fixed by the pull request. Builder: each key's shape as in the admitted set; a `SIGNING_SECRETS` label that is not a hosted app; a malformed key id or secret, with its own message for a 64-character hex string. Set-up: 0023 Decision 10's rows for `CALLER_BACKENDS`, with `CallerBackend` as the base; `SharedSecretBackend` listed twice; its option other than `secrets`; an app name outside `app-name`; zero or three or more keys; spec 03's example secret in either spelling; one secret under two app names or two key ids; one app and key id with two secrets; a hosted app that requires a contract in `REMOTE_PROVIDERS` without a signing key (conditional on A3). Every message names the path and the rule and never a secret. **Warnings**, with working ids fixed by the pull request that adds each: `neolith.W004`, no caller backend on a host that serves contract paths; `neolith.W005`, a `PrincipalBackend` that declares a `neolith-` header; `neolith.W006`, under `check --deploy`, `SharedSecretBackend` listed.
- **Specification changes**, in neutral terms and with new HTTP requirement IDs: the `OWS` rule; unknown `Neolith-` headers ignored and the prefix reserved; the `tracestate` limit and truncation; the 401 challenge in exactly one form, `Bearer realm="neolith"`; the arrival time taken before authentication; the caller's refusal of a call from no app before sending; the steps of Decision 5 as requirements (most already exist as TRN-42, TRN-43 and spec 03's outline); and each item of spec 03's validity list for a shared-secret token as one requirement, with the refusal without fallback to a later backend as its own. The 60-second lifetime, the two-key limit, the 43-character secret and the settings are django-neolith's, not the specification's (P2).
- **Tests.**
  - Conformance: spec 03's example token verifies under its example secret at its `iat`, and fails at `exp + 60`; each row of Decision 2 and each row of Decision 8 gives its kind, status, message and challenge; a call from an unlisted app with a malformed principal header gets `permission_denied`, not `invalid_argument`; the same call in-process and over HTTP gets the same kind and message, including a call from a scope of no app, with one host that both signs as an app and accepts that app's calls under the same key; an unknown `Neolith-` header changes nothing; a `tracestate` over 512 characters is cut as W3C section 3.3.1.5 says.
  - Hypothesis: tokens drawn from the admitted set verify, and one mutation of one byte, member, type or time gives the row it breaks; secrets drawn from `token_urlsafe(32)` are admitted, and padded, hex (`token_hex(16)` to `token_hex(64)`), short, long, `bytes` and alphabet-mixed ones are refused by name; spec 03's example secret is refused in both spellings.
  - Repeated fields joined with `,`, as Django joins them under ASGI, and runserver (wsgiref) and gunicorn under WSGI, give the outcomes of Decision 6.
  - The deadline: with a test-only backend that sleeps, the budget the action sees is counted from before the backend ran.
  - No output holds a secret: a test captures every log record, refusal message, error body, the technical 500 page's HTML (`get_traceback_html()`) under `DEBUG = True` and the `mail_admins` message, for a failing call (an `internal` on a contract path included, a failure raised inside `hmac.digest` in the signer and in the backend, and one raised inside the HTTP client under a caller's view) and for a refused setting, and asserts that no secret, principal value, token or any part of a token, the signing input included, occurs in any of them. The page's text form is not enough, since it lists no locals.
  - The constant-time comparison is shown by reading the code, not by timing: timing tests are not reliable on shared machines (T3).

## Open questions

1. **A `Neolith-` header a provider must understand.** Decision 6 ignores unknown ones in 0.1, so a newer caller can roll out before its providers. If the specification later adds a header whose being ignored would change a call's meaning, it needs a way for an older provider to refuse it: a version marker, or a list of headers the caller requires to be understood. Refusing and stripping are weighed in Decision 6.
2. **Production authentication** (the repository's open question 7). Mutual TLS with workload identities, asymmetric tokens and signed delegation are the candidates of Decision 10.
3. **Underscore spellings on other WSGI servers.** Decision 6 names the gap. A `neolith check` warning for a server configured to pass such headers, where the host can see the configuration, is a candidate.
4. **Secrets from files**, as container platforms mount them. A `SECRET_FILE` form would be a second form of a secret; evidence from adopters would show whether it is needed.
5. **Checking that a provider holds a caller's key.** No startup check sees the provider's key table (Consequences). A probe at Boot, or a field of the health or OpenAPI document, could tell a caller that a provider does not accept its app before failover meets it, at the cost of a network check that 0014's amendment keeps out of serving.
6. **`Authorization` taken by a platform.** Where a gateway or mesh owns `Authorization`, a dedicated header for neolith's credential is something a later backend, or a revision of spec 03, could weigh.

## References

- RFC 9110, [sections 5.1, 5.3 and 5.5](https://www.rfc-editor.org/rfc/rfc9110#section-5) (field names, repeated fields, field values) and [section 11](https://www.rfc-editor.org/rfc/rfc9110#section-11) (authentication); RFC 9111, [section 3](https://www.rfc-editor.org/rfc/rfc9111#section-3) (storing responses).
- [RFC 6750](https://www.rfc-editor.org/rfc/rfc6750), sections 3, 3.1 and 5.3: the `Bearer` challenge, error codes, TLS.
- [RFC 7515](https://www.rfc-editor.org/rfc/rfc7515), [RFC 7518](https://www.rfc-editor.org/rfc/rfc7518) section 3.2, [RFC 7519](https://www.rfc-editor.org/rfc/rfc7519), [RFC 8725](https://www.rfc-editor.org/rfc/rfc8725), [RFC 2104](https://www.rfc-editor.org/rfc/rfc2104) section 2, [RFC 8693](https://www.rfc-editor.org/rfc/rfc8693), [RFC 9651](https://www.rfc-editor.org/rfc/rfc9651) section 4.1.6.
- [W3C Trace Context](https://www.w3.org/TR/trace-context/), sections 3.3.1.1, 3.3.1.5 and 4.3.
- [RFC 9421](https://www.rfc-editor.org/rfc/rfc9421) (HTTP Message Signatures), [RFC 9449](https://www.rfc-editor.org/rfc/rfc9449) (DPoP), [AWS SigV4](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv-create-signed-request.html).
- Prior art: [gRPC status codes](https://grpc.io/docs/guides/status-codes/); [Connect protocol](https://connectrpc.com/docs/protocol/) (`Connect-Timeout-Ms`, the model for `Neolith-Timeout` in 0017); [Stripe webhooks](https://docs.stripe.com/webhooks) (secret roll, signed payload, `whsec_` prefix, CSRF exemption); [Standard Webhooks](https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md); [GitHub token formats](https://github.blog/engineering/platform-security/behind-githubs-new-authentication-token-formats/); [Kubernetes service accounts](https://kubernetes.io/docs/reference/access-authn-authz/service-accounts-admin/); [Dapr API token](https://docs.dapr.io/operations/security/api-token/) and [access control lists](https://docs.dapr.io/operations/configuration/invoke-allowlist/); [Cloud Run service-to-service authentication](https://docs.cloud.google.com/run/docs/authenticating/service-to-service); [Envoy headers](https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_conn_man/headers) (`x-envoy-*`, XFCC); [Django `RemoteUserMiddleware`](https://docs.djangoproject.com/en/5.2/howto/auth-remote-user/); [DRF authentication](https://www.django-rest-framework.org/api-guide/authentication/) (`WSGIPassAuthorization`, challenge from the first class); [OWASP Authentication Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html#authentication-and-error-messages).
- Python: [`hmac.compare_digest` and `hmac.digest`](https://docs.python.org/3/library/hmac.html), [`secrets.token_urlsafe`](https://docs.python.org/3/library/secrets.html), [`base64`](https://docs.python.org/3/library/base64.html).
- Django 5.2.19 and 6.1.3: `core/handlers/asgi.py`, `core/servers/basehttp.py`, `views/debug.py`, `views/decorators/debug.py`, `core/management/commands/diffsettings.py`, `core/checks/registry.py`, `core/checks/security/base.py`, `utils/log.py`.
- gunicorn 27.0.1: `gunicorn/config.py` (`HeaderMap`), `gunicorn/http/message.py`, `gunicorn/http/wsgi.py`.
- PyJWT 2.10.1: `jwt/utils.py`, `jwt/api_jws.py`, `jwt/api_jwt.py`.
- django-neolith at 0.1.0a2: `src/neolith/_connector.py` (`InProcess._refusal`), `src/neolith/_context.py` (`read_principal`, `principal_form`); spec 02 (TRN-42, TRN-43), spec 03, spec 05 (APP-5); records 0008, 0010, 0017, 0021, 0022, 0023.
