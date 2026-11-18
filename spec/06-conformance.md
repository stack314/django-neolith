# 06. Conformance

**Status:** outline.

An implementation conforms to neolith `spec 0.1` when it passes the conformance suite. The suite is part of this repository and is the authoritative meaning of "implements neolith".

## What the suite exercises

1. **Contracts.** A reference contract with every portable type form, an idempotent and a non-idempotent action, and declared errors. Its projection matches the expected OpenAPI document.
2. **In-process transport.** Calls round-trip through serialization; a provider returning a non-portable value fails with `internal` rather than leaking it; deadlines produce `timeout`.
3. **HTTP transport.** Addressing, headers, error statuses and bodies exactly as in [03](03-http-transport.md). The implementation's HTTP provider is driven by the suite's own HTTP caller, and the implementation's HTTP connector is driven against the suite's own reference provider, so that two conformant implementations interoperate.
4. **Routing.** `single` and `local-first` policies. Failover happens on `unavailable` for idempotent actions only, within the call's deadline, and never on `timeout` or for non-idempotent ones.
5. **Lifecycle.** A host with an unsatisfied requirement fails to start. A host with a provided contract whose implementation misses an action fails to start. A single app boots alone.
6. **Same semantics.** The same contract test cases run over the in-process and the HTTP transport and produce identical results.
7. **Call context** ([ADR 0017](../docs/adr/0017-call-context-explicit-at-the-action-ambient-for-propagation.md)). A connector call from a thread that runs in a fresh, empty context fails as a mistake in the calling code. A call through a scope that has closed fails the same way, for an action scope and a root. A parent's context rebuilt with a later deadline, another principal or another key derives the same child as the original. A call carrying a principal from a caller the contract does not list is refused with `permission_denied` in-process and over HTTP, and the same call from a listed caller succeeds in both. A call with no principal from an unlisted caller is not refused by the list in either mode. A parent that fans out concurrently and is retried derives the same child keys on both attempts. A call whose principal has an empty subject or a lone surrogate in a string, or a sent form ([03](03-http-transport.md)) whose base64url encoding is over 4096 bytes, fails at the caller with `internal`, in-process and over HTTP. One holding U+FFFF, or whose encoding is exactly 4096 bytes, succeeds in both, and the provider receives the principal decoded from its sent form in both.

## Same outcome over both transports

**CNF-1.** An implementation MUST give the caller the same outcome over the in-process and the HTTP transport for a call that reaches its provider and returns before its deadline, for a call whose deadline passes first, and for a call the caller refuses before sending it: the same reply, or an error of the same kind and detail and, apart from `internal`, `unavailable` and `timeout`, the same message. A call whose deadline passes first ends in `timeout` over both.

A refusal that this specification places on one transport only is outside that rule. In 0.1 there are four, all on the HTTP transport ([ADR 0033](../docs/adr/0033-eight-points-of-the-http-transport-settled-for-0-1.md) Decision 1): a call from an app that holds no key to sign a shared-secret token with, which the caller refuses (HTTP-18) and the in-process transport serves; a call from an app the provider's host holds no key for, which that host refuses with `unauthenticated` ([ADR 0027](../docs/adr/0027-a-caller-backend-names-the-calling-app-over-http.md)); a request that a provider's host refuses for its body limit (HTTP-40); and a request over plain HTTP to a host that requires HTTPS ([ADR 0011](../docs/adr/0011-the-host-serves-contract-endpoints-itself.md), row 5). A refusal that this specification later places on one transport only joins this list here, never in a test.

## Claiming conformance

An implementation states the specification version it targets and publishes its conformance run. Partial conformance is not a claim; an implementation either passes the suite for a version or it does not.
