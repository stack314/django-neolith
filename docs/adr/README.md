# Architecture decision records

An ADR captures one significant decision: the context, the options considered, what we chose, and the consequences. We use a short version of [MADR](https://adr.github.io/madr/) (see [`template.md`](template.md)).

## How we use them

- **When to write one:** a decision is hard to reverse, shapes public API, or will be questioned later. Small, reversible choices do not need one.
- **Numbering:** sequential, `NNNN-short-title.md`. Numbers are never reused.
- **Status:** `Proposed` → `Accepted` (or `Rejected`). A later decision that reverses one is a new ADR. It marks the old one `Superseded by NNNN` and links back. To narrow part of it, the new record quotes the sentences it narrows, and the old record gains a line under its status, `Narrowed by NNNN (Decision k)`; nothing else in it changes. We never delete or rewrite an accepted ADR's decision.
- **Who decides:** anyone can propose. The maintainer accepts.

Questions not yet decided are in [open-questions.md](../open-questions.md).

## Index

| # | Decision | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](0002-django-app-is-the-unit-of-deployment.md) | The Django app is the unit of deployment | Accepted |
| [0003](0003-apps-communicate-through-contracts.md) | Apps communicate only through contracts and connectors | Accepted |
| [0004](0004-each-app-owns-its-data.md) | Each app owns its data | Accepted |
| [0005](0005-small-required-core.md) | Small required core; no bundled shared-service apps | Proposed |
| [0006](0006-contracts-are-typed-and-published-as-openapi.md) | Contracts are typed and published as OpenAPI | Accepted |
| [0007](0007-in-process-transport-serializes.md) | The in-process transport serializes, like the network ones | Accepted |
| [0008](0008-typed-contract-errors-in-both-modes.md) | Contract errors are typed and identical in both modes | Accepted |
| [0009](0009-one-database-alias-per-app.md) | One database alias per app | Accepted |
| [0010](0010-pluggable-principal-resolution.md) | Pluggable principal resolution in core | Accepted |
| [0011](0011-the-host-serves-contract-endpoints-itself.md) | The host serves contract endpoints itself, from its root middleware, with no web framework | Accepted |
| [0012](0012-supported-python-and-django-versions.md) | Supported Python and Django versions | Accepted |
| [0013](0013-license.md) | License: BSD-3-Clause | Accepted |
| [0014](0014-pluggable-connector-transports.md) | Connector transports are pluggable, and a host names each remote provider by contract in `NEOLITH["REMOTE_PROVIDERS"]` | Accepted |
| [0015](0015-specification-first-django-reference-implementation.md) | Specification first; django-neolith is the reference implementation | Accepted |
| [0016](0016-an-app-may-run-attached-and-detached-at-once.md) | An app may run attached and detached at the same time | Superseded by [0031](0031-a-contract-version-is-routed-by-single-unless-local-first-is-named.md) |
| [0017](0017-call-context-explicit-at-the-action-ambient-for-propagation.md) | The call context is explicit at the action and ambient for propagation | Accepted |
| [0018](0018-requirement-ids-and-spec-to-test-traceability.md) | Requirement IDs and spec-to-test traceability | Accepted |
| [0019](0019-wire-encodings-of-the-portable-forms.md) | Wire encodings of the portable forms | Accepted |
| [0020](0020-wire-forms-pydantic-writes-otherwise-are-neolith-annotated-types.md) | A portable form whose wire spelling differs from Pydantic's default is a neolith Annotated type | Accepted |
| [0021](0021-an-app-declares-itself-on-its-neolithappconfig.md) | An app declares itself in class attributes of its `NeolithAppConfig`, with contracts named by their declaration classes | Accepted |
| [0022](0022-the-app-host-builds-settings-and-checks-before-it-serves.md) | The app host builds settings from declarations, checks at app loading, and boots when a handler is built | Accepted |
| [0023](0023-a-request-principal-comes-from-ordered-backends.md) | A request's principal comes from ordered backends that declare their request inputs, and a Django user is named by a deployer-given issuer and its primary key | Accepted |
| [0024](0024-user-documentation-is-built-with-sphinx-from-this-repository.md) | User documentation is built with Sphinx from this repository | Proposed |
| [0025](0025-drain-counts-a-request-from-its-request-line.md) | Drain counts a request from its request line | Accepted |
| [0026](0026-set-up-keeps-an-unknown-recorded-identity.md) | Set-up keeps a recorded identity it does not know, so the next set-up reports it again | Accepted |
| [0027](0027-a-caller-backend-names-the-calling-app-over-http.md) | On an HTTP call, a caller backend names the calling app from a shared-secret token, and the principal header is read only after the allow-list admits that app | Accepted |
| [0028](0028-the-http-transport-uses-the-standard-library-only.md) | The HTTP transport calls a provider over the standard library's sockets and TLS, with a strict HTTP/1.1 reader of its own, under one deadline for every blocking step, and adds no runtime dependency | Accepted |
| [0029](0029-a-record-belongs-to-the-app-whose-loading-asked-for-it.md) | A record belongs to the app whose loading step asked for it, and only a set-up that passes drops it | Accepted |
| [0030](0030-the-boundary-check-uses-the-standard-library-only.md) | `neolith check` derives the boundary from the declarations and Django's app registry, reads source with Python's own parser, and fails per app unless the repository marks the app report-only | Accepted |
| [0031](0031-a-contract-version-is-routed-by-single-unless-local-first-is-named.md) | A contract version is routed by `single` unless `NEOLITH["ROUTING_POLICIES"]` names `local-first`, which tries its listed providers in order and moves an idempotent call on `unavailable` | Accepted |
| [0032](0032-the-conformance-suite-runs-each-case-once-per-transport.md) | The conformance suite runs each contract case once per transport and compares the outcomes, lists its cases by name, and reports a run per requirement ID against the specification's own exemptions | Accepted |
| [0033](0033-eight-points-of-the-http-transport-settled-for-0-1.md) | Eight points the HTTP transport's two halves showed, settled for 0.1: network-only refusals, framing, a token's lifetime, the reply's size and what a refusal may print | Accepted |
| [0034](0034-two-aliases-on-one-database-are-refused-without-their-connection-settings.md) | Two aliases on one database are refused by name and rule, without their connection settings | Accepted |
