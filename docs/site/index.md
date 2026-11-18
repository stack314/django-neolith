# Neolith

Deploy Django apps, not Django projects.

**Status: alpha, before 0.1. The API may change in any release.** These pages are built from the `next` branch, which may be ahead of the latest release.

Neolith lets a Django app run inside a shared process or as its own service without changing its code. Apps talk to each other only through typed contracts, so the same code and the same tests hold in both arrangements. Neolith is defined by a specification written independently of Django and Python, and django-neolith is its reference implementation (ADR 0015).

## What the `next` branch does

- **Contracts** (spec 01). A contract and its actions are declared on a `Protocol` class and checked when the module is imported, with typed errors and the portable forms of ADR 0019.
- **The call context** (ADR 0017). An implementation's action can read the principal, the trace, the deadline and the idempotency key of the call it serves.
- **App declaration and the host's set-up** (spec 05). An app declares the contracts it provides and requires on its `NeolithAppConfig`. When Django loads the apps, the host resolves each required contract to a provider and checks each implementation, and `connect()` returns a connector to a contract by its identity.

The [changelog](project/changelog.md) lists every change, and the [roadmap](project/roadmap.md) lists the steps to 0.1.

## Where to read next

- [Concepts](concepts/index.md): apps, contracts, connectors and modes.
- [API reference](reference/index.md): every name the `neolith` package exports.
- [Specification](spec/index.md): what any implementation does, with an ID for each requirement.
- [Decision records](adr/index.md): why the design is as it is.
- [Project](project/index.md): vision, principles, roadmap, open questions and how to contribute.

```{toctree}
:hidden:

concepts/index
reference/index
spec/index
adr/index
project/index
```
