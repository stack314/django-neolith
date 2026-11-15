<div align="center">

<h1>
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/neolith-wordmark-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="docs/assets/neolith-wordmark-light.svg">
  <img alt="django-neolith" src="https://raw.githubusercontent.com/stack314/django-neolith/main/docs/assets/neolith-wordmark-light.svg" width="360">
</picture>
</h1>

<p><strong>Deploy Django apps, not Django projects.</strong></p>

<p>
<a href="https://github.com/stack314/django-neolith/actions/workflows/ci.yml?query=branch%3Amain"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/stack314/django-neolith/ci.yml?branch=main&label=CI"></a>
<a href="https://pypi.org/project/django-neolith/"><img alt="PyPI version" src="https://img.shields.io/pypi/v/django-neolith"></a>
<a href="https://pypi.org/project/django-neolith/"><img alt="Python versions" src="https://img.shields.io/pypi/pyversions/django-neolith"></a>
<a href="https://pypi.org/project/django-neolith/"><img alt="Django versions" src="https://img.shields.io/pypi/frameworkversions/django/django-neolith"></a>
<a href="LICENSE"><img alt="License: BSD-3-Clause" src="https://img.shields.io/badge/license-BSD--3--Clause-blue"></a>
<a href="https://github.com/astral-sh/ruff"><img alt="Code style: Ruff" src="https://img.shields.io/badge/code%20style-ruff-261230"></a>
<a href="https://mypy-lang.org/"><img alt="Checked with mypy" src="https://img.shields.io/badge/mypy-checked-2a6db2"></a>
</p>

</div>

**Status: alpha (0.1.0a1). The API may change before 0.1.**

```
pip install --pre django-neolith
```

## What it is

Neolith makes the Django app, not the Django project, the unit of deployment. An app can run inside a shared process or on its own as a service, with its models, migrations, admin and tests intact, and without changing its code. Apps talk to each other only through typed contracts, so the same code and the same tests hold in both arrangements. Neolith is defined by a [specification](spec/README.md) that is independent of Django and of Python, and django-neolith is its reference implementation.

## Features

What works on `main` today. The [changelog](CHANGELOG.md) has the details.

- **Typed contracts.** A contract is a `Protocol` class with `@contract` and `@action`, and an implementation is marked with `@implements`. Request and reply types are Pydantic models in a portable type system, and every check runs when the module is imported.
- **Typed errors.** `ContractError` carries a `kind`, a message and a detail. An action can declare its own error kinds with their detail records.
- **Calls cross as JSON, in-process too.** Every request, reply and error detail is written and read through its declared type, so an in-process call carries exactly what a remote one will.
- **App declaration.** An app declares what it provides, requires and owns on its `NeolithAppConfig`, and the declaration is checked when the class is defined.
- **Checks before serving.** At start-up the host resolves every required contract to a provider, validates each implementation and runs its system checks, and reports every problem at once.
- **One database per app.** `neolith.routers.AppRouter` keeps each app's data on a database alias of its own.
- **Call context.** A principal, a trace, a deadline and the calling app travel with each call. A root is opened for each request by `RootMiddleware`, and for a command or background work by `command_scope`.

Not yet on `main`: `neolith run <app>`, the HTTP transport, the conformance suite and events. See the roadmap below.

## A short example

A contract, in a contract package other apps may import:

```python
# inventory/contracts.py
from typing import Protocol

from pydantic import BaseModel

from neolith import action, contract


class ReserveRequest(BaseModel):
    sku: str
    qty: int


class ReserveReply(BaseModel):
    reserved: int


@contract("inventory.v1")
class Inventory(Protocol):
    @action(idempotent=True)
    def reserve(self, request: ReserveRequest) -> ReserveReply: ...
```

Its implementation, private to the app that provides it:

```python
# inventory/services.py
from neolith import CallContext, implements

from inventory.contracts import Inventory, ReserveReply, ReserveRequest


@implements(Inventory)
class InventoryService:
    def reserve(self, request: ReserveRequest, ctx: CallContext) -> ReserveReply:
        return ReserveReply(reserved=request.qty)
```

The two apps' declarations:

```python
# inventory/apps.py
from neolith import NeolithAppConfig, Provided

from inventory.contracts import Inventory


class InventoryConfig(NeolithAppConfig):
    name = "inventory"
    label = "inventory"
    provides = (
        Provided(
            Inventory,
            implementation="inventory.services.InventoryService",
            principal_from=("shop",),
        ),
    )


# shop/apps.py
from neolith import NeolithAppConfig

from inventory.contracts import Inventory


class ShopConfig(NeolithAppConfig):
    name = "shop"
    label = "shop"
    requires = (Inventory,)
```

And a call from the shop app, which knows the contract and nothing else of inventory:

```python
# shop/orders.py
from neolith import ContractError, connect

from inventory.contracts import ReserveRequest

inventory = connect("inventory.v1")


def place_order(sku: str, qty: int) -> int:
    try:
        reply = inventory.reserve(ReserveRequest(sku=sku, qty=qty))
    except ContractError as error:
        print(error.kind)  # "unavailable", "conflict", ...
        raise
    return reply.reserved
```

With `"inventory"`, `"shop"` and `"neolith"` in `INSTALLED_APPS`, a view in the shop app can call `place_order` directly; a command opens its root first, with `neolith.command_scope("shop")`.

## Roadmap

The steps to 0.1 are in the [roadmap](docs/roadmap.md).

## Links

- [Vision](VISION.md)
- [Specification](spec/README.md) (draft 0.1)
- [Roadmap to 0.1](docs/roadmap.md)
- [Guiding principles](docs/principles.md)
- [Architecture decision records](docs/adr/README.md)
- [Open questions](docs/open-questions.md)
- [Contributing](CONTRIBUTING.md)
- [Code of conduct](CODE_OF_CONDUCT.md)

Licensed under the [BSD 3-Clause License](LICENSE).
