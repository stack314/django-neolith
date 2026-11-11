# 0007. The in-process transport serializes, like the network ones

- **Status:** Accepted
- **Accepted:** 2026-11-11
- **Date:** 2026-11-03

## Context

The obvious in-process transport just calls the implementation and returns its result. That lets things leak that cannot cross a network: ORM instances, lazy querysets, shared mutable dicts, and objects that do not match the contract types. Code like that works attached and breaks on the day the app detaches, which is the exact failure neolith exists to prevent.

## Options

1. **Pass-through in-process calls.** Fastest, but drift appears only at detach time.
2. **Validate types only.** Catches some mismatches, but references are still shared.
3. **Round-trip through the contract's serializer.** Arguments and results are serialized and deserialized in-process, exactly as on the wire.

## Decision

Option 3. Values cross as JSON bytes, in-process too.

1. **Sending.** The sender dumps a value with the declared type's schema-built serializer (`__pydantic_serializer__`, with warnings as errors), never with a record's overridable methods such as `model_dump_json`. A value that is not an instance of the declared type is refused.
2. **The check.** Each value is checked against the contract's published description (spec 01, CON-16) by a strict checker derived from that description. The checker adds no runtime dependency, and it is tested differentially against `jsonschema[format-nongpl]`, a development dependency.
3. **Receiving.** The receiver runs the same check, ignoring unknown fields (CON-6, CON-7), then the declared type's schema-built validator (`__pydantic_validator__`). Callers and implementations get fresh contract-typed objects, never shared references. In-process the check runs once per value, since sender and receiver share it.
4. **Failures.** A request that fails the check is `invalid_argument`, raised without dispatching the call, in both modes. A reply or an error detail that fails it is `internal` ([0008](0008-typed-contract-errors-in-both-modes.md) Decisions 2 and 3).

Follows principles 5 (*same semantics over same mechanics*) and 8 (*one test suite over many code paths*).

## Consequences

- Detach-breaking code fails in attached mode, during development.
- The round-trip is always on. A round-trip only in tests would make attached production differ from what the tests ran.
- Each in-process call pays a serialization cost. Roadmap step 3 publishes it against a direct call (CONTRIBUTING K10). A per-contract escape hatch can be added later only if measurements justify it, and would need its own ADR.
- The overridable methods of a record (`model_dump*`, `model_validate*`, custom serializers on the instance) cannot change what crosses.
- The intended checker is a pydantic-core validator compiled from the description, using the patterns and ranges the description carries, so that the description is the single source of what is admitted.
- A JSON Schema validator at run time was set aside. `jsonschema` brings `rpds-py`, a compiled dependency of every adopter (K5), and it skips `format` checks unless extras are installed, so a value could pass it that the description forbids.
