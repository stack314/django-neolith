# 0004. Each app owns its data

- **Status:** Accepted
- **Date:** 2026-11-03

## Context

Detaching an app is only a *data move* if its data was never entangled with other apps' data. Joins, foreign keys and transactions across apps all work while attached, and then all break when the app detaches.

## Options

1. **Shared schema; untangle at detach time.** Costs nothing up front, and is the most expensive option later.
2. **Each app owns its data in both modes.** Rules apply from day one.

## Decision

Option 2.

- Each app has its own schema, table prefix or database. A router keeps each app on its own connection.
- No cross-app foreign keys or joins. Apps store other apps' IDs and fetch details through contracts. The neolith check flags cross-app foreign keys.
- No cross-app transactions, even while attached. Multi-app consistency uses events or workflow steps.
- No cross-app migration dependencies, so each app's migration graph stands alone.
- Reporting reads from replicas or events, never from another app's tables.

Follows principle 4 (*owned data over shared data*).

## Consequences

- `ForeignKey(settings.AUTH_USER_MODEL)` becomes a stored user ID plus the identity contract. This is the most visible change for adopters.
- `neolith detach <app>` can generate a planned data move (copy, switch writes, verify).
- Mechanism: see [0009](0009-one-database-alias-per-app.md).
