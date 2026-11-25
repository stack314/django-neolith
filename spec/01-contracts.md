# 01. Contracts

**Status:** draft. Identity, actions, the portable type system, errors, publication and checking are specified. How the provider's host and the connectors deliver errors follows [ADR 0008](../docs/adr/0008-typed-contract-errors-in-both-modes.md); a principal with no valid sent form fails the call at the caller (CON-51); one that cannot be resolved joins that requirement with the host that resolves principals (roadmap step 4, #11).

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

**CON-50.** A declaration MUST NOT name an action's request `context` or `timeout`. A connector call takes those names for its options ([ADR 0017](../docs/adr/0017-call-context-explicit-at-the-action-ambient-for-propagation.md) Decision 3), and a call may pass the request by its name.

**CON-12.** Action names MUST be unique within a contract.

Each action is either idempotent or non-idempotent.

- **CON-4.** An action is **non-idempotent** unless it is declared idempotent. Connectors MUST NOT retry or fall back on a non-idempotent action. Forgetting the declaration therefore costs a retry, never a duplicated effect.
- **CON-5.** An action declared **idempotent** MUST produce the same effect when called repeatedly with the same idempotency key as when called once. The declaration is the app author's promise; the conformance suite checks it for the reference contract ([06](06-conformance.md)).

## The portable type system

The portable forms are the types that every transport can represent, JSON Schema and protobuf at least. They are:

- scalars: boolean, integer, number (a finite IEEE 754 double), string, bytes, decimal, timestamp (RFC 3339, so with an offset from UTC), date, duration;
- `list<T>`;
- `map<string, T>`;
- `record`: named, ordered fields, each with a type and required/optional;
- `enum`: a closed set of string names;
- `optional<T>`.

**CON-13.** A request type MUST be a record built only from the portable forms.

**CON-14.** A reply type MUST be either a record built only from the portable forms, or empty. An empty reply carries no value.

