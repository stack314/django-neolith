# 03. HTTP transport

**Status:** outline. The default network transport, REQUIRED in every implementation.

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
| `Neolith-Deadline` | The absolute deadline, RFC 3339 |

## Errors

Errors are carried as a JSON body with a fixed shape and an HTTP status that follows the kind:

| Kind | Status |
|---|---|
| `invalid_argument` | 400 |
| `permission_denied` | 403 |
| `not_found` | 404 |
| `conflict` | 409 |
| `timeout` | 504 |
| `unavailable` | 503 |
| `internal` | 500 |
| declared kinds | 422 |

Body: `{"kind": "<kind>", "message": "<text>", "detail": {<typed detail record>}}`.

## Projection

A provider MUST serve an OpenAPI 3.1 document describing every contract version it provides, derived from the contracts. The document is the HTTP transport's publication of the contract ([01](01-contracts.md)). Its location is `GET /.well-known/neolith/openapi.json`.

## Principal resolution

The token in `Authorization` is opaque to the transport. The host resolves it into a principal through a pluggable resolver ([ADR 0010](../docs/adr/0010-pluggable-principal-resolution.md)). A shared-secret token for host-to-host calls is the minimum every implementation supports.

## Health

`GET /.well-known/neolith/health` returns 200 when the host can serve its contracts.
