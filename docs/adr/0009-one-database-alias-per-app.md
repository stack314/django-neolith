# 0009. One database alias per app

- **Status:** Proposed
- **Date:** 2026-11-03

## Context

[0004](0004-each-app-owns-its-data.md) forbids cross-app transactions, even while attached. Enforcing that by inspecting code is hard.

## Options

1. **Shared `default` alias with a lint rule.** Enforcement is incomplete.
2. **One alias per app, set by the app host, even when several aliases point at the same database server.**

## Decision

Option 2. The app host gives every app its own database alias, and a router pins each app's models to it. Django opens a separate connection per alias, and `transaction.atomic()` is per alias. So two attached apps cannot share a transaction even on one server, which is exactly how they behave once detached.

Follows principle 5 (*behave detached, even when attached*).

## Consequences

- No-cross-app-transactions is enforced by the mechanism, not by review.
- More connections per process when many apps are attached. Pooling or PgBouncer handles this, and it should be documented.
- Tests must declare which aliases they touch (Django's `databases = {...}`).
