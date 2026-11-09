# 0008. Contract errors are typed and identical in both modes

- **Status:** Proposed
- **Date:** 2026-11-03

## Context

`notify.send(...)` reads like a local call. Remote calls can time out, be refused, or fail halfway. Experience with RPC systems that make remote calls look local suggests callers then tend to under-handle failure. If local and remote calls raise different exceptions, callers can tell the modes apart, and their error handling is only correct in one of them.

## Options

1. **Let exceptions propagate as-is.** Local calls raise the implementation's exceptions, and remote calls raise HTTP-client errors.
2. **Declared errors plus a common transport-error family**, raised identically by both connectors.

## Decision

Option 2.

- Actions declare their domain errors in the contract. These serialize on the wire and are re-raised as the same types on the caller's side.
- Transport failures (timeout, unavailable, protocol error) raise a common `ContractError` hierarchy. The in-process transport can raise these too, and the test harness can simulate them in attached mode.
- Any undeclared exception from an implementation surfaces to callers as a generic contract error, in both modes.

Follows principle 6 (*visible failure over transparent calls*).

## Consequences

- Callers write one set of error handling that is correct in both modes.
- Retries rely on idempotent actions and idempotency keys, owned by the connector and its transport.
