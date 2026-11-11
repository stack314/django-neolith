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

The context travels in headers. Names and semantics are fixed by this document so that implementations interoperate:

| Header | Carries |
|---|---|
| `Authorization: Bearer <token>` | The calling app's credential, which the provider's host authenticates |
| `Neolith-Principal` | The principal on whose behalf the call is made, if there is one ([below](#the-principal)) |
| `Idempotency-Key` | The idempotency key |
| `traceparent` | The trace identifier, per W3C Trace Context |
| `Neolith-Timeout` | The remaining time budget, in whole milliseconds |

The deadline is sent as a relative budget, not an absolute time, so that hosts do not depend on synchronised clocks. The receiving host computes its own local deadline from the budget when the request arrives.

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

Two identities cross with a call ([ADR 0010](../docs/adr/0010-pluggable-principal-resolution.md)): the calling app, which the provider's host authenticates, and an optional principal on whose behalf the calling app calls, which the calling app asserts.

Every value below that is said to be base64url-encoded uses the base64url alphabet only (RFC 4648, section 5), with no padding, no whitespace and zero unused bits. Every JSON object below is I-JSON (RFC 7493), so a duplicate member name or a lone surrogate makes it invalid.

**HTTP-6.** A caller MUST send the calling app's credential in `Authorization`, as a bearer token (RFC 6750).

**HTTP-7.** A provider's host MUST fail a call with `unauthenticated` when its credential is missing or does not resolve, or when the host has no backend to resolve it.

### The principal

A *principal object* is a JSON object with exactly the members `issuer` and `subject`, both non-empty strings, and optionally `claims`, an object whose values are strings, integers, booleans or lists of strings. Two principals are equal when their members are equal, with strings compared code point by code point and without Unicode normalization. `Neolith-Principal` carries a principal object, base64url-encoded, in at most 4096 bytes.

**HTTP-8.** A caller MUST send `Neolith-Principal` exactly when the call has a principal.

**HTTP-9.** A provider's host MUST fail a call with `invalid_argument` when `Neolith-Principal` is present but is not a principal object, encoded as above.

The shared-secret backend does not sign the principal: the calling app asserts it. Which calling apps a provider accepts that assertion from is not fixed yet ([ADR 0017](../docs/adr/0017-call-context-explicit-at-the-action-ambient-for-propagation.md), Proposed).

For example, the principal `{"issuer":"https://id.example.com","subject":"u-81f3","claims":{"roles":["clerk"]}}` is sent as:

```
Neolith-Principal: eyJpc3N1ZXIiOiJodHRwczovL2lkLmV4YW1wbGUuY29tIiwic3ViamVjdCI6InUtODFmMyIsImNsYWltcyI6eyJyb2xlcyI6WyJjbGVyayJdfX0
```

### Shared-secret tokens

**HTTP-10.** Every implementation MUST support the shared-secret token as a credential, on both the caller's and the provider's side.

The shared-secret token is for development only. A calling app's secret is held by that app and by every provider it calls, and any holder can mint tokens for that app. A token is a bearer token: anyone who sees one can call as the calling app, and assert any principal, until it expires.

**HTTP-11.** A token SHOULD travel only over TLS or a loopback connection (RFC 6750, section 5.3).

Each calling app has its own secret, named by a key id: a non-empty string, unique among the keys a provider's host holds. A host can hold more than one key for an app, so that a secret can be replaced without downtime.

**HTTP-12.** A secret MUST be at least 32 bytes from a cryptographically secure random source (RFC 7518, section 3.2).

A token is a JSON Web Token (RFC 7519) in JWS compact serialization (RFC 7515), signed with HMAC SHA-256 (`HS256`) under the calling app's secret. It is three base64url-encoded parts joined by `.`: the JOSE header, the claims, and the signature over the first two parts as sent. Times are integers, meaning JSON numbers with no fraction or exponent, in seconds since the epoch.

| Member | Part | Value |
|---|---|---|
| `alg` | header | `"HS256"` |
| `typ` | header | `"neolith+jwt"`, so that the token cannot be taken for a JWT of another kind (RFC 8725, section 3.11) |
| `kid` | header | The key id of the calling app's secret |
| `iss` | claims | The calling app's `name` from its declaration ([05](05-app-declaration.md)) |
| `aud` | claims | One string: the identity of the contract being called, such as `inventory.v1` |
| `iat` | claims | When the token was issued |
| `exp` | claims | When the token expires, at most 300 seconds after `iat` |

A token is valid only when all of these hold, with *now* the provider host's current time in seconds since the epoch:

- it is at most 4096 bytes, and its three parts are base64url-encoded, the first two as JSON objects;
- the header has exactly the members `alg`, `typ` and `kid`, and the claims exactly `iss`, `aud`, `iat` and `exp`, each of the type in the table;
- `alg` is `HS256`;
- `typ` is `neolith+jwt` or `application/neolith+jwt`, ignoring ASCII case (RFC 7515, section 4.1.9);
- `kid` names a key the host holds, and `iss` is the app that key belongs to;
- the signature is the HMAC SHA-256 of the first two parts, as sent, under that key's secret;
- `aud` is the identity of the contract in the request path;
- `iat` < `exp` ≤ `iat` + 300, `iat` ≤ *now* + 60, and *now* < `exp` + 60.

**HTTP-13.** A provider's host MUST fail a call with `unauthenticated` when its token is not valid.

**HTTP-14.** A provider's host SHOULD compare signatures in constant time.

**HTTP-15.** A caller MAY reuse a token for calls to the same contract until it expires.

The audience names the contract, which both sides know from the request path. It keeps a token from being used at another contract. It does not tell two providers of one contract apart; with a shared secret, every provider that checks a calling app's tokens could mint them anyway.

The 60 seconds allowed each way absorb differences between the hosts' clocks, so a host accepts a token for at most 420 seconds of its own clock. Unlike the deadline, the token depends on the clocks agreeing within that margin.

For example, with the secret `000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f` (hexadecimal) under the key id `orders-1`, the app `orders` calling `inventory.v1` with `iat` 1767225600 and `exp` 1767225900 sends:

```
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6Im5lb2xpdGgrand0Iiwia2lkIjoib3JkZXJzLTEifQ.eyJpc3MiOiJvcmRlcnMiLCJhdWQiOiJpbnZlbnRvcnkudjEiLCJpYXQiOjE3NjcyMjU2MDAsImV4cCI6MTc2NzIyNTkwMH0.AhMk-cm_SInC6ek-6CLP8pN7ciQFHuGb-i2MSGu3efU
```

## Health

`GET /.well-known/neolith/health` returns 200 when the host can serve its contracts.
