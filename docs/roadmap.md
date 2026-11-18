# Roadmap to 0.1

0.1 is a walking skeleton: one app boots alone, two apps call each other through a contract over the in-process and the HTTP transport, and one test suite passes over both. Every step is a pull request that leaves the open release branch working. Each step names the specification documents it implements, so the spec is written alongside the code ([ADR 0015](adr/0015-specification-first-django-reference-implementation.md)).

| Step | Delivers | Spec |
|---|---|---|
| 1 | Package layout, `pyproject.toml`, lint, type checks, test harness. CI matrix per [ADR 0012](adr/0012-supported-python-and-django-versions.md): Django 5.2 on Python 3.12 and the newest supported pair, on x86-64 and arm64, with deprecation warnings as errors | — |
| 2 | Contracts: `@contract`, `@action`, portable types, typed errors, `@implements`, validation at import time | 01 |
| 3 | In-process transport with serialization round-trip; connector with `single` routing | 02 |
| 4 | App declaration and host: `NeolithAppConfig`, settings built from declarations, `neolith run <app>` boots one app on SQLite | 05 |
| 5 | HTTP transport: provider endpoints via django-ninja, OpenAPI at the well-known path, connector over httpx, shared-secret tokens for the calling app, and the principal header | 03 |
| 6 | Conformance suite running every contract test over both transports; `local-first` routing and a failover test with two providers | 06, 02 |
| 7 | Boundary check: `neolith check` wrapping import-linter, report-only then enforce per app | — |
| 8 | Two small example apps under `examples/` showing attached, detached and both-at-once; the 0.1 proof | all |

Libraries named in the steps (django-ninja, httpx, import-linter) are working assumptions. Each needs an Accepted decision record before the step that adds it lands ([K5](../CONTRIBUTING.md)). For django-ninja that record is [ADR 0011](adr/0011-django-ninja-for-contract-endpoints.md), still Proposed.

Gate for releasing 0.1: verified on Python 3.15 with Django 6.1 ([ADR 0012](adr/0012-supported-python-and-django-versions.md)).

Deferred past 0.1: events and the outbox (spec 04), `neolith detach` data moves, breaking-change detection, routing policies beyond `single` and `local-first`, gRPC transport.

## The public surface of 0.1

Everything an app author touches. Designed before the internals; the internals serve this.

```python
from typing import Protocol

from neolith import NeolithAppConfig, contract, action, implements, connect, ContractError

@contract("inventory.v1")
class Inventory(Protocol):
    @action(idempotent=True)
    def reserve(self, request: ReserveRequest) -> ReserveReply: ...

    @action
    def release(self, request: ReleaseRequest) -> None: ...

@implements(Inventory)
class InventoryImpl:
    def reserve(self, request: ReserveRequest, ctx: CallContext) -> ReserveReply: ...
    def release(self, request: ReleaseRequest, ctx: CallContext) -> None: ...

class InventoryConfig(NeolithAppConfig):
    name = "inventory"
    provides = ["inventory.v1"]
    requires = []
    resources = {"database": "inventory"}

# in a caller
inventory = connect("inventory.v1")
try:
    reply = inventory.reserve(ReserveRequest(sku="A1", qty=2))
except ContractError as e:
    e.kind  # "unavailable", "conflict", ...
```

Rules that shape it:

- Request and reply types are Pydantic models restricted to the portable type system. Anything else fails at import time with a message naming the field.
- A contract is declared as a `Protocol` class; it is never instantiated. `connect()` returns a connector whose methods mirror the actions.
- Why `Protocol` and `@implements(Inventory)` rather than a plain class and `@Inventory.implement`: mypy accepts the `...` bodies of the actions only inside a Protocol, and it does not apply a class decorator's return type ([python/mypy#3135](https://github.com/python/mypy/issues/3135)), so an `implement` attribute added by `@contract` would not type-check.
- An implementation class is checked against its contract when the module is imported: every action present, signatures compatible. Missing or extra methods are errors, not warnings.
- An implementation receives the call context as an optional second parameter annotated `CallContext`, as the sketch shows. Outgoing calls find it on their own ([ADR 0017](adr/0017-call-context-explicit-at-the-action-ambient-for-propagation.md)).
- Configuration of providers, transports and routing lives in Django settings under one key, `NEOLITH`, and is validated by the host at startup.

Shell:

```
neolith run <app> [<app> ...]    boot the named apps in one host
neolith check [--report]         boundary check
neolith openapi <contract>       print the projection
```
