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

Option 3. Values cross as JSON text, in-process too.

1. **Sending.** The sender first checks that the value is an instance of exactly the declared type. The check is explicit, because the serializer does not refuse look-alikes: at any depth, pydantic-core serializes an object with the right attributes in a record's place (`SerCheck::None`, `src/serializers/type_serializers/model.rs`). The check covers the value itself, not the records inside it. A nested look-alike, reached through `model_construct` or attribute assignment, crosses only as JSON text, and only when that text passes the description check and the validator of the request check (0017 Decision 9, the caller's step 2), that is, when its data is right. The sender then dumps the value to JSON text with the declared type's schema-built serializer (`__pydantic_serializer__`, with warnings as errors), never with a record's overridable methods such as `model_dump_json`. Before the dump, the sender applies, at every depth, the conversions and refusals that [0019](0019-wire-encodings-of-the-portable-forms.md) gives the sender. These are conversions of the value itself; the serializer would otherwise write a `-0` as `-0.0`, for example. Durations and very small decimals are different: the serializer writes a duration as `PT1.5S`, and a nonzero decimal closer to zero than 0.000001, or a zero with more than 6 fractional digits, with an exponent (`0.0000001` as `1E-7`, `0.00000000` as `0E-8`). No conversion before the dump can change that, since no `Decimal` value prints as `0.0000001`. Bytes could be converted on the sending side, but the validator reads bytes as UTF-8 text unless its schema says otherwise, so the receiving side needs the schema too. These forms come from the schema the serializer and validator are built from, through a type or setting neolith provides, and which one is settled in roadmap step 3 (#10).
2. **The check.** Each value is checked against the contract's published description (spec 01, CON-16) by a strict checker derived from that description, with the patterns and ranges of 0019. The checker adds no runtime dependency.
3. **Receiving.** The receiver ignores unknown fields (CON-6, CON-7) and runs the description check, together with the rules 0019 lists under *What the description does not state*, then the declared type's schema-built validator (`__pydantic_validator__`). Callers and implementations get fresh contract-typed objects, never shared references. In-process the description check runs once per value, on the sending side: the receiver gets the very JSON text that passed it, and still ignores unknown fields and runs the validator. Over a network both sides run it.
4. **Order of checks.** The caller's and the provider's checks run in the order of [0017](0017-call-context-explicit-at-the-action-ambient-for-propagation.md) Decision 9, which also gives the retry rule. Its request check, the caller's step 2, is this record's sending side (Decisions 1 and 2), and its receive check, the provider's step 5, is this record's receiving side (Decisions 2 and 3).

   A neolith caller gets the same results in both modes, because its own checks run first. `invalid_argument` from the receive check reaches only a caller whose own checks did not run, such as a client that is not neolith. A reply or an error detail that fails the check is `internal` ([0008](0008-typed-contract-errors-in-both-modes.md) Decisions 2 and 3), apart from the detail of a kind the caller does not know, which arrives untyped (0008 Decision 5; spec 01, CON-28, tested in step 3).

Follows principles 5 (*same semantics over same mechanics*) and 8 (*one test suite over many code paths*).

## Consequences

- Detach-breaking code fails in attached mode, during development.
- The round-trip is always on. A round-trip only in tests would make attached production differ from what the tests ran. Akka takes the other path: it serializes local messages only when a test turns it on, and recommends that setting for tests only.
- Each in-process call pays a serialization cost. Roadmap step 3 publishes it against a direct call (CONTRIBUTING K10). A per-contract escape hatch can be added later only if measurements justify it, and would need its own ADR.
- The overridable methods of a record cannot change what crosses.
- The intended checker is a pydantic-core validator compiled from the description, so that the description is the single source of what is admitted.
- In step 3, `jsonschema[format-nongpl]`, a development dependency, checks the checker: the tests run it with a `FormatChecker`, since `format` alone is only an annotation (JSON Schema 2020-12, section 7.2.1). A duration needs 0019's pattern, as no format checker knows its form.
- A JSON Schema validator at run time was set aside. `jsonschema` brings `rpds-py`, a compiled dependency of every adopter (K5), and it skips `format` checks unless extras are installed.
- These rules are outline in spec 02. Their requirement IDs come with step 3 (#10).

## References

- Orleans [serialization](https://learn.microsoft.com/en-us/dotnet/orleans/host/configuration-guide/serialization): objects passed in grain calls are copied by default.
- grpchan [`inprocgrpc`](https://pkg.go.dev/github.com/fullstorydev/grpchan/inprocgrpc): in-process gRPC copies messages so client and server never share them.
- Temporal [sdk-java#3124](https://github.com/temporalio/sdk-java/issues/3124): local activity arguments are serialized even in the same process.
- Akka [serialization, verification](https://doc.akka.io/libraries/akka-core/current/serialization.html): serializing local messages, for tests only.
- [python-jsonschema, validating formats](https://python-jsonschema.readthedocs.io/en/stable/validate/#validating-formats): `FormatChecker` and the `format-nongpl` extra.
