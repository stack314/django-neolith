# 0015. Specification first; django-neolith is the reference implementation

- **Status:** Accepted
- **Accepted:** 2026-11-10
- **Date:** 2026-11-09

## Context

The first draft described neolith as a Django library whose only neutral part was the wire format. Review asked whether to start instead from a Django-agnostic, even language-agnostic, point of view and build adapters from there. The ideas at neolith's centre (an app declares contracts, calls travel over a chosen transport, an app boots alone from its declaration) do not depend on Django. What depends on Django is the batteries that travel with the app.

## Options

1. **Django library only.** Fastest to build. Other stacks can only interoperate at the wire.
2. **Framework-agnostic Python core with Django as one adapter.** Neutral within Python. Risks a core that is abstract for its own sake, with Django's batteries bolted on.
3. **A specification, with django-neolith as its reference implementation.** The spec defines contracts, transports, events and the app lifecycle in neutral terms. django-neolith implements it fully, Django batteries included. Other stacks implement the same spec.

## Decision

Option 3. Neolith is a specification plus a reference implementation.

- The **specification** lives in this repository under `spec/` and is written without reference to Django or Python. It covers contract definition and versioning, the transport interface and the HTTP transport, events, the app declaration and lifecycle, and conformance tests.
- **django-neolith** is the reference implementation and the first thing shipped. The spec is written alongside the Django implementation, not ahead of it, so it stays grounded.
- Another implementation (for example for FastAPI, or in another language) is conformant when it passes the conformance tests. Its apps and Django apps then call each other through the same contracts.

Follows principle 11 (*specification over implementation*).

## Consequences

- Every core design decision is written twice: once in the spec and once in Django. This costs time and keeps Django-specific assumptions out of the core.
- The HTTP transport and its OpenAPI projection ([0006](0006-contracts-are-typed-and-published-as-openapi.md), [0014](0014-pluggable-connector-transports.md)) are part of the spec, not of the Django package.
- Conformance tests are the authoritative definition of "implements neolith".
- The spec is versioned separately from django-neolith.