How each form is written in JSON is under [Wire encodings](#wire-encodings).

~~**CON-21.** Until the encodings of ADR 0019 are implemented (#10 task 1), an implementation MUST reject a contract whose request type, reply type or detail record (CON-8) uses decimal, before it serves the contract or calls any of its actions. The rejection names the contract, the action and the field.~~ Withdrawn in spec 0.1: decimals travel in the form under [Wire encodings](#wire-encodings) (ADR 0019).

**CON-33.** A field of a record MUST travel under its own name, with no alias.

Records are nominal within a contract and structural across the wire. **CON-15.** Two different records or enums used by one contract MUST NOT have the same name. Generated code for other languages names each record and enum once, so two with one name would collide.

**CON-20.** The name of a record or an enum MUST start with an ASCII letter, followed by ASCII letters, digits or underscores, so that generated code in any language can use it unchanged.

**CON-6.** Unknown fields in a request MUST be ignored by providers. **CON-7.** Unknown fields in a reply MUST be ignored by callers. This is what makes additive changes compatible.

## Wire encodings

Values travel as JSON text (RFC 8259), with each form written as below ([ADR 0019](../docs/adr/0019-wire-encodings-of-the-portable-forms.md)). Records and maps are JSON objects, a record's fields under their own names (CON-33); lists are arrays; an enum is its member's string; and an absent optional value is `null`. Every pattern is anchored with `^…$` and uses only ASCII, so it matches the same strings under ECMA-262 (with no flag, `u` or `v`), Rust's `regex` and Python's regular expressions. A string matches a pattern only when the whole string does, so `$` never admits a final newline.

- **boolean** and **string:** JSON's own.
- **integer:** a JSON number with no fraction or exponent, from −(2^53 − 1) to 2^53 − 1, the range every JSON reader holds exactly (I-JSON, RFC 7493, section 2.2). A 64-bit identifier travels as a string.
- **number:** a JSON number from −1.7976931348623157e308 to 1.7976931348623157e308, the largest double, so it is finite and a reader that turns `1e400` into infinity refuses it. `-0` and `0` are the same value.
- **bytes:** a string in base64url with padding and zero unused bits (RFC 4648, sections 3.5 and 5): `^([A-Za-z0-9_\-]{4})*([A-Za-z0-9_\-][AQgw]==|[A-Za-z0-9_\-]{2}[AEIMQUYcgkosw048]=)?$`. Some validators cannot match this pattern against a very large value; ADR 0019's Consequences give the size.
- **decimal:** a string of an optional `-`, digits with no leading zero, and optionally a point followed by at least one digit, with no exponent and no negative zero at any scale: `^(0(\.[0-9]+)?|-?[1-9][0-9]*(\.[0-9]+)?|-?0\.0*[1-9][0-9]*)$`. It has at most 34 digits, not counting the sign and the point: `^-?[0-9](\.?[0-9]){0,33}$`. So every value is an IEEE 754 decimal128, below 10^34 in magnitude. Trailing zeros are kept: `1.50` stays `1.50`.
- **duration:** seconds with an `s` suffix, with no fraction or with 3 or 6 fractional digits, as few as the value needs, below 10^11 seconds in magnitude: `^(0|-?(0|[1-9][0-9]{0,10})\.([0-9]{3})?(00[1-9]|0[1-9][0-9]|[1-9][0-9]{2})|-?[1-9][0-9]{0,10})s$`. For example `0s`, `-2s`, `1.500s` and `0.000001s`.
- **timestamp:** RFC 3339 with `T` and `Z` in upper case, an offset, years 0001 to 9999, no leap second and at most 6 fractional digits: `^(000[1-9]|00[1-9][0-9]|0[1-9][0-9]{2}|[1-9][0-9]{3})-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](\.[0-9]{1,6})?(Z|[+\-]([01][0-9]|2[0-3]):[0-5][0-9])$`. A timestamp is an instant; its offset is not data.
- **date:** the part of a timestamp before the `T`: `^(000[1-9]|00[1-9][0-9]|0[1-9][0-9]{2}|[1-9][0-9]{3})-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$`.

**CON-34.** The description (CON-16) MUST state each form's range or pattern above: an integer's and a number's range as `minimum` and `maximum`, and a pattern as `pattern`, with a decimal's further patterns under `allOf`. It also marks bytes with `contentEncoding: base64url`, a timestamp with `format: date-time` and a date with `format: date`.

**CON-35.** A sender MUST write a number's `-0` as `0`, as RFC 8785 does.

**CON-36.** A sender MUST write a timestamp whose offset is not a whole number of minutes in UTC, with `Z`. RFC 3339 writes an offset in hours and minutes only, and RFC 9557 (section 2.2) reads `Z` as "local offset unknown".

**CON-37.** A sender MUST write a decimal with a positive exponent at exponent 0, and a decimal zero without its sign, with the value and, for a zero, the scale kept: `1E+2` as `100`, and `-0.00` as `0.00`.

**CON-38.** A sender MUST NOT send a value that does not meet its form after those conversions, such as an infinite or NaN number.

**CON-39.** A receiver MUST refuse a value that its form's range or pattern refuses.

**CON-40.** A receiver MUST also refuse a value that breaks a rule the description cannot state: a date or timestamp whose day does not exist (`2027-02-29`), a timestamp whose instant falls outside years 0001 to 9999 in UTC, and an integer written with a fraction or an exponent (`1.0`), which JSON Schema counts as an integer.

**CON-41.** A sender MUST NOT send a body that holds a duplicate member name, a lone surrogate or a number outside the range of a double, which RFC 8785 (section 3.1) excludes from its input.

**CON-42.** A receiver MUST refuse a body that holds a duplicate member name, a lone surrogate or a number outside the range of a double.

**CON-52.** A receiver MUST refuse a body whose arrays and objects nest more than 256 levels deep, and judge that depth before a reader that recurses reads the body (RFC 8259, section 9, lets a parser limit nesting). A receiver whose reader admits less refuses a shallower body too.

Text may hold a noncharacter, such as U+FFFF, as CON-25 and CON-30 allow, although I-JSON (RFC 7493, section 2.1) excludes it.

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
| `timeout` | The deadline passed before the call ended; the action might or might not have run | no: the outcome is unknown, and the call's one deadline has passed |
| `internal` | The provider failed, its reply or error could not be read, or the caller's host could not form the call's principal | no |

**CON-8.** A contract MAY declare additional error kinds per action, each with a typed detail record. Error kinds are part of the contract and follow its compatibility rules.

**CON-22.** An error kind MUST match the rule `segment` of the grammar under "Identity and versioning". Every standard kind does.

**CON-23.** A declared error kind MUST NOT have the name of a standard kind or a reserved name. The reserved names are `cancelled`, `unknown`, `deadline_exceeded`, `already_exists`, `resource_exhausted`, `failed_precondition`, `aborted`, `out_of_range`, `unimplemented` and `data_loss`, the remaining status codes of gRPC. A later version of this specification adds standard kinds only from the reserved names, so an added standard kind never has the name of a kind a contract declares.

**CON-24.** A detail record MUST be a record built only from the portable forms whose values nest at most 32 levels deep in records, maps and lists, the detail record being the first, so that a caller that does not know its kind can hold it as an untyped map (CON-30). A record that contains itself can nest without limit. Its records and enums share the contract's names with those of the request and reply types (CON-15, CON-20).

**CON-25.** An error that crosses a contract MUST carry its kind and a message of Unicode text, which holds no lone surrogate, since JSON text cannot carry one.

**CON-26.** An error of a standard kind MUST NOT carry a detail.

**CON-27.** An error of a declared kind MUST carry the kind's detail record.

**CON-28.** When an error's kind is neither a standard kind the caller knows nor a kind its version of the contract declares for the action, the caller MUST receive the error with that kind, not as `internal`, and with its detail, if there is one, as an untyped map from the detail's field names to their values, unless the error cannot be read against its contract (CON-49). A provider that meets CON-31 never sends a detail that breaks CON-30. Adding an error kind to an action is a compatible change (CON-1), and a later version of this specification can add a standard kind (CON-23), so a caller can receive a kind it does not know.

**CON-30.** An untyped map MUST hold only text keys and values that are untyped maps, lists, text, booleans, null, integers from −(2^53−1) to 2^53−1 or finite numbers, nested at most 32 levels deep, the outermost map being the first, and holding at most 10,000 values in all, counting every map value and list item at every depth, with each occurrence of a repeated value counted separately. Its text is Unicode text, as in CON-25. The integer range is the portable integer's (see [Wire encodings](#wire-encodings)).

**CON-31.** The value of a detail record, as it travels, MUST meet the bounds of an untyped map (CON-30), so that a caller that does not know its kind can hold it (CON-28). CON-24 makes the depth bound hold for every value of a detail record; the other bounds depend on the value, so the provider's host checks them as it sends the error.

~~**CON-32.** Until the encodings of ADR 0019 are implemented (#10 task 1), an implementation MUST reject a contract whose detail record uses bytes, before it serves the contract or calls any of its actions, since whether such a detail travels within CON-30 depends on the encoding of bytes. The rejection names the contract, the action and the error kind.~~ Withdrawn in spec 0.1: bytes travel as base64url text, which an untyped map holds (ADR 0019).

**CON-29.** A mistake in the calling code, such as a call to an action the contract does not have, MUST NOT be reported as a contract error. It reports a bug in the calling code, not the outcome of a call, so code that handles contract errors does not swallow it.

The provider's host and the connectors deliver errors as [ADR 0008](../docs/adr/0008-typed-contract-errors-in-both-modes.md) decides. Only hosts and transports produce `unavailable` and `timeout`.

**CON-43.** A provider's host MUST deliver an error that the action produced itself, of a kind in the action's error set other than `unavailable` and `timeout`, with its kind, its message and, for a declared kind, its detail record, which passes the description check of TRN-15 as it is sent.

**CON-44.** A provider's host MUST deliver as `internal` every other failure that ends an action, and a failure of the host itself. That includes an error of a kind outside the action's error set, an `unavailable` or `timeout` the action produced, an error of a declared kind without a detail record of that kind, and a detail whose value as it travels breaks CON-31.

**CON-45.** A provider's host MUST NOT convert a signal to stop, such as cancellation or the process exiting, which is not a failure of the action.

**CON-46.** An `internal` error that stands for another failure MUST carry nothing of that failure: its message is fixed text and the call's trace identifier, under which the host that failed records the failure.

**CON-47.** A provider's host MUST record the original failure behind each `internal` error it delivers.

**CON-48.** An error that a provider's action receives from one of its own outgoing calls, and lets end the action, MUST reach the action's caller as `internal`, however the action raises it again. Passing a nested error on takes a new error produced by the action.

**CON-49.** A caller MUST receive as `internal` an error that arrives but cannot be read against its contract: a standard kind with a detail, a declared kind without a detail its record reads, an unknown kind whose detail breaks CON-30, or a kind or a message that breaks CON-22 or CON-25.

**CON-51.** A caller's host MUST fail a call whose principal has no valid sent form ([03](03-http-transport.md)) with `internal`, at the step [ADR 0017](../docs/adr/0017-call-context-explicit-at-the-action-ambient-for-propagation.md) Decision 9 gives, so the call is never sent, in any mode. The message is that of CON-46, and the caller's host records the cause with the trace identifier. Whether a principal is valid is decided once for a scope, so every call made from it gets the same answer. A principal that cannot be resolved fails the same way; that joins this requirement with the host that resolves principals (roadmap step 4, #11).

Decision record: [0008](../docs/adr/0008-typed-contract-errors-in-both-modes.md).

## Publication

**CON-9.** A contract MUST be publishable as a neutral description of its actions, types and errors. For the HTTP transport this is OpenAPI 3.1 ([03](03-http-transport.md)). Other transports define their own projection. The projection is derived from the contract, never hand-written.

**CON-16.** The neutral description MUST give, for each action, its name, whether it is idempotent, the JSON Schema (draft 2020-12) of its request type, the JSON Schema of its reply type unless the reply is empty, and each error kind it declares with the JSON Schema of the kind's detail record. The standard kinds are not listed, since every action has them. Each record and enum is described once and referenced by name, and a value that travels validates against the description.

## Checking a contract

**CON-17.** An implementation MUST reject a contract that does not meet CON-10, CON-11, CON-12, CON-13, CON-14, CON-15, CON-20, CON-22, CON-23, CON-24, CON-33 or CON-50, or whose records cannot ignore unknown fields as CON-6 and CON-7 require, before it serves the contract or calls any of its actions. The rejection names the contract, the action, the error kind where there is one and, for a type, the field.

**CON-18.** A provider MUST implement every action of its contract, taking the action's request type and returning its reply type.

**CON-19.** An implementation MUST reject a provider that does not meet CON-18 before it serves the contract.
