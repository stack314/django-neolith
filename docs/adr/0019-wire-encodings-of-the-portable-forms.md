# 0019. Wire encodings of the portable forms

- **Status:** Accepted
- **Accepted:** 2026-11-11
- **Date:** 2026-11-11

## Context

Spec 01 names the portable forms but not how each is written in JSON. Several forms have more than one common encoding: a 64-bit integer as a number or a string, bytes in either base64 alphabet, a duration as RFC 3339 text or as seconds, a timestamp with an offset in seconds. Two implementations that pick differently cannot interoperate, and a decimal cannot be admitted at all until its form is fixed (CON-21).

The published description (CON-16) is JSON Schema 2020-12. In that draft, `format` and `contentEncoding` are annotations: a validator is not required to check them (JSON Schema Validation, sections 7.2.1 and 8.1). A description that says only `"format": "date-time"` therefore admits any string for most readers. Regular expressions differ too: pydantic-core uses Rust regex, JSON Schema names ECMA-262, and many validators use Python `re`, which disagree on anchors, `\d` and Unicode classes.

Integers are a second trap. A JSON number above 2^53 loses precision in JavaScript and in any reader that parses numbers as doubles, and I-JSON (RFC 7493) asks senders not to rely on more.

## Options

1. **Formats only.** Short descriptions, but most readers check nothing, so the description says less than the provider enforces.
2. **ProtoJSON.** A complete, documented mapping, with 64-bit integers as strings and durations as `"1.5s"`. It writes every int64 as a string, which most JSON callers then have to convert.
3. **An explicit pattern or range for every encoded form**, borrowing ProtoJSON's duration form and RFC 3339 for times, with integers as JSON numbers in the range every JSON reader holds exactly.

## Decision

Option 3. Every encoded form carries a `pattern`, and integers carry `minimum` and `maximum`, so that the description states every rule. Patterns use an ASCII-only subset, anchored with `^…$`, that matches the same strings under ECMA-262, Rust regex and Python's `re.fullmatch`. Bodies are I-JSON (RFC 7493).

- **integer:** a JSON number with no fraction or exponent, within ±(2^53 − 1). A 64-bit identifier travels as a string. Widening the range later is additive; narrowing it would break callers, so the narrow range comes first. ProtoJSON's int64-as-string covers the full range, at the cost of a string for every integer field.
- **bytes:** base64url with padding (RFC 4648, section 5). A receiver accepts both base64 alphabets and requires padding. Pydantic 2.12.0 and 2.14.3 emit this form; they also read unpadded input, so the description's pattern is what requires padding.
- **number:** finite only, refused on both sides. Infinity and NaN are never written as `null` or as strings.
- **decimal:** a string with no exponent, no leading zeros and no `-0`, with at most 34 significant digits (IEEE 754 decimal128) and its scale preserved (`"1.50"` stays `"1.50"`). This lifts CON-21.
- **duration:** `-?[0-9]+(\.[0-9]{1,6})?s`, the ProtoJSON form, within ±315576000000 seconds (ten thousand years). It is not RFC 3339's duration.
- **timestamp and date:** RFC 3339, with an offset required on a timestamp, years 0001 to 9999, no leap second and at most 6 fractional digits. A timestamp whose offset has seconds is converted to UTC on send, since RFC 3339 writes offsets in minutes.
- **aliases:** refused in 0.1. A field's wire name is its name.
- **Canonical form:** where a value is hashed, as for the idempotency keys of [0017](0017-call-context-explicit-at-the-action-ambient-for-propagation.md), its wire form is canonicalized with the JSON Canonicalization Scheme (RFC 8785), unchanged.

Follows principles 8 (*one test suite over many code paths*) and 11 (*specification over implementation*).

## Consequences

- Any JSON Schema validator in any language checks what the provider enforces, without format extensions.
- A contract with a 64-bit identifier declares it as a string. Integers beyond ±(2^53 − 1) are refused on both sides.
- Decimals become usable. Spec 01 withdraws CON-21 with this record, and the implementation admits decimals in roadmap step 3 (#10). Until then it still refuses them, and it does not yet check the other encodings either.
- A duration written by a Python program in its default ISO 8601 form is not accepted; the implementation writes the ProtoJSON form.
- Spec 01 states each encoding as a requirement, and the description and validation implement it, in roadmap step 3 (#10).

## References

- [JSON Schema Validation 2020-12](https://json-schema.org/draft/2020-12/json-schema-validation), sections 7.2.1 (format as an annotation) and 8.1 (content as an annotation).
- [RFC 7493](https://www.rfc-editor.org/rfc/rfc7493), I-JSON; [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785), JSON Canonicalization Scheme; [RFC 3339](https://www.rfc-editor.org/rfc/rfc3339); [RFC 4648](https://www.rfc-editor.org/rfc/rfc4648).
- [ProtoJSON format](https://protobuf.dev/programming-guides/json/): int64 as strings, `Duration` as `"1.5s"`.
- [IEEE 754-2019](https://standards.ieee.org/ieee/754/6210/), decimal128: 34 significant digits.
