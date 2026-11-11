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
| `Authorization: Bearer <token>` | The principal, as a token the provider's host can resolve |
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

Body: `{"kind": "<kind>", "message": "<text>", "data": {<typed detail record>}}`. `data` is present only for a declared kind. The member is not named `detail`, which RFC 9457 uses for a human-readable explanation.

A 401 response also carries `WWW-Authenticate: Bearer`, as RFC 9110 (section 15.5.2) requires for that status.

The provider's host carries every failure on a contract path in this body, including a request that fails validation (`invalid_argument`) and a failure of the host itself (`internal`). A response in the error format of the host's web framework never reaches a caller.

A connector takes the kind from the body only, never from the status. A non-success response without a neolith error body maps to `unavailable`. Examples are a 404 from a host that does not serve that contract version, or an error page from a proxy. A success response whose body is not a valid reply maps to `internal`.

## Projection

**HTTP-2.** A provider MUST serve an OpenAPI 3.1 document describing every contract version it provides, derived from the contracts. The document is the HTTP transport's publication of the contract ([01](01-contracts.md)). Its location is `GET /.well-known/neolith/openapi.json`.

## Principal resolution

The token in `Authorization` is opaque to the transport. The host resolves it into a principal through a pluggable resolver ([ADR 0010](../docs/adr/0010-pluggable-principal-resolution.md)). A shared-secret token for host-to-host calls is the minimum every implementation supports.

## Health

`GET /.well-known/neolith/health` returns 200 when the host can serve its contracts.
