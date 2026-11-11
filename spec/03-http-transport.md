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
| `Neolith-Principal` | The principal on whose behalf the call is made, if there is one |
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

## Caller and principal

Two identities cross with a call ([ADR 0010](../docs/adr/0010-pluggable-principal-resolution.md)): the calling app, which the provider's host authenticates, and an optional principal on whose behalf the calling app calls, which the calling app asserts.

`Authorization` carries the calling app's credential as a bearer token (RFC 6750). The provider's host resolves it through a pluggable backend. A call whose credential is missing or does not resolve, or that reaches a host with no backend, fails with `unauthenticated`.

`Neolith-Principal` carries the principal as the base64url encoding, without padding (RFC 4648, section 5), of a UTF-8 JSON object:

```
{"iss": "<issuer>", "sub": "<subject>", "claims": {<claims>}}
```

`iss` and `sub` are non-empty strings. `claims` is an optional object. The header is absent when the call has no principal. A header that does not decode to such an object fails the call with `unauthenticated`. The shared-secret backend does not sign the principal: it is an assertion by the authenticated calling app. Which calling apps a provider accepts that assertion from is not fixed yet ([ADR 0017](../docs/adr/0017-call-context-explicit-at-the-action-ambient-for-propagation.md), Proposed).

### Shared-secret tokens

The shared-secret token is the one credential every implementation supports. It is for development only: a calling app's secret is held by that app and by every provider it calls, and any holder can call as that app.

Each calling app has its own secret of at least 32 random bytes, named by a key id. A provider's host holds the key id and secret of each app it accepts calls from, and knows which app each key id belongs to.

The token is a JSON Web Token (RFC 7519) in JWS compact serialization (RFC 7515), signed with HMAC SHA-256 (`HS256`, RFC 7518, section 3.2) under the calling app's secret. Its JOSE header and claims are exactly these, and nothing else:

| Member | Where | Value |
|---|---|---|
| `alg` | header | `"HS256"` |
| `typ` | header | `"neolith+jwt"`, so that the token cannot be mistaken for a JWT of another kind (RFC 8725, section 3.11) |
| `kid` | header | The key id of the calling app's secret |
| `iss` | claims | The calling app's `name` from its declaration ([05](05-app-declaration.md)) |
| `aud` | claims | One string: the identity of the contract being called, such as `inventory.v1` |
| `iat` | claims | When the token was issued, in whole seconds since the epoch |
| `exp` | claims | When the token expires, in whole seconds since the epoch, at most 300 seconds after `iat` |

The provider's host accepts the token only when the header and claims are exactly those above, `kid` names a key it holds, the signature verifies under that key's secret, `iss` is the app that key belongs to, `aud` is the contract named in the request path, `iat` is not in the future and `exp` is not in the past. Each time comparison allows 60 seconds of difference between the hosts' clocks. Any other token fails the call with `unauthenticated`. A caller can reuse a token for calls to the same contract until it expires.

Unlike the deadline, the token depends on the hosts' clocks agreeing, within the 60 seconds allowed.

## Health

`GET /.well-known/neolith/health` returns 200 when the host can serve its contracts.
