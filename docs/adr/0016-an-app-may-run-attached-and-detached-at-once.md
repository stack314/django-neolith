# 0016. An app may run attached and detached at the same time

- **Status:** Proposed
- **Date:** 2026-11-09

## Context

The first draft treated attached and detached as exclusive: an app runs in one mode per deployment. Review asked for redundancy, with the same app running attached in the shared process and detached as its own service at the same time. The same arrangement also describes a migration in progress, where traffic moves from one mode to the other.

## Options

1. **One mode per app per deployment.** Redundancy comes only from running more instances of a detached app.
2. **Both modes at once, with the connector routing between them.** The connector for a contract knows every available provider (in-process and remote) and applies a routing policy.

## Decision

Option 2 is a goal. The connector may have more than one provider for a contract and chooses among them by a configured policy: local first with remote fallback, remote first, weighted, or explicit.

Constraints that keep this sound:

- **One owner of the data.** Both instances of the app use the app's single database ([0004](0004-each-app-owns-its-data.md)). Redundancy is of the process, not of the data.
- **Idempotent actions** ([0008](0008-typed-contract-errors-in-both-modes.md)) so that a fallback after a failed call is safe.
- **Same serialization everywhere** ([0007](0007-local-connector-serializes.md)) so that callers cannot tell which provider answered.

Follows principles 3 (*configuration over code changes*) and 5 (*same semantics over same mechanics*).

## Consequences

- The connector gains a routing policy and a provider list. Both are configuration.
- The contract test suite gains a case for failover between providers.
- Events published by either instance must be de-duplicated by the outbox, since both share one database.
- v0.1 need not ship routing policies beyond "single provider", but the connector interface must allow more than one from the start.
