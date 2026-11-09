# The neolith specification

Neolith is defined by this specification. It says how an app declares contracts, how calls travel between apps, how events are published, and how an app is booted on its own. It is written without reference to Django or Python. django-neolith is its reference implementation ([ADR 0015](../docs/adr/0015-specification-first-django-reference-implementation.md)).

- **Status:** draft 0.1. Nothing here is stable until django-neolith 0.1 ships against it.
- **Normative words:** MUST, MUST NOT, SHOULD, MAY, as in RFC 2119.
- **Versioning:** the specification is versioned on its own (`spec 0.1`, `spec 0.2`). An implementation states which specification version it conforms to.

## Documents

| # | Document | Defines | In 0.1 |
|---|---|---|---|
| 00 | [Overview and terms](00-overview.md) | The vocabulary, the call model and the two modes | yes |
| 01 | [Contracts](01-contracts.md) | Contract identity, actions, the portable type system, errors, compatibility | yes |
| 02 | [Transports and routing](02-transports.md) | The transport interface, in-process semantics, providers and routing | yes |
| 03 | [HTTP transport](03-http-transport.md) | The HTTP/JSON carriage of a call and its OpenAPI projection | yes |
| 04 | [Events](04-events.md) | Publishing, subscribing, the outbox and delivery guarantees | outline only |
| 05 | [App declaration and lifecycle](05-app-declaration.md) | What an app declares and how a host boots it | yes |
| 06 | [Conformance](06-conformance.md) | What an implementation must pass to claim conformance | yes |

## How the specification changes

The specification is written alongside the reference implementation, not ahead of it. A change that alters meaning gets a decision record first; editorial changes do not. When the specification and django-neolith disagree, the specification is wrong or the implementation is wrong, and one of them is fixed. The conformance tests are the arbiter.
