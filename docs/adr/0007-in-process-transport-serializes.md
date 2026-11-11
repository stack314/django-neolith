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

1. **Sending.** The sender first checks that the value is an instance of exactly the declared type. The check is explicit, because the serializer does not refuse look-alikes: at the top level, pydantic-core serializes any object with the right attributes (`SerCheck::None`, `src/serializers/type_serializers/model.rs`). The sender then dumps the value to JSON text with the declared type's schema-built serializer (`__pydantic_serializer__`, with warnings as errors), never with a record's overridable methods such as `model_dump_json`. Before the dump, the sender applies the wire encodings of [0019](0019-wire-encodings-of-the-portable-forms.md): a timestamp whose offset has seconds is converted to UTC, and `-0` becomes `0`, which the serializer would otherwise write as `-0.0`. A non-finite number and a string outside its pattern are refused.
2. **The check.** Each value is checked against the contract's published description (spec 01, CON-16) by a strict checker derived from that description, with the patterns and ranges of 0019. The checker adds no runtime dependency.
3. **Receiving.** The receiver ignores unknown fields (CON-6, CON-7) and runs the description check, then the declared type's schema-built validator (`__pydantic_validator__`). Callers and implementations get fresh contract-typed objects, never shared references. In-process the description check runs once per value, on the sending side: the receiver gets the very JSON text that passed it, and still ignores unknown fields and runs the validator. Over a network both sides run it.
4. **Order of checks.** On the caller's side, in both modes:

   1. The call context must be present; a call without one is a usage error.
   2. The request is checked: its exact type, the serializer, the description check, and the declared type's validator run on the dumped JSON text. A failure is a usage error, so a neolith caller never receives `invalid_argument` for its own request.
   3. The idempotency key is derived, and the parent's occurrence count goes up by one.
   4. A remaining budget that rounds down to 0 ms fails with `timeout`.
   5. The call is sent.

   A call counts as made once it passes steps 1 and 2, whatever the deadline does next. The connector runs these steps before the transport, so the transport's requirements in spec 02 apply only to calls that passed steps 1 and 2. On the provider's side:

   1. The calling app is authenticated (`unauthenticated`).
   2. Over HTTP, the neolith headers (the principal, the timeout and the idempotency key) are parsed and validated (`invalid_argument`).
   3. The principal assertion is authorized (`permission_denied`).
   4. The deadline is checked (`timeout`).
   5. The request gets the receive check (`invalid_argument`).
   6. The call is dispatched.

   A connector retries only an idempotent action, only on `unavailable`, and only within the call's one deadline, as in gRPC's retry design (A6). It never retries or falls back on `timeout`, whose outcome is unknown.

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
- [gRPC A6, client retries](https://github.com/grpc/proposal/blob/master/A6-client-retries.md): retries within the call's one deadline.
- [python-jsonschema, validating formats](https://python-jsonschema.readthedocs.io/en/stable/validate/#validating-formats): `FormatChecker` and the `format-nongpl` extra.
