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

Option 3. Every encoded form carries a `pattern`, and integers and numbers carry `minimum` and `maximum`. A sender writes exactly the forms below, and a receiver refuses any value that its form's pattern or range refuses. Patterns use an ASCII-only subset, anchored with `^…$`, that matches the same strings under ECMA-262 (with no flag, `u` or `v`), Rust regex and Python's `re.fullmatch`. A validator that matches with Python's `re.search`, as python-jsonschema does, also admits a final newline (`"1.500s\n"`), because there `$` matches before one; receivers match the whole string and refuse it.

- **I-JSON:** a body meets the constraints that RFC 8785 (section 3.1) puts on canonical input, as spec 03's objects do: no duplicate member name, no lone surrogate, and numbers that a double holds. A sender never writes a body that breaks them, and a receiver refuses one. Text may hold a noncharacter, as CON-25 and CON-30 allow, although I-JSON (RFC 7493, section 2.1) excludes it.
- **integer:** a JSON number within ±(2^53 − 1), stated as `minimum` and `maximum`. A sender writes it with no fraction or exponent, and a receiver refuses one written otherwise, as spec 03 does. A 64-bit identifier travels as a string. This is the widest range every JSON reader holds exactly, and changing it later in either direction would break callers or receivers, so it is fixed now. ProtoJSON's int64-as-string covers the full range, at the cost of a string for every integer field.
- **bytes:** base64url with padding and zero unused bits (RFC 4648, sections 3.5 and 5; I-JSON section 4.4): `^([A-Za-z0-9_\-]{4})*([A-Za-z0-9_\-][AQgw]==|[A-Za-z0-9_\-]{2}[AEIMQUYcgkosw048]=)?$`. The description says `contentEncoding: base64url` with that pattern, not Pydantic's `format: base64url`, which the OpenAPI format registry advises against in favour of `contentEncoding`. Pydantic 2.12.0 and 2.14.3 write this form and refuse unused bits that are not zero. They also read unpadded and standard-alphabet input, so the pattern is what refuses those.
- **number:** finite only, with `minimum` and `maximum` of ±1.7976931348623157e308, the largest double, so that a reader that turns `1e400` into infinity refuses it. Infinity and NaN are never written as `null` or as strings. `-0` and `0` are the same value on the wire, and a sender writes `0`, as RFC 8785 does.
- **decimal:** a string with an optional `-`, digits with no leading zero, and optionally a point followed by at least one digit, with no exponent and no negative zero at any scale (`-0.00` is written `0.00`): `^(0(\.[0-9]+)?|-?[1-9][0-9]*(\.[0-9]+)?|-?0\.0*[1-9][0-9]*)$`. It has at most 34 digits, not counting the sign and the point, which a second pattern states in an `allOf`: `^-?[0-9](\.?[0-9]){0,33}$`. So every value is an IEEE 754 decimal128, below 10^34 in magnitude. Trailing zeros are kept (`"1.50"` stays `"1.50"`), and a decimal with a positive exponent is written at exponent 0, with its value kept (`1E+2` as `"100"`). CON-21 is withdrawn when the implementation admits decimals.
- **duration:** seconds with an `s` suffix, as ProtoJSON writes them, down to the microsecond: no fraction, or 3 or 6 fractional digits, as few as the value needs (`0s`, `-2s`, `1.500s`, `0.000001s`). Its magnitude is below 10^11 seconds, about 3,168 years, inside ProtoJSON's range of ±315576000000 seconds: `^(0|-?(0|[1-9][0-9]{0,10})\.([0-9]{3})?(00[1-9]|0[1-9][0-9]|[1-9][0-9]{2})|-?[1-9][0-9]{0,10})s$`. So a duration has one encoding, and the pattern states its range. A ProtoJSON library writes the same text for any whole number of microseconds in that range; a finer duration needs 9 digits and is refused. It is not the RFC 3339 duration that I-JSON (RFC 7493, section 4.3) recommends, whose grammar has neither a sign nor fractions of a second.
- **timestamp and date:** RFC 3339, with `T` and `Z` in upper case, an offset required on a timestamp, years 0001 to 9999, no leap second and at most 6 fractional digits: `^(000[1-9]|00[1-9][0-9]|0[1-9][0-9]{2}|[1-9][0-9]{3})-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](\.[0-9]{1,6})?(Z|[+\-]([01][0-9]|2[0-3]):[0-5][0-9])$`. A date is the part before the `T`. The instant a timestamp names also falls in years 0001 to 9999 in UTC, as a protobuf `Timestamp` requires. The description also carries `format`, `date-time` or `date`. A timestamp is an instant; its offset is not data, and a receiver does not rely on it. RFC 3339 writes offsets in hours and minutes only (section 5.6), so a timestamp whose offset is not a whole number of minutes is converted to UTC on send and written with `Z`, which RFC 9557 (section 2.2) reads as "local offset unknown": an honest reading once the offset is dropped.
- **aliases:** refused in 0.1. A field's wire name is its name.
- **What the description does not state.** Its patterns and ranges state every rule above except four, which only an implementation of this specification checks, on send and on receive: calendar validity (`2027-02-29`), left to `format`, which few validators check, because a pattern for it runs to hundreds of characters; a timestamp's range in UTC; an integer written with a fraction or an exponent, which JSON Schema counts as an integer when the fraction is zero (`1.0`); and the I-JSON constraints, which concern a body's text rather than the values a validator sees. One rule binds the sender only, because a receiver reads the other spelling as the same value: `0` for a number's `-0`.
- **When it takes effect.** These encodings take effect in the specification with roadmap step 3 (#10 task 1), whose pull request implements them. Until then CON-21, which refuses decimals, and CON-32, which refuses bytes in a detail record, stand as temporary restrictions, and that pull request withdraws or replaces them.
- **Canonical form:** where a value is hashed, as for the idempotency keys of [0017](0017-call-context-explicit-at-the-action-ambient-for-propagation.md), its wire form is canonicalized with the JSON Canonicalization Scheme (RFC 8785), unchanged.

Follows principles 8 (*one test suite over many code paths*) and 11 (*specification over implementation*).

## Consequences

- A JSON Schema validator in any language checks each form's pattern and range without format extensions. That covers every rule except the four listed above, though a validator that matches with Python's `re.search` also admits a final newline, and one built on V8 runs out of stack on a bytes value of about 4 MB of text.
- A contract with a 64-bit identifier declares it as a string. Integers beyond ±(2^53 − 1) are refused on both sides.
- Until #10 task 1, spec 01 describes the encodings as future, and the implementation keeps refusing decimals under CON-21 and bytes in a detail record under CON-32.
- The idempotency-key derivation of [0017](0017-call-context-explicit-at-the-action-ambient-for-propagation.md) hashes a request's wire form, so it relies on these encodings, I-JSON bodies especially.
- A duration in Python's default ISO 8601 form (`PT1.5S`), or written `1.5s`, is refused; its form is `1.500s`.
- Pydantic's default serializer writes bytes as UTF-8 text, a duration as `PT1.5S`, and with an exponent a decimal that is nonzero and below 0.000001 in magnitude, or zero with more than 6 fractional digits (`0.0000001` as `1E-7`, `0.0000000` as `0E-7`). No conversion of a duration or of those decimals before the dump changes that, so their forms come from the schema the serializer is built from, through a type or setting neolith provides; the bytes form can come from it too, through `ser_json_bytes` and, for reading, `val_json_bytes`, since the default validator reads `"-_8="` as those four bytes. Which one changes the public API, and is an open design point on #10.

## References

- [JSON Schema Validation 2020-12](https://json-schema.org/draft/2020-12/json-schema-validation), sections 7.2.1 (format as an annotation) and 8.1 (content as an annotation).
- [RFC 7493](https://www.rfc-editor.org/rfc/rfc7493), I-JSON, sections 2.1 (characters), 2.2 (numbers), 4.3 (times and durations) and 4.4 (binary data); [RFC 9557](https://www.rfc-editor.org/rfc/rfc9557), section 2.2 (`Z` as an unknown local offset); [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785), JSON Canonicalization Scheme, section 3.1 (input constraints); [RFC 3339](https://www.rfc-editor.org/rfc/rfc3339); [RFC 4648](https://www.rfc-editor.org/rfc/rfc4648).
- [ProtoJSON format](https://protobuf.dev/programming-guides/json/): int64 as strings, `Duration` as `"1.500s"`, with 0, 3, 6 or 9 fractional digits, and `Timestamp` from year 0001 to 9999.
- [OpenAPI format registry, `base64url`](https://spec.openapis.org/registry/format/base64url.html): use `contentEncoding` instead.
- [IEEE 754-2019](https://standards.ieee.org/ieee/754/6210/), decimal128: 34 significant digits.
