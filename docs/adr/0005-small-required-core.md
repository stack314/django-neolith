# 0005. Small required core; no bundled shared-service apps

- **Status:** Proposed
- **Date:** 2026-11-09

## Context

Detached apps still need users, messages and processes, so there is a pull to build identity, workflow, notifications and more into the framework. Company-backed peers (ABP, Encore) can carry that breadth. A volunteer project has to be more selective. Earlier Django projects in this space show how much work the core alone takes.

An earlier draft of this record named such apps "hub apps". That name was dropped: it implies a centre in a design whose point is that no app is central, and it is not an established term.

## Options

1. **Batteries-included framework** with shared services built in.
2. **Small required core, plus optional reference apps** for identity, notifications and the like, shipped by neolith.
3. **Small required core, and nothing else.** Shared capabilities are ordinary apps behind contracts, written by projects or by the community, with no special status in neolith.

## Decision

Option 3. The required core is **contracts, connectors and transports, the app host, events and checks**. Neolith ships no identity, notification, workflow, configuration or analytics app and gives no app a privileged role. Where the core needs a capability that such an app would provide (for example, resolving who is calling), it defines a seam that any app or external system can fill (see [0010](0010-pluggable-principal-resolution.md)).

Follows principles 12 (*small core over broad features*) and 13 (*integrate over own*).

## Consequences

- The core depends on no particular app. Every cross-cutting need is a seam, not a dependency.
- Reference or community apps for common capabilities may appear later as separate packages. Whether to curate them is a later decision.
- Which capabilities are common enough to deserve a standard contract is an open research question (see [open questions](../open-questions.md)).
