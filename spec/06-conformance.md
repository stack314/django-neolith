# 06. Conformance

**Status:** draft. What the suite exercises, its items mapped to requirement IDs, the cases, the reference contract, CNF-1 and CNF-2, the requirements that bind others and claiming conformance are specified.

An implementation conforms to neolith `spec 0.1` when it passes the conformance suite. The suite is part of this repository and is the authoritative meaning of "implements neolith".

## What the suite exercises

1. **Contracts.** A reference contract with every portable type form, an idempotent and a non-idempotent action, and declared errors. Its projection matches the expected OpenAPI document.
2. **In-process transport.** Calls round-trip through serialization; a provider returning a non-portable value fails with `internal` rather than leaking it; deadlines produce `timeout`.
3. **HTTP transport.** Addressing, headers, error statuses and bodies exactly as in [03](03-http-transport.md). The implementation's HTTP provider is driven by the suite's own HTTP caller, and the implementation's HTTP connector is driven against the suite's own reference provider, so that two conformant implementations interoperate.
4. **Routing.** `single` and `local-first`. Under `local-first`, a call moves to the next provider on `unavailable` and for an idempotent action only, within the call's one deadline, at most once to each entry, with the same principal, trace and idempotency key on every attempt, and never on `timeout` or another kind. Each call starts from the first provider. When a call is not moved after an attempt, it ends with that attempt's outcome, apart from the text of a `timeout` after a move (TRN-74). The failover cases run with two providers.
5. **Lifecycle.** A host with an unsatisfied requirement fails to start. A host with a provided contract whose implementation misses an action fails to start. A single app boots alone.
6. **Same semantics.** The same contract test cases run over the in-process and the HTTP transport and produce identical results.
7. **Call context** ([ADR 0017](../docs/adr/0017-call-context-explicit-at-the-action-ambient-for-propagation.md)). A connector call from a thread that runs in a fresh, empty context fails as a mistake in the calling code. A call through a scope that has closed fails the same way, for an action scope and a root. A parent's context rebuilt with a later deadline, another principal or another key derives the same child as the original. A call carrying a principal from a caller the contract does not list is refused with `permission_denied` in-process and over HTTP, and the same call from a listed caller succeeds in both. A call with no principal from an unlisted caller is not refused by the list in either mode. A parent that fans out concurrently and is retried derives the same child keys on both attempts. A call whose principal has an empty subject or a lone surrogate in a string, or a sent form ([03](03-http-transport.md)) whose base64url encoding is over 4096 bytes, fails at the caller with `internal`, in-process and over HTTP. One holding U+FFFF, or whose encoding is exactly 4096 bytes, succeeds in both, and the provider receives the principal decoded from its sent form in both.

## Items and requirements

