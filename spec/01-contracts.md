# 01. Contracts

**Status:** draft. Identity, actions, the portable type system, publication and checking are specified. Errors stay an outline until [ADR 0008](../docs/adr/0008-typed-contract-errors-in-both-modes.md) is decided.

## Identity and versioning

A contract is identified by a name and a major version, written `<name>.v<N>`, for example `inventory.v1` or `billing.invoices.v2`.

**CON-10.** A contract identity MUST match the rule `contract-id` of the grammar below, written in ABNF (RFC 5234).

```abnf
contract-id = name ".v" major
name        = segment *("." segment)
segment     = lower *(lower / DIGIT / "_")
major       = %x31-39 *DIGIT        ; a positive integer with no leading zero
lower       = %x61-7A               ; "a" to "z"
```

Identities use lowercase ASCII letters, digits and underscores only, so that one identity can be written unchanged in an HTTP path and a protobuf package.

- **CON-1.** Within one major version, changes MUST be backward compatible for existing callers: adding an action, adding an optional request field, adding a reply field, adding an error kind to an action.
- Anything else is a breaking change and requires a new major version. **CON-2.** Old versions SHOULD remain served until no caller uses them.
- **CON-3.** A provider MAY serve several versions of the same contract at once.

Decision records: [0003](../docs/adr/0003-apps-communicate-through-contracts.md), [0006](../docs/adr/0006-contracts-are-typed-and-published-as-openapi.md).

## Actions

An action has a name, a request type, a reply type and a set of error kinds.

**CON-11.** An action name MUST match the rule `segment` of the grammar above.

**CON-12.** Action names MUST be unique within a contract.

Each action is either idempotent or non-idempotent.

- **CON-4.** An action is **non-idempotent** unless it is declared idempotent. Connectors MUST NOT retry or fall back on a non-idempotent action. Forgetting the declaration therefore costs a retry, never a duplicated effect.
- **CON-5.** An action declared **idempotent** MUST produce the same effect when called repeatedly with the same idempotency key as when called once. The declaration is the app author's promise; the conformance suite checks it for the reference contract ([06](06-conformance.md)).

## The portable type system

The portable forms are the types that every transport can represent, JSON Schema and protobuf at least. They are:

- scalars: boolean, integer (64-bit), number (IEEE 754 double), string, bytes, decimal (as string), timestamp (RFC 3339, so with an offset from UTC), date, duration;
- `list<T>`;
- `map<string, T>`;
- `record`: named, ordered fields, each with a type and required/optional;
- `enum`: a closed set of string names;
- `optional<T>`.

**CON-13.** A request type MUST be a record built only from the portable forms.

**CON-14.** A reply type MUST be either a record built only from the portable forms, or empty. An empty reply carries no value.

Records are nominal within a contract and structural across the wire. **CON-15.** Two different records or enums used by one contract MUST NOT have the same name. Generated code for other languages names each record and enum once, so two with one name would collide.

**CON-20.** The name of a record or an enum MUST start with an ASCII letter, followed by ASCII letters, digits or underscores, so that generated code in any language can use it unchanged.

**CON-6.** Unknown fields in a request MUST be ignored by providers. **CON-7.** Unknown fields in a reply MUST be ignored by callers. This is what makes additive changes compatible.

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

**CON-16.** The neutral description MUST give, for each action, its name, whether it is idempotent, the JSON Schema (draft 2020-12) of its request type, and the JSON Schema of its reply type unless the reply is empty. Each record and enum is described once and referenced by name, and a value that travels validates against the description.

## Checking a contract

**CON-17.** An implementation MUST reject a contract that does not meet CON-10, CON-11, CON-12, CON-13, CON-14, CON-15 or CON-20, or whose records cannot ignore unknown fields as CON-6 and CON-7 require, before it serves the contract or calls any of its actions. The rejection names the contract, the action and, for a type, the field.

**CON-18.** A provider MUST implement every action of its contract, taking the action's request type and returning its reply type.

**CON-19.** An implementation MUST reject a provider that does not meet CON-18 before it serves the contract.
