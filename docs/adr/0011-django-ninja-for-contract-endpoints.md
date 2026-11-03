# 0011. django-ninja for contract endpoints

- **Status:** Proposed
- **Date:** 2026-11-03

## Context

Contracts are Pydantic-typed and published as OpenAPI ([0006](0006-contracts-are-typed-and-published-as-openapi.md)). Neolith has to generate REST endpoints for them. Django REST Framework is the most-used Django package (about 49% in the 2025 Django survey). django-ninja is built on Pydantic and type hints.

## Options

1. **Django REST Framework.** Familiar to most of the audience, but serializers are not Pydantic, so contracts would need a translation layer, and OpenAPI needs drf-spectacular.
2. **django-ninja.** Pydantic-native, and generates OpenAPI from type hints. A smaller ecosystem than DRF.
3. **A thin in-house view layer.** No dependency, but we would be reimplementing what ninja already does.

## Decision

Option 2, as an internal dependency of the generated endpoints. App authors write contracts, not ninja routers. The choice stays behind neolith's API, so it can be replaced without breaking users. Adopters keep using DRF for their own non-contract APIs.

Follows principles 10 (*Django-native*) and 13 (*integrate over own*).

## Consequences

- One type system (Pydantic) spans contracts, validation, serialization and OpenAPI.
- Adds a dependency (django-ninja 1.x).
- If ninja stalls, the endpoint layer is the only thing to replace.
