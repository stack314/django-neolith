# 0008. Contract errors are typed and identical in both modes

- **Status:** Accepted
- **Accepted:** 2026-11-11
- **Date:** 2026-11-03

## Context

A connector call such as `connect("<contract>.v1").<action>(request)` reads like a local call. Remote calls can time out, be refused, or fail halfway. Experience with RPC systems that make remote calls look local suggests callers then tend to under-handle failure. If local and remote calls raise different exceptions, callers can tell the modes apart, and their error handling is only correct in one of them.

Errors reach a caller from three places:

- **The implementation**: a domain failure, such as a missing entity, or a bug.
- **The host and the transport**: no provider could be reached, the deadline passed, or the reply could not be read.
- **The caller's own code**: a call made with no call context, or to an action the contract does not have.

Only the first two are outcomes of a call. The third is a mistake in the calling code.

An exception that escapes an implementation carries its text, its type, its traceback and the exceptions chained to it. Sent to the caller, these expose the provider's internals across an app boundary, and a caller can start to depend on them.

A deadline that passes does not say whether the action ran. gRPC's `DEADLINE_EXCEEDED` is defined the same way: an operation that changes state may have completed.

Web frameworks render failures in their own formats. django-ninja 1.8 answers a request that fails validation with a 422 and a body of the form `{"detail": [...]}`, and in DEBUG mode answers an unhandled exception with a plain-text traceback and a 500 (`ninja/errors.py`, `_default_validation_error` and `_default_exception`). The draft spec 03 uses 422 for declared kinds, and its error body names the detail record `detail`, which RFC 9457 defines as a human-readable explanation.

## Options

1. **Let exceptions propagate as-is.** Local calls raise the implementation's exceptions, and remote calls raise HTTP-client errors. Nothing to build, but error handling differs by mode and internals cross the boundary.
2. **One `ContractError` with a kind, raised identically by both connectors.** Standard kinds plus the kinds each action declares, each declared kind with a typed detail record. Callers branch on the kind, as with gRPC status codes. A caller cannot catch one kind with its own `except` clause.
3. **A class per kind.** Declared errors are re-raised as their own exception types on the caller's side. Callers catch each kind by class, but every class has to be importable from the contract package, and a caller on an older version of the contract has no class for a kind added later.

## Decision

Option 2.

1. **One error type.** Every error that crosses a contract is a `ContractError` with a `kind`, a message and, for a declared kind, its typed detail record. An implementation may raise any standard kind except `unavailable` and `timeout`, or a kind its action declares.
2. **Transport failures.** Only hosts and transports produce `unavailable` and `timeout`. A transport failure surfaces only as one of those two, and a reply that arrives but does not validate as the action's reply type surfaces as `internal`. In-process, the deadline is checked before the call is dispatched and again when the action returns, and a reply that comes back after the deadline is discarded as `timeout`. In every mode, `timeout` means the outcome is unknown, as with gRPC's `DEADLINE_EXCEEDED`.
3. **Unexpected failures.** An `Exception` that escapes an implementation becomes `internal`. So does a `ContractError` whose kind is not in the action's error set. Cancellation and interpreter exit (`asyncio.CancelledError`, `KeyboardInterrupt`, `SystemExit`) are not converted. No exception text, type, traceback or `__cause__` crosses, in any mode. The message is fixed and carries the trace identifier, and the provider's host logs the original with that identifier. The test harness may opt in to re-raising the original, so that a failing test shows it.
4. **Nested calls.** A `ContractError` that an implementation receives from one of its own outgoing calls, and lets escape unchanged, becomes `internal` for its caller. Passing it on takes an explicit re-raise: the implementation raises its own `ContractError`, under the rules of Decision 1. A `not_found` from another contract says that the other contract's entity is missing, which is not what `not_found` from this action means. Google's API design guide gives the same advice: errors from a dependency are translated, not propagated blindly.
5. **Unknown kinds.** A kind the caller's version of the contract does not know arrives as a `ContractError` with that kind, and with its detail as an untyped value. Adding an error kind to an action is a compatible change (spec 01, CON-1), so an older caller must be able to receive one.
6. **Usage errors are not contract errors.** A mistake in the caller's own code, such as a connector call with no active context or a call to an action the contract does not have, raises an ordinary exception, not a `ContractError`. It reports a bug, not the outcome of a call, and code that handles contract errors must not swallow it. [0017](0017-call-context-explicit-at-the-action-ambient-for-propagation.md) Decision 5 says the same.
7. **`unauthenticated`.** A new standard kind: the call's credentials are missing or could not be verified. Over HTTP it is 401. It is not retry safe. `permission_denied` stays for a caller that is identified but may not perform the action. gRPC draws the same line: `PERMISSION_DENIED` must not be used when the caller cannot be identified, and `UNAUTHENTICATED` is used instead.
8. **HTTP.** The provider's host renders every failure on a contract path in the neolith error body of spec 03, including a request that fails validation and a failure of the host itself, so the framework's own error responses never reach a caller. The body's member for the detail record is renamed from `detail` to `data`, the name JSON-RPC 2.0 gives the same member, so that a client or gateway that reads RFC 9457 problem details does not take the record for a human-readable message.

Follows principles 5 (*same semantics over same mechanics*) and 6 (*visible failure over transparent calls*).

## Consequences

- Callers write one set of error handling, which is correct in both modes. They branch on `kind` instead of catching a class per kind.
- Retries rely on idempotent actions and idempotency keys, owned by the connector and its transport. Because `timeout` leaves the outcome unknown, a caller of a non-idempotent action has to learn the outcome another way before it tries again.
- An in-process action that overruns its deadline still runs to the end in the caller's thread (spec 02, TRN-6). The caller sees `timeout` only when the action returns. The transport cannot interrupt it.
- Debugging a provider failure starts from the trace identifier in the message and the provider host's log, not from a traceback in the caller.
- A connector marks every `ContractError` it raises as received, so that the provider's host can tell an error passed through unchanged (Decision 4) from one the implementation raised itself.
- A 401 carries a `WWW-Authenticate` challenge, as RFC 9110 requires for that status.
- CONTRIBUTING K7 binds from this record's acceptance.
- Spec 01, 02 and 03 describe these rules in outline. They become requirements, with IDs and the tests that cite them, together with the code: the errors with the error tasks of roadmap step 2, the HTTP carriage with step 5. The specification does not merge ahead of the code ([0018](0018-requirement-ids-and-spec-to-test-traceability.md)).

## References

- [gRPC status codes](https://grpc.io/docs/guides/status-codes/): `DEADLINE_EXCEEDED`, `UNAUTHENTICATED` and `PERMISSION_DENIED`.
- [Google API design guide, errors](https://cloud.google.com/apis/design/errors#error_propagation): error propagation.
- [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457), Problem Details for HTTP APIs, section 3.1.4 (`detail`).
- [JSON-RPC 2.0](https://www.jsonrpc.org/specification#error_object), the error object (`code`, `message`, `data`).
- [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110#name-401-unauthorized), section 15.5.2, 401 Unauthorized.
