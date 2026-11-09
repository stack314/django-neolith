# 0007. The local connector serializes, like the remote one

- **Status:** Proposed
- **Date:** 2026-11-03

## Context

The obvious local connector just calls the implementation and returns its result. That lets things leak that cannot cross a network: ORM instances, lazy querysets, shared mutable dicts, and objects that do not match the contract types. Code like that works attached and breaks on the day the app detaches, which is the exact failure neolith exists to prevent.

## Options

1. **Pass-through local calls.** Fastest, but drift appears only at detach time.
2. **Validate types only.** Catches some mismatches, but references are still shared.
3. **Round-trip through the contract's serializer.** Arguments and results are serialized and deserialized in-process, exactly as on the wire.

## Decision

Option 3. Local calls round-trip arguments and results through the same Pydantic serialization the remote connector uses. Callers get fresh contract-typed objects, never shared references.

Follows principles 5 (*same semantics over same mechanics*) and 8 (*one test suite over many code paths*).

## Consequences

- Detach-breaking code fails in attached mode, during development.
- Each local call pays a serialization cost. This is acceptable at contract granularity. A per-contract escape hatch can be added later only if measurements justify it, and would need its own ADR.
