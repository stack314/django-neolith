# 0020. A portable form whose wire spelling differs from Pydantic's default is a neolith Annotated type

- **Status:** Accepted
- **Accepted:** 2026-11-11
- **Date:** 2026-11-11

## Context

[ADR 0019](0019-wire-encodings-of-the-portable-forms.md) fixes how each portable form is written in JSON, and leaves one point open: three forms come out of Pydantic in another spelling, so they need "a type or setting neolith provides". Pydantic writes a `timedelta` as ISO 8601 text (`"PT1.5S"`) or as a number, a `Decimal` as Python's `str` (`"1E-7"`, `"1E+2"`, `"-0"`), and `bytes` as UTF-8 text. [ADR 0007](0007-in-process-transport-serializes.md) Decision 1 dumps a value with the declared type's own serializer, so the spelling has to come from the type's schema, not from a conversion before the dump.

At Pydantic 2.12.0 and 2.14.3, no setting writes these forms exactly. `ser_json_timedelta` and `ser_json_temporal` write `"PT1.5S"`, `1.5` or `1500.0`. Nothing changes how a decimal is written. `ser_json_bytes="base64"` writes padded base64url, but `val_json_bytes="base64"` also reads the standard alphabet and unpadded text, and both are settings on the adopter's own record. Pydantic's `Base64UrlBytes` reads `_w=!=` and `_w==\n` as `b"\xff"`, and from Python it decodes the bytes it is given.

## Options

1. **Plain types, with record settings.** Authors keep writing `timedelta`, `Decimal` and `bytes`. Only bytes have a setting, which reads more than the form and changes the adopter's own classes (CONTRIBUTING K6). Durations and decimals would still have no way to be written in their form.
2. **Wrapper classes.** A `neolith.Duration` class holding a `timedelta`. Its schema can be exact, but every value an author handles is neolith's type, not the standard one, and code converts at each boundary.
3. **Defer.** Keep refusing durations, small decimals and bytes until later. Spec 01 lists them as portable, so contracts would carry integer fields or strings in their place, and v1 contracts would keep them.
4. **`Annotated` aliases.** A neolith type is `Annotated[timedelta, marker]`, whose marker brings its own validator, serializer and JSON Schema. Values stay the standard types.

## Decision

Option 4, from the design debate on #10 ("Design debate outcome: the Python type for durations"). A recorded dissent there, citing prior art that exposes plain `timedelta` (Temporal, Dapr, msgspec, FastAPI, Django), was withdrawn once the type was an `Annotated` alias rather than a wrapper class.

- **The types:** `neolith.Duration` (`timedelta`), `neolith.Decimal128` (`Decimal`) and `neolith.Binary` (`bytes`). No name shadows the standard type it annotates. `Decimal128` names the admitted set: fixed-point decimals within decimal128's 34-digit precision. `Binary` is the name schema languages give opaque bytes.
- **Reading and writing:** from JSON, each reads only its form from ADR 0019, matched over the whole string, and writes that form exactly, one spelling per value. From Python, `Duration` and `Decimal128` take a value of their type or a string in their form. `Binary` takes `bytes` only: a plain `bytes` field reads a string as its UTF-8 bytes, so reading the same call's string as base64url would silently change what it holds. Its JSON Schema states the form's pattern in validation and serialization modes, so Pydantic's own schema agrees with the wire.
- **Admission by identity:** the contract checker admits the core schema that a neolith marker builds, with neolith's own reader and writer, and nothing else that resembles it. A second serializer or a constraint on one of these types is refused, as any other is.
- **Plain types are refused:** a contract whose record uses a plain `timedelta`, `Decimal` or `bytes` is refused when it is declared, with a message that names the neolith type to use.

This settles the open design point of ADR 0019. Follows principles 5 (*same semantics over same mechanics*) and 11 (*specification over implementation*).

## Consequences

- Authors write `neolith.Duration` where they would write `timedelta`, and handle plain `timedelta` values. The three names are public API, a one-way door accepted knowingly.
- A field annotated with a plain type fails when the contract is declared, so the change is visible at once rather than on the wire (CONTRIBUTING K2).
- Pydantic's own constraints on these types, such as `max_length` on bytes, are refused: Pydantic applies them in a validator after the type's own, which the checker refuses like any other.
- `model_dump_json`, `model_json_schema` and anything built on them agree with the wire for these forms, without settings on the adopter's records.
- A `timedelta` may reach 999,999,999 days, beyond ADR 0019's range. `neolith.Duration` refuses a value of 10^11 seconds or more, and the send-side check of #10 refuses one set after validation.

## References

- [Pydantic, custom types with `Annotated`](https://docs.pydantic.dev/latest/concepts/types/#custom-types) and `__get_pydantic_core_schema__`.
- [ProtoJSON format](https://protobuf.dev/programming-guides/json/): `Duration` as `"1.500s"`.
- [IEEE 754-2019](https://standards.ieee.org/ieee/754/6210/), decimal128: 34 significant digits.
