# 03. HTTP transport

**Status:** outline.

**HTTP-1.** This is the default network transport, REQUIRED in every implementation.

## Addressing

One action is one HTTP endpoint:

```
POST /<name>/v<N>/<action>
```

The request body is the request record as JSON. The reply body is the reply record as JSON. GET is not used for actions, so that actions with side effects and actions without are carried the same way.

## Call context

The context travels in headers. The calling app's credential travels in `Authorization` ([below](#calling-app-and-principal)). Names and semantics are fixed by this document so that implementations interoperate:

| Header | Carries |
|---|---|
| `Neolith-Principal` | The principal on whose behalf the call is made, if there is one ([below](#the-principal)) |
| `Idempotency-Key` | The idempotency key |
| `traceparent` | The trace identifier, per W3C Trace Context |
| `Neolith-Timeout` | The remaining time budget, in whole milliseconds, rounded down |

The deadline is sent as a relative budget, not an absolute time, so that the deadline does not depend on synchronised clocks. The receiving host computes its own local deadline from the budget when the request arrives. In outline, following [ADR 0017](../docs/adr/0017-call-context-explicit-at-the-action-ambient-for-propagation.md):

- `Neolith-Timeout` is a positive integer of at most 10 digits with no leading zeros, as Connect's `Connect-Timeout-Ms` is. An absent header means the call has no deadline. A caller sends a budget above 9999999999 ms as 9999999999, and never sends 0: a call whose budget rounds down to 0 ms fails with `timeout` before it is sent. A provider's host fails a call whose header is present but does not match with `invalid_argument`, and can clamp a budget to its own maximum.
- `traceparent` follows W3C Trace Context, including its rules for versions above `00`. One that is absent or invalid starts a fresh trace, and `tracestate` is then dropped; otherwise `tracestate` is passed on.
- `Idempotency-Key` carries the call's key as a structured-field string (RFC 9651), of at most 255 characters, as the IETF Idempotency-Key draft defines it: one derived from its parent's key as in [02](02-transports.md), or a random one at a root. A malformed one fails the call with `invalid_argument`. A call that arrives without one has no key, so the calls it makes get random keys, and a retry of it does not reproduce them.

These become requirements, with their tests, with the HTTP transport in roadmap step 5 (#12).

## Errors

Errors are carried as a JSON body with a fixed shape and an HTTP status that follows the kind:

| Kind | Status |
|---|---|
| `invalid_argument` | 400 |
| `unauthenticated` | 401 |
| `permission_denied` | 403 |
| `not_found` | 404 |
| `conflict` | 409 |
| `timeout` | 504 |
| `unavailable` | 503 |
| `internal` | 500 |
| declared kinds | 422 |

Body: `{"kind": "<kind>", "message": "<text>", "data": {<typed detail record>}}`, with `data` present only for a declared kind. A response carries a *neolith error body* when its body is a JSON object whose `kind` is a string. The member for the detail record is named `data`, not `detail`, which RFC 9457 and some web frameworks use for a human-readable explanation.

A *contract path* is the path of an action of a contract version the host provides. A response on any other path carries no neolith error body. The provider's host answers every failure on a contract path with a neolith error body and the status of its kind, including a request that fails validation and a failure of the host itself ([ADR 0008](../docs/adr/0008-typed-contract-errors-in-both-modes.md)). A 401 also carries a `Bearer` challenge in `WWW-Authenticate` (RFC 6750, section 3), since RFC 9110, section 15.5.2, requires a challenge with that status.

A connector takes the kind from the body only, never from the status. A non-success response without a neolith error body maps to `unavailable`. Examples are a 404 from a host that does not provide that contract version, which is not a contract path, or an error page from a proxy. A success response whose body is not a valid reply, and a neolith error body that does not validate against the contract, map to `internal`, apart from an error of a kind the caller does not know.

## Projection

**HTTP-2.** A provider MUST serve an OpenAPI 3.1 document describing every contract version it provides, derived from the contracts. The document is the HTTP transport's publication of the contract ([01](01-contracts.md)). Its location is `GET /.well-known/neolith/openapi.json`.

## Calling app and principal

Two identities cross with a call ([ADR 0010](../docs/adr/0010-pluggable-principal-resolution.md)): the calling app, which the provider's host authenticates, and an optional principal on whose behalf the calling app calls, which the calling app asserts. A *backend* is the part of the provider's host that resolves a call's credentials into the calling app and checks the principal. Other backends, such as one built on mutual TLS or OIDC, can carry both differently. This section fixes the carriage of the shared-secret backend, which every implementation supports.

A host with several backends tries them in a configured order. The first that recognizes a call's credentials decides, by resolving them or refusing the call, and a backend that does not recognize them abstains.

Base64url-encoded values here use the base64url alphabet only (RFC 4648, section 5), with no padding, no whitespace and zero unused bits. JSON objects here are I-JSON (RFC 7493), so a duplicate member name or a lone surrogate makes one invalid, and integers are JSON numbers with no fraction or exponent, within ±(2^53 − 1).

On a contract path, the provider's host authenticates the calling app before it reads the request body or any call-context header. A call on a contract path whose credential is missing, is recognized by no backend or does not resolve, or that reaches a host with no backend, fails with `unauthenticated`. It then checks the principal, as below, before it validates the request body. A request on any other path is answered as the Errors section says. In-process there are no credentials: the calling app is the app the call is made from. A call made from no app fails with `unauthenticated` on every transport.

### The principal

A *principal object* is a JSON object with exactly the members `issuer` and `subject`, both non-empty strings, and optionally `claims`, an object whose values are strings, integers, booleans or lists of strings. An absent `claims` is the same as an empty one. Two principals are equal when their members are equal, with strings compared code point by code point and without Unicode normalization, and lists compared in order. A subject is unique only within its issuer, so code that stores a principal stores both.

With the shared-secret backend, `Neolith-Principal` is present exactly when the call has a principal, and carries it as a bare assertion: a principal object, base64url-encoded, in at most 4096 bytes. A header with an empty value counts as present. A bare assertion that is not a principal object, encoded as above, fails the call with `invalid_argument`, once the calling app has been authenticated and found on the allow-list below.

The principal is not signed: the authenticated calling app asserts it, and it never goes inside the shared-secret token. A provider accepts `Neolith-Principal` only from a calling app on its allow-list of apps that may assert a principal. A call from any other calling app that carries the header fails with `permission_denied`, whatever the header holds. A later backend can carry a signed delegation token in this header instead of the bare assertion, under its own format.

For example, the principal `{"issuer":"https://id.example.com","subject":"u-81f3","claims":{"roles":["clerk"]}}` is sent as:

```
Neolith-Principal: eyJpc3N1ZXIiOiJodHRwczovL2lkLmV4YW1wbGUuY29tIiwic3ViamVjdCI6InUtODFmMyIsImNsYWltcyI6eyJyb2xlcyI6WyJjbGVyayJdfX0
```

### Shared-secret tokens

The shared-secret token is for development only. An implementation supports it on both the caller's and the provider's side, and enables it only when configured to. A calling app's secret is held by that app and by every provider it calls, and any holder can mint tokens for that app. A token is a bearer token: anyone who sees one can call as the calling app until the provider stops accepting it, and assert any principal wherever that app is on the allow-list. RFC 6750, section 5.3, requires TLS for bearer tokens. Development setups often use plain HTTP on a private network, and anyone who can watch that network can replay a token.

Each calling app has one or more secrets, each named by a key id that is a non-empty string, unique among that app's keys. More than one key lets a secret be replaced without downtime. A secret is at least 32 bytes (RFC 7518, section 3.2), generated at random (RFC 8725, section 3.5).

The token travels as `Authorization: Bearer <token>`. It is a JSON Web Token (RFC 7519) in JWS compact serialization (RFC 7515), signed with HMAC SHA-256 (`HS256`) under one of the calling app's secrets. It is three base64url-encoded parts joined by `.`: the JOSE header, the claims, and the signature, computed over the first two parts joined by `.`, exactly as sent (RFC 7515, section 5.1). Times are integers, in seconds since the epoch.

| Member | Part | Value |
|---|---|---|
| `alg` | header | `"HS256"` |
| `typ` | header | `"neolith+jwt"`, so that the token cannot be taken for a JWT of another kind (RFC 8725, section 3.11) |
| `kid` | header | The key id of the secret that signed the token |
| `iss` | claims | The calling app's `name` from its declaration ([05](05-app-declaration.md)) |
| `aud` | claims | One string: the identity of the contract being called, such as `inventory.v1` |
| `iat` | claims | When the token was issued |
| `exp` | claims | When the token expires, at most 300 seconds after `iat` |

The shared-secret backend recognizes a call whose `Authorization` uses the `Bearer` scheme (compared ignoring ASCII case) with a token whose first part decodes, under the encoding rules above, to a JSON object whose `typ` is `neolith+jwt` or `application/neolith+jwt`, ignoring ASCII case. It abstains on any other credentials, including a token whose first part breaks those rules, so a later backend can try them. A recognized token is valid only when all of these hold, with *now* the provider host's current time in seconds since the epoch:

- it is at most 4096 bytes, and its three parts are base64url-encoded, the first two as JSON objects;
- the header has exactly the members `alg`, `typ` and `kid`, and the claims exactly `iss`, `aud`, `iat` and `exp`, each of the type in the table;
- `alg` is `HS256`;
- `typ` is `neolith+jwt` or `application/neolith+jwt`, ignoring ASCII case (RFC 7515, section 4.1.9);
- `kid` names a key the host holds for the app named by `iss`;
- the signature is the HMAC SHA-256 of the first two parts joined by `.`, as sent, under that key's secret, compared in constant time;
- `aud` is the identity of the contract in the request path;
- `iat` < `exp` ≤ `iat` + 300, `iat` ≤ *now* + 60, and *now* < `exp` + 60.

A recognized token that is not valid fails the call with `unauthenticated`, with no fallback to a later backend. The token identifies the calling app only; it never carries the end user. A caller can reuse a token for calls to the same contract while it is valid, so a provider does not refuse a token for having seen it before.

The audience names the contract, which both sides know from the request path. It keeps a token from being used at another contract. It does not keep a token seen at one provider of a contract from being replayed at another provider of the same contract, such as the attached and detached providers of one app, until it stops being valid. With a shared secret, every provider that checks a calling app's tokens could also mint them.

The 60 seconds allowed each way absorb differences between the hosts' clocks, so a host accepts a token for at most 420 seconds of its own clock. Unlike the deadline, the token depends on the clocks agreeing within that margin.

For example, with the example secret `000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f` (hexadecimal, not random) under the key id `orders-1`, the app `orders` calling `inventory.v1` with `iat` 1767225600 and `exp` 1767225900 sends:

```
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6Im5lb2xpdGgrand0Iiwia2lkIjoib3JkZXJzLTEifQ.eyJpc3MiOiJvcmRlcnMiLCJhdWQiOiJpbnZlbnRvcnkudjEiLCJpYXQiOjE3NjcyMjU2MDAsImV4cCI6MTc2NzIyNTkwMH0.AhMk-cm_SInC6ek-6CLP8pN7ciQFHuGb-i2MSGu3efU
```

These rules become requirements, with IDs and tests, with the HTTP transport in roadmap step 5 (#12). The allow-list joins the app declaration in step 4 (#11).

## Health

`GET /.well-known/neolith/health` returns 200 when the host can serve its contracts.
