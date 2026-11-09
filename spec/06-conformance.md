# 06. Conformance

**Status:** outline.

An implementation conforms to neolith `spec 0.1` when it passes the conformance suite. The suite is part of this repository and is the authoritative meaning of "implements neolith".

## What the suite exercises

1. **Contracts.** A reference contract with every portable type form, an idempotent and a non-idempotent action, and declared errors. Its projection matches the expected OpenAPI document.
2. **In-process transport.** Calls round-trip through serialization; a provider returning a non-portable value fails with `internal` rather than leaking it; deadlines produce `timeout`.
3. **HTTP transport.** Addressing, headers, error statuses and bodies exactly as in [03](03-http-transport.md). The implementation's HTTP provider is driven by the suite's own HTTP caller, and the implementation's HTTP connector is driven against the suite's own reference provider, so that two conformant implementations interoperate.
4. **Routing.** `single` and `local-first` policies. Failover happens on `unavailable` or `timeout` for idempotent actions only, and never for non-idempotent ones.
5. **Lifecycle.** A host with an unsatisfied requirement fails to start. A host with a provided contract whose implementation misses an action fails to start. A single app boots alone.
6. **Same semantics.** The same contract test cases run over the in-process and the HTTP transport and produce identical results.

## Claiming conformance

An implementation states the specification version it targets and publishes its conformance run. Partial conformance is not a claim; an implementation either passes the suite for a version or it does not.
