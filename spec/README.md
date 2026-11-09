# The neolith specification

Neolith is defined by this specification. It says how an app declares contracts, how calls travel between apps, how events are published, and how an app is booted on its own. It is written without reference to Django or Python. django-neolith is its reference implementation ([ADR 0015](../docs/adr/0015-specification-first-django-reference-implementation.md)).

- **Status:** draft 0.1. Nothing here is stable until django-neolith 0.1 ships against it.
- **Normative words:** MUST, MUST NOT, REQUIRED, SHOULD, MAY, as in RFC 2119.
- **Versioning:** the specification is versioned on its own (`spec 0.1`, `spec 0.2`). An implementation states which specification version it conforms to.
- **Requirement IDs:** every statement that uses a normative word carries an ID, such as **CON-4**: the document's area, from the table below, and a number. An ID is never renumbered or reused. A withdrawn requirement keeps its ID, struck through, with the version that withdrew it and why. Tests and other implementations cite a requirement by version and ID, for example `spec 0.1 CON-4` ([ADR 0018](../docs/adr/0018-requirement-ids-and-spec-to-test-traceability.md)).

## Documents

| # | Document | Defines | In 0.1 | IDs |
|---|---|---|---|---|
| 00 | [Overview and terms](00-overview.md) | The vocabulary, the call model and the two modes | yes | `OVR` |
| 01 | [Contracts](01-contracts.md) | Contract identity, actions, the portable type system, errors, compatibility | yes | `CON` |
| 02 | [Transports and routing](02-transports.md) | The transport interface, in-process semantics, providers and routing | yes | `TRN` |
| 03 | [HTTP transport](03-http-transport.md) | The HTTP/JSON carriage of a call and its OpenAPI projection | yes | `HTTP` |
| 04 | [Events](04-events.md) | Publishing, subscribing, the outbox and delivery guarantees | outline only | `EVT` |
| 05 | [App declaration and lifecycle](05-app-declaration.md) | What an app declares and how a host boots it | yes | `APP` |
| 06 | [Conformance](06-conformance.md) | What an implementation must pass to claim conformance | yes | `CNF` |

## How the specification changes

The specification is written alongside the reference implementation, not ahead of it. A change that alters meaning gets a decision record first; editorial changes do not. When the specification and django-neolith disagree, the specification is wrong or the implementation is wrong, and one of them is fixed. The conformance tests are the arbiter.
