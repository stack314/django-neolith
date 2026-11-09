# 0003. Apps communicate only through contracts and connectors

- **Status:** Accepted
- **Date:** 2026-11-03

## Context

If one app imports another's models, services or signals, the two can never run in separate processes. The boundary has to exist, and be enforced, while they are still attached.

## Options

1. **Convention only.** Ask developers not to cross-import. This erodes silently.
2. **Static boundaries only** (import-linter, Tach). These catch imports, but give no way to call across a process boundary.
3. **Contracts plus connectors, with static enforcement.** A contract declares an app's public actions. Callers get a connector that calls locally or remotely. A check fails CI on cross-app internal imports.

## Decision

Option 3.

- A **contract** (`@contract("name.vN")` with `@action` methods) is an app's only public surface. It lives in a contract package that other apps may import natively for its interface and types. The implementation is private to the app.
- Callers use `connect("name.vN")`. The host wires that to a **local** or **remote** connector depending on where the provider runs.
- **Events** (`event(...).publish`, `@subscribe`) replace cross-app Django signals, and work in both modes.
- A **neolith check**, built on existing import linting, enforces the boundary in CI.

Follows principles 2 (*contracts over imports*) and 5 (*same semantics over same mechanics*).

## Consequences

- Callers never receive ORM objects from other apps, only contract types.
- Contracts are a versioned API with real maintenance cost (see [0006](0006-contracts-are-typed-and-published-as-openapi.md)).
- Adoption can be report-only first, then enforced app by app.
