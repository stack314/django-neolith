# 02. Transports and routing

**Status:** outline.

## The transport interface

A transport exposes one operation to a connector:

```
call(contract, version, action, request, context) -> reply | error
```

A transport MUST:

- **TRN-1.** serialize the request into the portable type system before it leaves the caller, and deserialize the reply before it reaches the caller, even when caller and provider share a process;
- **TRN-2.** carry the call context intact;
- ~~**TRN-3.** map every failure it can observe onto a standard error kind ([01](01-contracts.md)); a transport MUST NOT surface its own exception types to the caller;~~ Withdrawn in spec 0.1: it allowed any standard kind; TRN-8 and TRN-9 name the kinds a transport produces ([ADR 0008](../docs/adr/0008-typed-contract-errors-in-both-modes.md)).
- **TRN-4.** enforce a deadline, and fail with `timeout` when it passes;
- **TRN-8.** map every failure of its own in carrying a call onto `unavailable` or `timeout`, and never pass its own exception types to the caller;
- **TRN-9.** map a reply or an error from the provider that does not validate against the contract onto `internal`.

An error the provider's host delivers passes through the transport unchanged. Only hosts and transports produce `unavailable` and `timeout`.

## In-process transport

**TRN-5.** Defined by this specification and REQUIRED in every implementation. Caller and provider share a process. The transport still serializes and deserializes ([ADR 0007](../docs/adr/0007-in-process-transport-serializes.md)), so that a provider cannot leak a live object and a caller cannot depend on one.

**TRN-6.** The in-process transport MUST run the provider's action in the caller's thread of control, so that attached mode has no hidden concurrency.

**TRN-10.** Because the action runs in the caller's thread, the in-process transport MUST check the deadline before it dispatches the call and again when the action ends, and fail the call with `timeout` if the deadline has passed, discarding the action's reply or error. The action might have run, so `timeout` leaves the outcome unknown, as it does over a network ([ADR 0008](../docs/adr/0008-typed-contract-errors-in-both-modes.md)).

## Providers and routing

A connector holds an ordered set of providers for a contract version. Each provider names a transport and the configuration that transport needs.

A **routing policy** chooses the provider for each call. Defined policies:

- `single`: exactly one provider; required in 0.1;
- `local-first`: prefer an in-process provider, and fall back to the others under the fallback rule below; required in 0.1;
- `remote-first`: the reverse;
- `weighted`: distribute by configured weights;
- `explicit`: chosen by a function of the call context.

**TRN-7.** Fallback after a failed call MUST happen only for idempotent actions and only on `unavailable` or `timeout`.

Decision records: [0014](../docs/adr/0014-pluggable-connector-transports.md), [0016](../docs/adr/0016-an-app-may-run-attached-and-detached-at-once.md).

## Other transports

A transport not defined here (gRPC, a message queue) is conformant when it satisfies the transport interface and passes the conformance tests ([06](06-conformance.md)). Its schema projection of contracts is its own concern.
