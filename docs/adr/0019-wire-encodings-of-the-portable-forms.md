# 0019. Wire encodings of the portable forms

- **Status:** Accepted
- **Accepted:** 2026-11-11
- **Date:** 2026-11-11

## Context

Spec 01 names the portable forms but not how each is written in JSON. Several forms have more than one common encoding: a 64-bit integer as a number or a string, bytes in either base64 alphabet, a duration as RFC 3339 text or as seconds, a timestamp with an offset in seconds. Two implementations that pick differently cannot interoperate, and a decimal cannot be admitted until its form is fixed and implemented (CON-21).

The published description (CON-16) is JSON Schema 2020-12. In that draft, `format` and `contentEncoding` are annotations: a validator is not required to check them (JSON Schema Validation, sections 7.2.1 and 8.1). A description that says only `"format": "date-time"` therefore admits any string for most readers. Regular expressions differ too: pydantic-core uses Rust regex, JSON Schema names ECMA-262, and many validators use Python `re`, which disagree on anchors, `\d` and Unicode classes.

Integers are a second trap. A JSON number above 2^53 loses precision in JavaScript and in any reader that parses numbers as doubles, and I-JSON (RFC 7493) asks senders not to rely on more.

## Options

1. **Formats only.** Short descriptions, but most readers check nothing, so the description says less than the provider enforces.
2. **ProtoJSON.** A complete, documented mapping, with 64-bit integers as strings and durations as `"1.500s"`. It writes every int64 as a string, which most JSON callers then have to convert.
3. **An explicit pattern or range for every encoded form**, borrowing ProtoJSON's duration form and RFC 3339 for times, with integers as JSON numbers in the range every JSON reader holds exactly.

## Decision

Option 3. Every encoded form carries a `pattern`, and integers carry `minimum` and `maximum`. A sender writes exactly the forms below, and a receiver refuses any value that its form's pattern or range refuses. Patterns use an ASCII-only subset, anchored with `^…$`, that matches the same strings under ECMA-262, Rust regex and Python's `re.fullmatch`. A validator that matches with Python's `re.search`, as python-jsonschema does, also admits a final newline (`"1.500s\n"`), because there `$` matches before one; receivers match the whole string and refuse it. Bodies are I-JSON (RFC 7493).