Most items restate requirements of the other documents, so they carry no ID of their own; CNF-1 and CNF-2 are the two that no other document states. Each item is verified by the cases named beside it ([Cases](#cases)). The last column gives the requirement IDs that state what the item checks; each is among the IDs those cases list, and a case can list others that another item states:

| Item | Cases | Requirements |
|---|---|---|
| 1. Contracts | `contract-values.json`, `contract-errors.json`, and the projection and `deposit` cases of `wire-provider.json` | CNF-2, HTTP-2, HTTP-20, CON-5, CON-16, CON-25 to CON-27, CON-34, CON-35, CON-37, CON-43 |
| 2. In-process transport | `contract-parity.json`, `contract-values.json`, `contract-errors.json`, `contract-deadlines.json` | TRN-1, TRN-4, TRN-14 to TRN-16, TRN-18, TRN-27 to TRN-29, TRN-38, TRN-48, CON-44, CON-46 |
| 3. HTTP transport | `wire-provider.json`, `wire-connector.json` | HTTP-3, HTTP-9 to HTTP-16, HTTP-19, HTTP-21 to HTTP-23, HTTP-28, HTTP-30 to HTTP-38, HTTP-41, HTTP-44 to HTTP-47, TRN-2, TRN-3, TRN-12, TRN-30, TRN-31, TRN-33, CON-42, CON-49 |
| 4. Routing | `routing.json` | CON-4, TRN-8, TRN-17, TRN-18, TRN-38, TRN-68 to TRN-72, APP-63 |
| 5. Lifecycle | `lifecycle.json` | APP-1, APP-16, APP-23, APP-31, APP-33, APP-38 to APP-41, APP-43 to APP-46 |
| 6. Same semantics | every `contract-*.json` case | CNF-1, HTTP-1 |
| 7. Call context | `contract-context.json`, `contract-parity.json` | OVR-6, TRN-2, TRN-23, TRN-25, TRN-26, TRN-30, TRN-36, TRN-39, TRN-42, TRN-43, CON-29, CON-51, HTTP-18, HTTP-42 |

[02](02-transports.md)'s TRN-73, the share of the connect phase, and TRN-74, the text of a `timeout` after a move, have no case: no case on one machine holds a connect phase open, or lets a deadline pass after a move without waiting for it. The implementation's own tests verify them.

## Cases

Every case of the suite has a name and an entry in a case file under [`conformance/cases/`](conformance/cases/), so that another implementation can write its own runner from the files alone. A file is named `<part>.json`, or `<part>-<topic>.json` where a part has more than one file, after the part of the suite its cases belong to: `contract`, `wire`, `routing` or `lifecycle`. Each file holds a JSON array of cases, each an object with exactly these members: `name`, unique across every file; `part`, the file's part; `state`, what holds before the case runs; `procedure`, its steps in numbered prose; `expected`, its outcome; and `requirements`, the requirement IDs it verifies.

- **Contract.** One call, made over the in-process and the HTTP transport, with the outcomes compared (CNF-1), against the reference provider: [`contract-parity.json`](conformance/cases/contract-parity.json), [`contract-values.json`](conformance/cases/contract-values.json), [`contract-errors.json`](conformance/cases/contract-errors.json), [`contract-deadlines.json`](conformance/cases/contract-deadlines.json) and [`contract-context.json`](conformance/cases/contract-context.json).
- **Wire.** The HTTP transport alone, against bytes [03](03-http-transport.md) fixes: the implementation's provider driven by the suite's own caller, and its connector driven against the suite's own scripted provider: [`wire-provider.json`](conformance/cases/wire-provider.json) and [`wire-connector.json`](conformance/cases/wire-connector.json).
- **Routing.** A host configured with one or more remote providers per contract version, against the suite's scripted providers, each of which refuses every connection or answers every request in one fixed way, and records the requests it receives: [`routing.json`](conformance/cases/routing.json).
- **Lifecycle.** Hosts that stop before they serve, one app that boots alone, and Drain: [`lifecycle.json`](conformance/cases/lifecycle.json).

## Reference contract

The reference contract is `reference.v1`. Its definition is the reference document, [`conformance/reference.v1.openapi.json`](conformance/reference.v1.openapi.json): the projection ([03](03-http-transport.md#projection)) of the reference contract alone, as a provider that conforms to `spec 0.1` serves it. The document gives every action, record and kind. The reference provider, which the suite runs, behaves as follows:

| Action | Idempotent | Request → reply | Behaviour of the reference provider |
|---|---|---|---|
| `echo` | yes | `Values` → `Values` | Returns the request. `Values` holds every portable form of [01](01-contracts.md), each once required and once optional. |
| `accept` | no | `Values` → empty | Returns nothing. |
| `refuse` | yes | `Refusal` → `Values` | Fails with the kind the request names, one of `invalid_argument`, `unauthenticated`, `permission_denied`, `not_found`, `conflict` and the declared kind `gone`, with the request's message; `gone` carries the detail `Gone`, whose `reason` is that message. |
| `seen` | yes | `Empty` → `Seen` | Returns what its call context holds: the principal or none, with the principal's claims as the JSON text of an object, the calling app, and whether a deadline and an idempotency key are present. |
| `deposit` | yes | `Deposit` → `Balance` | Adds `amount` to `account` once per account and idempotency key, and returns the total as it stood after the call that first used that pair; a call with no key always adds. |
| `leak` | yes | `Empty` → `Values` | Returns a value that is not of the reply type. |
| `boom` | yes | `Empty` → `Values` | Fails with an error that is not one of the contract's errors. |
| `stall` | yes | `Empty` → `Values` | Returns once its deadline has passed; with no deadline, fails at once with `invalid_argument`. |

A provider's host serves one OpenAPI document for every contract version it provides (HTTP-2), so a host that provides the reference contract beside others serves more than the reference document. Its part for the reference contract is what this section checks: the paths of the reference contract's actions, every component those paths reach, the security schemes the document requires, and the top-level members that the projection's structure fixes ([03](03-http-transport.md#projection)).

**CNF-2.** The OpenAPI document that a provider of the reference contract serves at the projection's location MUST hold the reference contract's part of the reference document unchanged.

Members are compared as JSON values, so the order of an object's members and the spacing are free, a number is compared by its value and never equals `true` or `false`, and a member name given twice is not JSON a reader accepts. Arrays compare in order, apart from `required`, `enum`, `oneOf`, `anyOf` and `allOf`, which [03](03-http-transport.md#projection) does not order and which compare without their order: the same entries, each as many times. Those names are keywords only where a schema or an OpenAPI object has them; a record's field called `required` keeps its schema, and a value given as data (`const`, `default`, `examples`) compares as written. The document can describe other contract versions beside the reference contract, and their paths and components are not compared.

## Same outcome over both transports

**CNF-1.** An implementation MUST give the caller the same outcome over the in-process and the HTTP transport for a call that reaches its provider and returns before its deadline, for a call whose deadline passes first, and for a call the caller refuses before sending it: the same reply, or an error of the same kind and detail and, apart from `internal`, `unavailable` and `timeout`, the same message. A call whose deadline passes first ends in `timeout` over both.

A refusal that this specification places on one transport only is outside that rule. In 0.1 there are four, all on the HTTP transport ([ADR 0033](../docs/adr/0033-eight-points-of-the-http-transport-settled-for-0-1.md) Decision 1): a call from an app that holds no key to sign a shared-secret token with, which the caller refuses (HTTP-18) and the in-process transport serves; a call from an app the provider's host holds no key for, which that host refuses with `unauthenticated` ([ADR 0027](../docs/adr/0027-a-caller-backend-names-the-calling-app-over-http.md)); a request that a provider's host refuses for its body limit (HTTP-40); and a request over plain HTTP to a host that requires HTTPS ([ADR 0011](../docs/adr/0011-the-host-serves-contract-endpoints-itself.md), row 5). A refusal that this specification later places on one transport only joins this list here, never in a test.

## Requirements that bind others

A must-level requirement that binds app code, contract authors or a deployment, and not an implementation, is verified by none of the suite's cases. These are the ones in spec 0.1, and every implementation's run reports them as not applicable; an implementation cannot add to this list:

| ID | Binds |
|---|---|
| OVR-2 | App code |
| OVR-7 | App code |
| CON-1 | Contract authors |
| APP-4 | Deployment |

## Claiming conformance

An implementation states the specification version it targets and publishes its conformance run. It conforms to that version when one run of the suite reports every must-level requirement of every document in the version as passed, apart from those listed under [Requirements that bind others](#requirements-that-bind-others). Partial conformance is not a claim; an implementation either passes the suite for a version or it does not.
