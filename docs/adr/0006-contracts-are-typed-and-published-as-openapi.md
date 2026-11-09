# 0006. Contracts are typed and published as OpenAPI

- **Status:** Accepted
- **Date:** 2026-11-03

## Context

A contract has to be usable in-process, over the network, and eventually by services that are not written in Django or even Python.

## Options

1. **Untyped dicts over HTTP.** Simple, but drift is invisible.
2. **Python types only.** Typed, but Python-specific.
3. **Python type hints on Pydantic models, from which neolith generates each transport's schema and endpoints.**

## Decision

Option 3. In django-neolith, contract types are Pydantic models. This is the reference implementation's choice; the specification itself defines contract types only in neutral terms (JSON Schema), per [0015](0015-specification-first-django-reference-implementation.md). For the HTTP transport, neolith generates the OpenAPI document and the endpoints from the contract types. OpenAPI is the HTTP projection, not the only one ([0014](0014-pluggable-connector-transports.md)). Contracts are versioned by name (`<contract>.v1`, `<contract>.v2`). A breaking change creates a new version, and old versions stay until no caller uses them.

Follows principle 11 (*specification over implementation*).

## Consequences

- Breaking-change detection on the generated spec can run in CI (later).
- Non-Django services can dock as detached peers by implementing the spec.
- Other transports project the same types into their own schema (see [0014](0014-pluggable-connector-transports.md)).
- Standard-library dataclasses are not supported in 0.1. Pydantic v2 validates them through `TypeAdapter`, so adding them later is cheap if adopters ask.
- Endpoint framework choice: see [0011](0011-django-ninja-for-contract-endpoints.md).
