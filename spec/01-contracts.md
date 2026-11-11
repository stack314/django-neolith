# 01. Contracts

**Status:** draft. Identity, actions, the portable type system, errors, publication and checking are specified. How transports and the provider's host produce errors is an outline of [ADR 0008](../docs/adr/0008-typed-contract-errors-in-both-modes.md). It becomes requirements, with their tests, in roadmap steps 3 to 5 (#10 to #12).

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

- scalars: boolean, integer (64-bit), number (IEEE 754 double), string, bytes, decimal, timestamp (RFC 3339, so with an offset from UTC), date, duration;
- `list<T>`;
- `map<string, T>`;
- `record`: named, ordered fields, each with a type and required/optional;
- `enum`: a closed set of string names;
- `optional<T>`.

**CON-13.** A request type MUST be a record built only from the portable forms.

**CON-14.** A reply type MUST be either a record built only from the portable forms, or empty. An empty reply carries no value.

The encoding of decimal is settled by the in-process transport (step 3 of the [roadmap](../docs/roadmap.md), issue #10). **CON-21.** Until that encoding is settled, an implementation MUST reject a contract whose request type, reply type or detail record (CON-8) uses decimal, before it serves the contract or calls any of its actions. The rejection names the contract, the action and the field.

Records are nominal within a contract and structural across the wire. **CON-15.** Two different records or enums used by one contract MUST NOT have the same name. Generated code for other languages names each record and enum once, so two with one name would collide.

**CON-20.** The name of a record or an enum MUST start with an ASCII letter, followed by ASCII letters, digits or underscores, so that generated code in any language can use it unchanged.

**CON-6.** Unknown fields in a request MUST be ignored by providers. **CON-7.** Unknown fields in a reply MUST be ignored by callers. This is what makes additive changes compatible.

## Errors

An action's *error set* is the standard kinds below, which every action has whether it declares them or not, and the kinds the action declares.

| Kind | Meaning | Retry safe |
|---|---|---|
| `invalid_argument` | The request failed validation | no |
| `not_found` | A referenced entity does not exist | no |
| `unauthenticated` | The call's credentials are missing or could not be verified, so the caller cannot be identified | no |
| `permission_denied` | The caller, or the principal it calls for, may not perform the action | no |
| `conflict` | The action conflicts with current state | no |
| `unavailable` | No provider could be reached | yes, if idempotent |
| `timeout` | The deadline passed before the call ended; the action might or might not have run | yes, if idempotent |
| `internal` | The provider failed, or its reply or error could not be read | no |

**CON-8.** A contract MAY declare additional error kinds per action, each with a typed detail record. Error kinds are part of the contract and follow its compatibility rules.

**CON-22.** An error kind MUST match the rule `segment` of the grammar under "Identity and versioning". Every standard kind does.

**CON-23.** A declared error kind MUST NOT have the name of a standard kind or a reserved name. The reserved names are `cancelled`, `unknown`, `deadline_exceeded`, `already_exists`, `resource_exhausted`, `failed_precondition`, `aborted`, `out_of_range`, `unimplemented` and `data_loss`, the remaining status codes of gRPC. A later version of this specification adds standard kinds only from the reserved names, so an added standard kind never has the name of a kind a contract declares.

**CON-24.** A detail record MUST be a record built only from the portable forms whose values nest at most 32 levels deep in records, maps and lists, the detail record being the first, so that a caller that does not know its kind can hold it as an untyped map (CON-30). A record that contains itself can nest without limit. Its records and enums share the contract's names with those of the request and reply types (CON-15, CON-20).

**CON-25.** An error that crosses a contract MUST carry its kind and a message of Unicode text, which holds no lone surrogate, since JSON text cannot carry one.

**CON-26.** An error of a standard kind MUST NOT carry a detail.

**CON-27.** An error of a declared kind MUST carry the kind's detail record.

**CON-28.** When an error's kind is neither a standard kind the caller knows nor a kind its version of the contract declares, the caller MUST receive the error with that kind, not as `internal`, and with its detail, if there is one, as an untyped map from the detail's field names to their values. Adding an error kind to an action is a compatible change (CON-1), and a later version of this specification can add a standard kind (CON-23), so a caller can receive a kind it does not know.

**CON-30.** An untyped map MUST hold only text keys and values that are untyped maps, lists, text, booleans, null, integers in the 64-bit range or finite numbers, nested at most 32 levels deep, the outermost map being the first, and holding at most 10,000 values in all, counting every map value and list item at every depth, with each occurrence of a repeated value counted separately. Its text is Unicode text, as in CON-25.

**CON-29.** A mistake in the calling code, such as a call to an action the contract does not have, MUST NOT be reported as a contract error. It reports a bug in the calling code, not the outcome of a call, so code that handles contract errors does not swallow it.

How transports and the provider's host produce errors becomes requirements together with them, in roadmap steps 3 to 5. In outline:

- Only hosts and transports produce `unavailable` and `timeout`. A provider's action produces any other standard kind, or a kind the action declares.
- A failure of a provider's action that is not an error of its error set, an `unavailable` or `timeout` that the action produces, and a failure of the provider's host itself reach the caller as `internal`. A signal to stop, such as cancellation or the process exiting, is not a failure of the action and is not converted. The `internal` error's message is fixed text with the trace identifier, and nothing of the original failure crosses. The provider's host records the original with the trace identifier.
- An error that a provider's action receives from one of its own outgoing calls, and lets end the action, reaches its caller as `internal`. Passing a nested error on takes a new error produced by the action.

Decision record: [0008](../docs/adr/0008-typed-contract-errors-in-both-modes.md).

## Publication

**CON-9.** A contract MUST be publishable as a neutral description of its actions, types and errors. For the HTTP transport this is OpenAPI 3.1 ([03](03-http-transport.md)). Other transports define their own projection. The projection is derived from the contract, never hand-written.

**CON-16.** The neutral description MUST give, for each action, its name, whether it is idempotent, the JSON Schema (draft 2020-12) of its request type, the JSON Schema of its reply type unless the reply is empty, and each error kind it declares with the JSON Schema of the kind's detail record. The standard kinds are not listed, since every action has them. Each record and enum is described once and referenced by name, and a value that travels validates against the description.

## Checking a contract

**CON-17.** An implementation MUST reject a contract that does not meet CON-10, CON-11, CON-12, CON-13, CON-14, CON-15, CON-20, CON-22, CON-23 or CON-24, or whose records cannot ignore unknown fields as CON-6 and CON-7 require, before it serves the contract or calls any of its actions. The rejection names the contract, the action, the error kind where there is one and, for a type, the field.

**CON-18.** A provider MUST implement every action of its contract, taking the action's request type and returning its reply type.

**CON-19.** An implementation MUST reject a provider that does not meet CON-18 before it serves the contract.
