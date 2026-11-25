# 03. HTTP transport

**Status:** draft. Addressing, the call context, errors, sending a call and reading its response, receiving a call, projection, the calling app and the principal, shared-secret tokens and health are specified.

**HTTP-1.** This is the default network transport, REQUIRED in every implementation.

## Addressing

One action is one HTTP endpoint:

```
POST /<name>/v<N>/<action>
```

The request body is the request record as JSON. The reply body is the reply record as JSON. GET is not used for actions, so that actions with side effects and actions without are carried the same way.

## Call context

The context travels in headers. The calling app's credential travels in `Authorization` ([below](#calling-app-and-principal)). Names and semantics are fixed by this document so that implementations interoperate:

| Header | Carries |
|---|---|
| `Neolith-Principal` | The principal on whose behalf the call is made, if there is one ([below](#the-principal)) |
| `Idempotency-Key` | The idempotency key |
| `traceparent` | The trace identifier, per W3C Trace Context |
| `Neolith-Timeout` | The remaining time budget, in whole milliseconds, rounded down |

The deadline is sent as a relative budget, not an absolute time, so that the deadline does not depend on synchronised clocks. The receiving host computes its own local deadline from the budget when the request arrives. Each header follows [ADR 0017](../docs/adr/0017-call-context-explicit-at-the-action-ambient-for-propagation.md), and a provider's host reads them once the calling app is authenticated and admitted ([below](#calling-app-and-principal)). Header names are compared ignoring ASCII case, as HTTP compares them.

**HTTP-21.** A provider's host MUST read a call-context header's value with the OWS at either end removed (RFC 9110, section 5.5), and nothing inside it changed. A value that holds a character outside ASCII has none of the forms below.

`Neolith-Timeout` is 1 to 10 ASCII digits, the first not `0` (`%x31-39 0*9DIGIT`). That is stricter than Connect's `Connect-Timeout-Ms`, a positive integer of at most 10 digits, which does not rule out a leading zero. An absent header means the call has no deadline. A caller never sends 0: a call whose budget rounds down to 0 ms fails with `timeout` before it is sent. In every mode, a budget above 9999999999 ms counts as 9999999999, and a provider's host can cap it lower, at a maximum of its own.

**HTTP-22.** A provider's host MUST fail a call whose `Neolith-Timeout` is present but does not have that form, such as `0050`, with `invalid_argument`.

`Idempotency-Key` carries the call's key: one derived from its parent's key as in [02](02-transports.md), or a random one at a root. The IETF Idempotency-Key draft makes it a structured-field String (RFC 9651); neolith narrows that to a String of 1 to 255 characters, counted after unescaping, with no parameters. A call that arrives without one has no key, so the calls it makes get random keys, and a retry of it does not reproduce them.

**HTTP-23.** A provider's host MUST fail a call whose `Idempotency-Key` is present but does not have that form with `invalid_argument`.

`traceparent` follows W3C Trace Context, including its rules for versions above `00`, and `tracestate` goes with it.

**HTTP-24.** A provider's host MUST start a fresh trace for a call whose `traceparent` is absent or invalid, and drop its `tracestate`; neither fails the call.

**HTTP-25.** With a valid `traceparent`, a provider's host MUST pass on the list-members of a `tracestate` whose value is a W3C Trace Context list of 1 to 32 list-members (section 3.3.1.1), in their order, leaving out the empty ones.

**HTTP-26.** A provider's host MUST shorten such a list that is longer than 512 characters by whole list-members, those longer than 128 characters first, then from the end, until it is at most 512 characters, as W3C Trace Context section 3.3.1.5 asks.

**HTTP-27.** A provider's host MUST drop a `tracestate` that is not such a list, and never fail the call for it.

**HTTP-28.** A provider's host MUST ignore every request header this document does not name, one whose name starts with `Neolith-` included (RFC 9110, section 5.1). Header names that start with `Neolith-` are reserved to this document.

## Errors

Errors are carried as a JSON body with a fixed shape and an HTTP status that follows the kind:

| Kind | Status |
|---|---|
| `invalid_argument` | 400 |
| `unauthenticated` | 401 |
| `permission_denied` | 403 |
| `not_found` | 404 |
| `conflict` | 409 |
| `timeout` | 504 |
| `unavailable` | 503 |
| `internal` | 500 |
| declared kinds | 422 |

Body: `{"kind": "<kind>", "message": "<text>", "data": {<typed detail record>}}`, with `data` present only for a declared kind. A response carries a *neolith error body* when its body is a JSON object whose `kind` is a string. The member for the detail record is named `data`, not `detail`, which RFC 9457 and some web frameworks use for a human-readable explanation.

A *contract path* is the path of an action of a contract version the host provides. A response on any other path carries no neolith error body. The provider's host answers every failure on a contract path with a neolith error body and the status of its kind, including a request that fails validation and a failure of the host itself ([ADR 0008](../docs/adr/0008-typed-contract-errors-in-both-modes.md)). A 401 also carries a `Bearer` challenge in `WWW-Authenticate` (RFC 6750, section 3), since RFC 9110, section 15.5.2, requires a challenge with that status.

**HTTP-19.** A connector MUST take an error's kind from a neolith error body only, never from the status. A response whose status is not a success (2xx) and that carries no neolith error body therefore fails the call with `unavailable`. Examples are a 404 from a host that does not provide that contract version, which is not a contract path, a redirect, or an error page from a proxy. A success response is a reply only as 200 with the reply's JSON text, for an action whose reply is not empty, or as 204, for an action whose reply is empty ([ADR 0011](../docs/adr/0011-the-host-serves-contract-endpoints-itself.md) Decision 4). Any other success response, and one whose body is not a valid reply, fails the call with `internal` (TRN-18), and so does a neolith error body that cannot be read against the contract (CON-49), apart from an error of a kind the caller does not know (CON-28).

A caller reads a reply and a neolith error body as a network receiver reads any text (CON-42), before it reads them against the contract. A body that is not UTF-8, or is nested deeper than the caller's JSON reader admits (RFC 8259, sections 8.1 and 9; CON-52), is not JSON text to the caller, so it carries no neolith error body. Whether a body carries one is judged on its members as written, so a body with `kind` written twice, once as a string, carries one that cannot be read. A neolith error body has exactly the members `kind`, `message` and, for an error that carries a detail, `data`, whose value is the detail record's JSON text as the provider wrote it. One that is not I-JSON, holds another member, or has no `message` that is a string cannot be read. For a kind the caller does not know, a body without `data` is that error without a detail, and a `data` that is not a JSON object, `null` included, is a detail that breaks CON-30, so the caller receives `internal` (CON-49).

## Sending a call and reading its response

A caller writes HTTP/1.1. These rules keep every response the caller accepts to one reading, so that what is left on a connection is never taken for the start of another call's response.

**HTTP-3.** A caller MUST send a request with a `Content-Length` field that gives its body's length, never chunked, and with `Accept-Encoding: identity`, so that a provider answers in no content coding (RFC 9110, section 12.5.3).

**HTTP-4.** A caller MUST NOT follow a redirect or undo a content coding. A redirect is then a non-success response, mapped by HTTP-19 like any other, and a reply in a content coding is not a valid reply ([Errors](#errors)).

**HTTP-5.** A caller MUST read past every interim response (a `1xx` status) other than `101` to the final response, and take the call's outcome from the final response (RFC 9110, section 15.2). A caller can bound how many interim responses it reads; one past its bound, like a `101`, is a response it does not accept (HTTP-6).

**HTTP-6.** A caller MUST fail a call with `unavailable` when the response is not one with a single reading under RFC 9112. That covers a status line other than `HTTP/1.1` or `HTTP/1.0`, a space, three digits and a space; a field line with whitespace before its colon, folded onto the line before, or holding a CR or LF that does not end it; more than one `Content-Length` field, or one that is not all digits; a `Transfer-Encoding` other than `chunked`, or any in an HTTP/1.0 response; `Content-Length` and `Transfer-Encoding` together; a chunk size that is not hexadecimal digits; and a body that ends before its framing says it does. The caller then closes the connection.

**HTTP-17.** A caller MUST read the budget it sends in `Neolith-Timeout` once it has a connection ready to write the request on, so that the time it took to reach the provider is not counted again by the provider's host. A budget under 1 ms then fails the call with `timeout`, unsent (TRN-28).

## Receiving a call

A provider's host *owns* every path that starts with `/<name>/v<N>/`, for a contract version it provides, or with `/.well-known/neolith/`. It compares paths after their percent-encoding is decoded, code point by code point. The *well-known paths* are `/.well-known/neolith/openapi.json` ([Projection](#projection)) and `/.well-known/neolith/health` ([Health](#health)).

**HTTP-29.** A provider's host MUST answer a request on a path it owns whose `Host` names no host it serves without a neolith error body, since such a request is on none of its contract paths.

**HTTP-30.** A provider's host MUST answer a request on a path it owns that is neither a contract path nor a well-known path with status 404 and an empty body, whatever its method. A connector maps it to `unavailable` ([Errors](#errors)).

**HTTP-31.** A provider's host MUST answer `GET` and `HEAD` on a well-known path, and `HEAD` with the status and header fields that `GET` gets and no body, except that a `HEAD` answer can leave out `Content-Length`, as RFC 9110, section 9.3.2, allows a server to.

**HTTP-32.** A provider's host MUST answer any other method on a well-known path with status 405, `Allow: GET, HEAD` and an empty body.

**HTTP-33.** A provider's host MUST answer a request on a well-known path that has a query with status 400 and an empty body.

On a contract path, a provider's host checks a call in the order of [ADR 0017](../docs/adr/0017-call-context-explicit-at-the-action-ambient-for-propagation.md) Decision 9 and stops at the first check that fails (TRN-35). It checks the method and the query within that order's step 3, before the call-context headers, and the body's own header fields within step 5, before the body. A provider's host MUST fail a call on a contract path with `invalid_argument`:

- **HTTP-34.** when its method is not `POST`, with `Allow: POST`;
- **HTTP-35.** when its request target has a query;
- **HTTP-36.** when the value of `Content-Type` it receives is not `application/json`, optionally followed by OWS `;` OWS `charset=` and either the token `utf-8` or the quoted-string `"utf-8"`, with the type, the subtype, the parameter's name and its value compared ignoring ASCII case, with `Accept-Post: application/json`;
- **HTTP-37.** when it has a `Content-Encoding` field, with `Accept-Encoding: identity`;
- **HTTP-38.** when it has no `Content-Length` field, or one whose value, as the host receives it, is not 1 to 19 ASCII digits, which several fields joined with `,` are not;
- **HTTP-39.** when its body, as received, is not as long as its `Content-Length` says.

**HTTP-40.** A provider's host MAY fail a call whose `Content-Length` is above a limit of its own with `invalid_argument`, before it reads the body.

A repeated `Content-Type` or `Content-Length` reaches the host joined with `,` under ASGI, and so fails HTTP-36 or HTTP-38; under WSGI the server decides first ([ADR 0011](../docs/adr/0011-the-host-serves-contract-endpoints-itself.md), a repeated body header): gunicorn and `neolith run` answer it with a 400 of their own, and `runserver` keeps the first field.

The body itself then gets the receive check of [01](01-contracts.md).

**HTTP-41.** A provider's host MUST answer a reply with status 200, `Content-Type: application/json` and the reply's JSON text as the body, and an empty reply with status 204 and no body.

## Projection

**HTTP-2.** A provider MUST serve an OpenAPI 3.1 document describing every contract version it provides, derived from the contracts. The document is the HTTP transport's publication of the contract ([01](01-contracts.md)). Its location is `GET /.well-known/neolith/openapi.json`.

The document is built from the description of each contract version the provider provides ([01](01-contracts.md#publication)) and from the version of this specification that the provider conforms to. It has this structure:

- **Top level.** `openapi` is `3.1.2`. `jsonSchemaDialect` is the description's dialect, JSON Schema 2020-12, so every schema in the document is read as plain JSON Schema. `info` has the title `neolith contracts` and, as its version, the version of this specification that the provider conforms to. There is no `servers` member, so paths are relative to the origin's root.
- **Paths.** Contract versions are ordered by identity, compared code point by code point, and each version's actions keep their declaration order. Each action has the path `/<name>/v<N>/<action>`, which holds one `post` operation with:
  - `operationId` set to `<name>.v<N>.<action>`, and `tags` holding only `<name>.v<N>`;
  - `x-neolith-idempotent`, the action's idempotency as the description states it, `true` or `false`;
  - optional header parameters for the call context: `Neolith-Principal` (base64url characters, at most 4096), `Neolith-Timeout` (its pattern above), `Idempotency-Key` (a String of 1 to 255 characters after unescaping, with no parameters), and `traceparent` and `tracestate` as strings;
  - a required request body of media type `application/json`, referencing the request record;
  - responses: `200` with the reply record, or `204` with no content when the reply is empty; for each standard kind, the status [Errors](#errors) gives it, with an error body of that kind and no `data`; and `422`, only when the action declares kinds, whose error body is a `oneOf` with one member per declared kind: `kind` that kind's name as a constant, `message` a string, and `data` referencing the kind's detail record.
- **Components.** Every record and enum of each version's description is a schema keyed `<name>.v<N>.<Record>`, unchanged apart from each reference `#/$defs/<Record>`, which points to that key instead. One HTTP security scheme, `bearer`, is required by every operation.

**HTTP-20.** The document MUST have the structure that the list above gives.

## Calling app and principal

Two identities cross with a call ([ADR 0010](../docs/adr/0010-pluggable-principal-resolution.md)): the calling app, which the provider's host authenticates, and an optional principal on whose behalf the calling app calls, which the calling app asserts. A *backend* is the part of the provider's host that resolves a call's credentials into the calling app and checks the principal. Other backends, such as one built on mutual TLS or OIDC, can carry both differently. This section fixes the carriage of the shared-secret backend, which every implementation supports.

A host with several backends tries them in a configured order. The first that recognizes a call's credentials decides, by resolving them or refusing the call, and a backend that does not recognize them abstains.

Base64url-encoded values here use the base64url alphabet only (RFC 4648, section 5), with no padding, no whitespace and zero unused bits. JSON objects here meet the I-JSON rule of [ADR 0019](../docs/adr/0019-wire-encodings-of-the-portable-forms.md), so a duplicate member name or a lone surrogate makes one invalid while a noncharacter such as U+FFFF does not, and integers are JSON numbers with no fraction or exponent, within ±(2^53 − 1).

**HTTP-42.** On a contract path, a provider's host MUST authenticate the calling app before it reads the request body or any call-context header.

**HTTP-43.** A provider's host MUST take the time a call arrives, from which it counts the call's budget, before it authenticates the calling app, so that the time authentication takes comes off the budget.

**HTTP-44.** A call on a contract path whose credential is missing, is recognized by no backend or does not resolve, or that reaches a host with no backend, MUST fail with `unauthenticated`.

**HTTP-45.** A provider's host MUST send every response with status 401 with exactly one `WWW-Authenticate` field, whose value is `Bearer realm="neolith"`.

The host runs its other checks, the request body last, in the order of [ADR 0017](../docs/adr/0017-call-context-explicit-at-the-action-ambient-for-propagation.md) Decision 9 ([Receiving a call](#receiving-a-call)). A request on any other path is answered as the Errors section says. In-process there are no credentials: the calling app is the app the call is made from. A call made from no app fails with `unauthenticated` on every transport.

**HTTP-18.** A caller MUST fail a call with `unauthenticated`, without sending it, when the call has no calling app or its calling app holds no key to sign a shared-secret token with. A call with no calling app so gets the outcome it gets in-process (TRN-42), and no call reaches a provider without a credential.

### The principal

A *principal object* is a JSON object with exactly the members `issuer` and `subject`, both non-empty strings, and optionally `claims`, an object whose values are strings, integers, booleans or lists of strings. An absent `claims` is the same as an empty one. Two principals are equal when their members are equal, with strings compared code point by code point and without Unicode normalization, and lists compared in order. A subject is unique only within its issuer, so code that stores a principal stores both.

With the shared-secret backend, `Neolith-Principal` is present exactly when the call has a principal, and carries it as a bare assertion: a principal object, base64url-encoded, with the encoding at most 4096 bytes. A caller sends the object's *sent form*: its JCS serialization (RFC 8785) in UTF-8, with `claims` left out when empty. A provider accepts any serialization that meets these rules. No valid serialization of a principal object is shorter than its sent form, so a principal that arrived in a valid header still fits when the provider passes it on in calls of its own. A header with an empty value counts as present. A bare assertion that is not a principal object, encoded as above, fails the call with `invalid_argument`, once the calling app has been authenticated and found on the allow-list below. A neolith caller never sends one, because it checks the principal's sent form against these rules first, in every mode, in the order of [ADR 0017](../docs/adr/0017-call-context-explicit-at-the-action-ambient-for-propagation.md) Decision 9. A provider receives the principal decoded from that form.

The principal is not signed: the authenticated calling app asserts it, and it never goes inside the shared-secret token. A provider accepts `Neolith-Principal` only from a calling app on its allow-list of apps that may assert a principal. A call from any other calling app that carries the header fails with `permission_denied`, whatever the header holds.

**HTTP-46.** A provider's host MUST read `Neolith-Principal` only once the calling app is authenticated and found on the allow-list, so a call from any other calling app that carries the header fails with `permission_denied` (TRN-43) however the header is malformed.

A later backend can carry a signed delegation token in this header instead of the bare assertion, under its own format.

For example, the principal with issuer `https://id.example.com`, subject `u-81f3` and the claim `roles` holding `["clerk"]` has the sent form `{"claims":{"roles":["clerk"]},"issuer":"https://id.example.com","subject":"u-81f3"}` and is sent as:

```
Neolith-Principal: eyJjbGFpbXMiOnsicm9sZXMiOlsiY2xlcmsiXX0sImlzc3VlciI6Imh0dHBzOi8vaWQuZXhhbXBsZS5jb20iLCJzdWJqZWN0IjoidS04MWYzIn0
```

### Shared-secret tokens

The shared-secret token is for development only. An implementation supports it on both the caller's and the provider's side, and enables it only when configured to. A calling app's secret is held by that app and by every provider it calls, and any holder can mint tokens for that app. A token is a bearer token: anyone who sees one can call as the calling app until the provider stops accepting it, and assert any principal wherever that app is on the allow-list. RFC 6750, section 5.3, requires TLS for bearer tokens. Development setups often use plain HTTP on a private network, and anyone who can watch that network can replay a token.

Each calling app has one or more secrets, each named by a key id that is a non-empty string, unique among that app's keys. More than one key lets a secret be replaced without downtime. A secret is at least 32 bytes (RFC 7518, section 3.2), generated at random (RFC 8725, section 3.5).

The token travels as `Authorization: Bearer <token>`. It is a JSON Web Token (RFC 7519) in JWS compact serialization (RFC 7515), signed with HMAC SHA-256 (`HS256`) under one of the calling app's secrets. It is three base64url-encoded parts joined by `.`: the JOSE header, the claims, and the signature, computed over the first two parts joined by `.`, exactly as sent (RFC 7515, section 5.1). Times are integers, in seconds since the epoch.

| Member | Part | Value |
|---|---|---|
| `alg` | header | `"HS256"` |
| `typ` | header | `"neolith+jwt"`, so that the token cannot be taken for a JWT of another kind (RFC 8725, section 3.11) |
| `kid` | header | The key id of the secret that signed the token |
| `iss` | claims | The calling app's `name` from its declaration ([05](05-app-declaration.md)) |
| `aud` | claims | One string: the identity of the contract being called, such as `inventory.v1` |
| `iat` | claims | When the token was issued |
| `exp` | claims | When the token expires, at most 300 seconds after `iat` |

The shared-secret backend recognizes a call whose `Authorization` uses the `Bearer` scheme (compared ignoring ASCII case) with a token whose first part decodes, under the encoding rules above, to a JSON object whose `typ` is `neolith+jwt` or `application/neolith+jwt`, ignoring ASCII case. **HTTP-7.** It MUST abstain on any other credentials, including a token whose first part breaks those rules, so a later backend can try them. A first part nested deeper than the host's JSON reader admits (RFC 8259, section 9, lets a parser limit nesting) counts as one that does not decode, so the backend abstains on it; this changes no valid token, whose first part is three string members with no nesting.

A provider's host MUST accept a recognized token as valid only when all of these hold, with *now* the provider host's current time in seconds since the epoch:

- **HTTP-8.** it is at most 4096 bytes, and its three parts are base64url-encoded, the first two as JSON objects;
- **HTTP-9.** the header has exactly the members `alg`, `typ` and `kid`, and the claims exactly `iss`, `aud`, `iat` and `exp`, each of the type in the table;
- **HTTP-10.** `alg` is `HS256`;
- **HTTP-11.** `typ` is `neolith+jwt` or `application/neolith+jwt`, ignoring ASCII case (RFC 7515, section 4.1.9);
- **HTTP-12.** `kid` names a key the host holds for the app named by `iss`;
- **HTTP-13.** the signature is the HMAC SHA-256 of the first two parts joined by `.`, as sent, under that key's secret, compared in constant time;
- **HTTP-14.** `aud` is the identity of the contract in the request path;
- **HTTP-15.** `iat` < `exp` ≤ `iat` + 300, `iat` ≤ *now* + 60, and *now* < `exp` + 60.

**HTTP-16.** A recognized token that is not valid MUST fail the call with `unauthenticated`, with no fallback to a later backend. The token identifies the calling app only; it never carries the end user. A caller can reuse a token for calls to the same contract while it is valid, so a provider does not refuse a token for having seen it before.

The audience names the contract, which both sides know from the request path. It keeps a token from being used at another contract. It does not keep a token seen at one provider of a contract from being replayed at another provider of the same contract, such as the attached and detached providers of one app, until it stops being valid. With a shared secret, every provider that checks a calling app's tokens could also mint them.

The 60 seconds allowed each way absorb differences between the hosts' clocks, so a host accepts a token for at most 420 seconds of its own clock. Unlike the deadline, the token depends on the clocks agreeing within that margin.

For example, with the example secret `000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f` (hexadecimal, not random) under the key id `orders-1`, the app `orders` calling `inventory.v1` with `iat` 1767225600 and `exp` 1767225900 sends:

```
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6Im5lb2xpdGgrand0Iiwia2lkIjoib3JkZXJzLTEifQ.eyJpc3MiOiJvcmRlcnMiLCJhdWQiOiJpbnZlbnRvcnkudjEiLCJpYXQiOjE3NjcyMjU2MDAsImV4cCI6MTc2NzIyNTkwMH0.AhMk-cm_SInC6ek-6CLP8pN7ciQFHuGb-i2MSGu3efU
```

## Health

**HTTP-47.** A provider's host MUST answer `GET /.well-known/neolith/health` with status 200 and an empty body only once it can serve its contracts: the path means ready, not merely running.
