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
- **TRN-3.** map every failure it can observe onto a standard error kind ([01](01-contracts.md)); a transport MUST NOT surface its own exception types to the caller;
- **TRN-4.** enforce a deadline: a call whose deadline passes before the call ends fails with `timeout`.

Which kinds a transport produces is set by [ADR 0008](../docs/adr/0008-typed-contract-errors-in-both-modes.md): `unavailable` or `timeout` for a failure of its own in carrying a call, and `internal` for a reply or error from the provider that does not validate against the contract, apart from an error of a kind the caller does not know. An error the provider's host delivers otherwise passes through unchanged.

## In-process transport

**TRN-5.** Defined by this specification and REQUIRED in every implementation. Caller and provider share a process. The transport still serializes and deserializes ([ADR 0007](../docs/adr/0007-in-process-transport-serializes.md)), so that a provider cannot leak a live object and a caller cannot depend on one.

**TRN-6.** The in-process transport MUST run the provider's action in the caller's thread of control, so that attached mode has no hidden concurrency.

Because the action runs in the caller's thread, the in-process transport checks the deadline before it dispatches the call and again when the action ends. If the deadline has passed by then, the action's reply, or a failure it ends with, is discarded and the call fails with `timeout`; the provider's host still records a discarded failure. A signal to stop, such as cancellation or the process exiting, is not a failure of the action and is not discarded. The action might have run, so `timeout` leaves the outcome unknown, as it does over a network.

## Providers and routing

A connector holds an ordered set of providers for a contract version. Each provider names a transport and the configuration that transport needs.

A **routing policy** chooses the provider for each call. Defined policies:

- `single`: exactly one provider; required in 0.1;
- `local-first`: prefer an in-process provider, and fall back to the others under the fallback rule below; required in 0.1;
- `remote-first`: the reverse;
- `weighted`: distribute by configured weights;
- `explicit`: chosen by a function of the call context.

~~**TRN-7.** Fallback after a failed call MUST happen only for idempotent actions and only on `unavailable` or `timeout`.~~ Withdrawn in spec 0.1: `timeout` leaves the outcome unknown, so it is no longer grounds for fallback (ADR 0017).

**TRN-8.** A retry or a fallback after a failed call MUST happen only for an idempotent action, only on `unavailable`, and only within the call's deadline.

Decision records: [0014](../docs/adr/0014-pluggable-connector-transports.md), [0016](../docs/adr/0016-an-app-may-run-attached-and-detached-at-once.md).

## The call context across calls

How the call context of [00](00-overview.md) moves from call to call follows [ADR 0017](../docs/adr/0017-call-context-explicit-at-the-action-ambient-for-propagation.md). In outline:

- A context starts only at a *root*: the provider side of a transport, a request the host serves, a scope opened explicitly for a command or background work, or a test harness. A transport never fabricates a context. A request root gets the host's default deadline, 30 seconds unless the host is configured otherwise, or none if it is configured so. A command or background scope, and a test harness, have no deadline unless they are given one.
- A child call's deadline is the earlier of its parent's and its own timeout. The trace continues from the parent, the principal is carried over unchanged, and the calling app is the app making the call.
- A child call's idempotency key is derived from its parent's key, when the parent has one: `base64url(SHA-256(JCS([parent_key, contract_id, action, base64url(SHA-256(JCS(request))), count])))`. `JCS` is RFC 8785, `base64url` is RFC 4648 section 5 without padding, `contract_id` is the contract identity, such as `inventory.v1`, `request` is the request's JSON wire form under the I-JSON encodings of [ADR 0019](../docs/adr/0019-wire-encodings-of-the-portable-forms.md), and `count` is how many calls with the same contract, action and request hash the parent made before this one. A parent with no key gives its child a random key of 128 bits. A retried parent therefore reproduces its children's keys, under concurrent fan-out too.
- On the caller's side, in both modes: a call with no context is a mistake in the calling code, as is a request that fails the caller's own check ([01](01-contracts.md)); then the key is derived and the parent's count goes up; then a remaining budget that rounds down to 0 ms fails with `timeout`; then the call is sent. A call counts as made once it passes the first two checks. On the provider's side: authentication, then the deadline, then the request check (`invalid_argument`), then dispatch.
- A connector retries only an idempotent action, only on `unavailable` and only within the call's one deadline. It never retries or falls back on `timeout`, whose outcome is unknown (TRN-8).
- Two contexts are equal when their propagated and per-call fields are equal, leaving out the trace's span identifier, which changes at each hop, and the deadline's value, which each host holds on its own clock. Whether a deadline is present is compared.

These become requirements, with their tests, in roadmap step 3 (#10).

## Other transports

A transport not defined here (gRPC, a message queue) is conformant when it satisfies the transport interface and passes the conformance tests ([06](06-conformance.md)). Its schema projection of contracts is its own concern.
