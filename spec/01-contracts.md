# 01. Contracts

**Status:** outline. Each heading names what this document will define and the requirements already settled by decision records. Prose follows as the reference implementation is written.

## Identity and versioning

- A contract is identified by `<name>.v<N>`. `name` is lowercase, dot-separated segments; `N` is a positive integer.
- **CON-1.** Within one major version, changes MUST be backward compatible for existing callers: adding an action, adding an optional request field, adding a reply field, adding an error kind to an action.
- Anything else is a breaking change and requires a new major version. **CON-2.** Old versions SHOULD remain served until no caller uses them.
- **CON-3.** A provider MAY serve several versions of the same contract at once.

Decision records: [0003](../docs/adr/0003-apps-communicate-through-contracts.md), [0006](../docs/adr/0006-contracts-are-typed-and-published-as-openapi.md).

## Actions

- An action has a name, a request type, a reply type and a set of declared error kinds.
- Action names are unique within a contract.
- **CON-4.** An action is **non-idempotent** unless it is declared idempotent. Connectors MUST NOT retry or fall back on a non-idempotent action. Forgetting the declaration therefore costs a retry, never a duplicated effect.
- **CON-5.** An action declared **idempotent** MUST produce the same effect when called repeatedly with the same idempotency key as when called once. The declaration is the app author's promise; the conformance suite checks it for the reference contract ([06](06-conformance.md)).

## The portable type system

Request and reply types are built only from these forms, so that every transport can represent them (JSON Schema and protobuf at least):

- scalars: boolean, integer (64-bit), number (IEEE 754 double), string, bytes, decimal (as string), timestamp (RFC 3339), date, duration;
- `list<T>`;
- `map<string, T>`;
- `record`: named, ordered fields, each with a type and required/optional;
- `enum`: a closed set of string names;
- `optional<T>`.

Records are nominal within a contract and structural across the wire. **CON-6.** Unknown fields in a request MUST be ignored by providers. **CON-7.** Unknown fields in a reply MUST be ignored by callers. This is what makes additive changes compatible.

## Errors

Every action's error set includes the standard kinds, whether declared or not:

| Kind | Meaning | Retry safe |
|---|---|---|
| `invalid_argument` | The request failed validation | no |
| `not_found` | A referenced entity does not exist | no |
| `permission_denied` | The principal may not perform the action | no |
| `conflict` | The action conflicts with current state | no |
| `unavailable` | No provider could be reached | yes, if idempotent |
| `timeout` | The provider did not reply in time | yes, if idempotent |
| `internal` | The provider failed | no |

**CON-8.** A contract MAY declare additional error kinds per action, each with a typed detail record. Error kinds are part of the contract and follow its compatibility rules.

Decision record: [0008](../docs/adr/0008-typed-contract-errors-in-both-modes.md).

## Publication

**CON-9.** A contract MUST be publishable as a neutral description of its actions, types and errors. For the HTTP transport this is OpenAPI 3.1 ([03](03-http-transport.md)). Other transports define their own projection. The projection is derived from the contract, never hand-written.