- **integer:** a JSON number within ±(2^53 − 1), stated as `minimum` and `maximum`. A sender writes it with no fraction or exponent; JSON Schema and receivers read `1.0` as the same integer. A 64-bit identifier travels as a string. Widening the range later is additive; narrowing it would break callers, so the narrow range comes first. ProtoJSON's int64-as-string covers the full range, at the cost of a string for every integer field.
- **bytes:** base64url with padding and zero unused bits (RFC 4648, sections 3.5 and 5; I-JSON section 4.4), the only form a receiver accepts. The description says `contentEncoding: base64url` with a pattern, not Pydantic's `format: base64url`, which the OpenAPI format registry advises against in favour of `contentEncoding`. Pydantic 2.12.0 and 2.14.3 write this form and refuse unused bits that are not zero. They also read unpadded and standard-alphabet input, so the pattern is what refuses those.
- **number:** finite only, refused on both sides. Infinity and NaN are never written as `null` or as strings. `-0` and `0` are the same value on the wire, and a sender writes `0`, so that a value has one encoding and hashes the same everywhere (RFC 8785 writes both as `0`).
- **decimal:** a string with no exponent, no leading zeros in its integer part and no negative zero at any scale (`-0.00` is written `0.00`). Trailing zeros are kept (`"1.50"` stays `"1.50"`), and a decimal with a positive exponent is written at exponent 0, with its value kept (`1E+2` as `"100"`). Counted on the written string, it has at most 34 significant digits, from its first non-zero digit to its last digit, and at most 6176 digits after the point, so that every value is an IEEE 754 decimal128. CON-21 is withdrawn when the implementation admits decimals.
- **duration:** seconds with an `s` suffix, as ProtoJSON writes them, down to the microsecond: no fraction, or 3 or 6 fractional digits, as few as the value needs (`0s`, `-2s`, `1.500s`, `0.000001s`). The pattern is `^(0|-?(0|[1-9][0-9]{0,11})\.([0-9]{3})?(00[1-9]|0[1-9][0-9]|[1-9][0-9]{2})|-?[1-9][0-9]{0,11})s$`, so a duration has one encoding. A ProtoJSON library writes the same text for any whole number of microseconds; a finer duration needs 9 digits and is refused. The range is ±315576000000 seconds (ten thousand years), as in ProtoJSON. It is not RFC 3339's duration.
- **timestamp and date:** RFC 3339, with an offset required on a timestamp, years 0001 to 9999, no leap second and at most 6 fractional digits. A timestamp is an instant; its offset is not data, and a receiver does not rely on it. RFC 3339 writes offsets in hours and minutes only (section 5.6), so a timestamp whose offset has seconds is normalized to UTC on send and written with `Z`, which RFC 9557 (section 2.2) reads as "local offset unknown": an honest reading once the offset is dropped.
- **aliases:** refused in 0.1. A field's wire name is its name.
- **What a description cannot state.** The patterns and ranges state every rule above except four, which only an implementation of this specification checks, on send and on receive: a duration's range, beyond the 12 integer digits the pattern allows; a decimal's count of significant digits, and of digits after its point; and calendar validity (`2027-02-29`). Two rules bind the sender only, because a receiver reads the other spelling as the same value: an integer with no fraction or exponent, and `0` for a number's `-0`.
- **When it takes effect.** These encodings take effect in the specification with roadmap step 3 (#10 task 1), whose pull request implements them. Until then CON-21, which refuses decimals, stands as a temporary restriction, and that pull request withdraws it.
- **Canonical form:** where a value is hashed, as for the idempotency keys of [0017](0017-call-context-explicit-at-the-action-ambient-for-propagation.md), its wire form is canonicalized with the JSON Canonicalization Scheme (RFC 8785), unchanged.

Follows principles 8 (*one test suite over many code paths*) and 11 (*specification over implementation*).

## Consequences

- A JSON Schema validator in any language checks each form's pattern and range without format extensions. That covers every rule except the four listed above.
- A contract with a 64-bit identifier declares it as a string. Integers beyond ±(2^53 − 1) are refused on both sides.
- Until #10 task 1, spec 01 describes the encodings as future, and the implementation keeps refusing decimals under CON-21.
- The idempotency-key derivation of [0017](0017-call-context-explicit-at-the-action-ambient-for-propagation.md) hashes a request's wire form, so it relies on these encodings, I-JSON bodies especially.
- A duration in Python's default ISO 8601 form (`PT1.5S`), or written `1.5s`, is refused; its form is `1.500s`.
- Pydantic's default serializer writes bytes as UTF-8 text, a duration as `PT1.5S`, and some decimals with an exponent (`0.0000001` as `1E-7`, `0.00000000` as `0E-8`). No conversion of a value before the dump can change these, so these forms come from the schema the serializer is built from, through a type or setting neolith provides. Which one changes the public API, and is an open design point on #10.

## References

- [JSON Schema Validation 2020-12](https://json-schema.org/draft/2020-12/json-schema-validation), sections 7.2.1 (format as an annotation) and 8.1 (content as an annotation).
- [RFC 7493](https://www.rfc-editor.org/rfc/rfc7493), I-JSON, sections 2.2 (numbers) and 4.4 (binary data); [RFC 9557](https://www.rfc-editor.org/rfc/rfc9557), section 2.2 (`Z` as an unknown local offset); [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785), JSON Canonicalization Scheme; [RFC 3339](https://www.rfc-editor.org/rfc/rfc3339); [RFC 4648](https://www.rfc-editor.org/rfc/rfc4648).
- [ProtoJSON format](https://protobuf.dev/programming-guides/json/): int64 as strings, `Duration` as `"1.500s"`, with 0, 3, 6 or 9 fractional digits.
- [OpenAPI format registry, `base64url`](https://spec.openapis.org/registry/format/base64url.html): use `contentEncoding` instead.
- [IEEE 754-2019](https://standards.ieee.org/ieee/754/6210/), decimal128: 34 significant digits.
