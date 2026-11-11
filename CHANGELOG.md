# Changelog

All notable changes to this project are recorded in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0, a minor release may contain breaking changes; each one is listed under *Changed* or *Removed* and never made silently.

## [Unreleased]

### Added

- Contracts (spec 01): `@contract` on a `Protocol` class, `@action` with `idempotent=`, `@implements`, and `Contract.of(...)` with a neutral description of the actions and their JSON Schemas. Identities, action names, request and reply types and implementations are checked when the module is imported, and fail with `ContractDefinitionError`.
- Contract errors (spec 01, [ADR 0008](docs/adr/0008-typed-contract-errors-in-both-modes.md)): `ContractError` with a `kind`, a `message` and a detail in `data`, and `@action(errors={...})` to declare an action's own error kinds with their detail records. Declared kinds are checked when the contract is declared, may not use a standard or reserved kind's name, and appear in the neutral description. A kind the receiver does not know is held with an untyped, read-only detail.
- `Action` gains `errors`, a frozen mapping from each declared kind to its detail record, empty by default.
- `@action` marks a method once, and implementation methods are not marked, including through wrappers such as `functools.cache` or `property`. `@implements` reads the implementation class statically, so it never evaluates a lazy attribute. A default of an integer field is refused outside the 64-bit range.
- Runtime dependency on Pydantic v2 ([ADR 0006](docs/adr/0006-contracts-are-typed-and-published-as-openapi.md)).
