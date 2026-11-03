# 0006. Contracts are typed and published as OpenAPI

- **Status:** Accepted
- **Date:** 2026-11-03

## Context

A contract has to be usable in-process, over the network, and eventually by services that are not written in Django or even Python.

## Options

1. **Untyped dicts over HTTP.** Simple, but drift is invisible.
2. **Python types only.** Typed, but Python-specific.
3. **Python type hints (dataclasses or Pydantic) that generate an OpenAPI spec and the REST endpoints.**

## Decision

Option 3. Contracts are defined with type hints and Pydantic models or dataclasses. Neolith generates the OpenAPI spec and the REST endpoints from them. Contracts are versioned by name (`<contract>.v1`, `<contract>.v2`). A breaking change creates a new version, and old versions stay until no caller uses them.

Follows principle 11 (*language-neutral contracts over Python-only ones*).

## Consequences

- Breaking-change detection on the generated spec can run in CI (later).
- Non-Django services can dock as detached peers by implementing the spec.
- OpenAPI is the contract's projection for the HTTP transport. Other transports project the same types into their own schema (see [0014](0014-pluggable-connector-transports.md)).
- Endpoint framework choice: see [0011](0011-django-ninja-for-contract-endpoints.md).
