# django-neolith: vision

> **Deploy Django apps, not Django projects.**

## Mission

Make the Django app, not the Django project, the unit of deployment. Any app can run inside a shared process or on its own as a service, reached through the same contract, with its models, migrations, admin, auth, management commands and tests intact. A service is not a new Django project: neolith's app host boots the app from its own declaration, so there is no second `settings.py`, `urls.py` or `manage.py` to maintain.

Neolith is defined by a specification that is independent of Django and of Python: how apps declare contracts, how calls travel between them, and how an app is booted on its own. django-neolith is the first and reference implementation of that specification. Other frameworks and languages can implement the same specification and interoperate.

## The problem

Django's app is a great unit of modularity. Its unit of deployment is the project, so a Django codebase is deployed as a monolith: every app ships, scales and fails together. When a team wants some apps to be services, with their own scale, release pace or isolation, it usually picks one of two paths, and both carry real costs:

- **Split into several Django projects.** Every service duplicates settings, auth wiring, admin and boilerplate. Every former import becomes a hand-written network call. The result is many small monoliths talking over the network.
- **Rewrite services in a lighter framework.** The team loses the ORM, migrations, admin, auth and the app ecosystem, which are the reasons it chose Django in the first place.

Other ecosystems have explored this, notably ABP in .NET and Encore in Go and TypeScript. In Django, projects such as django-modulith and django-microscope have laid useful groundwork, and they suggest where the hardest part lies: getting a single app to genuinely run on its own.

## Who it is for

Anyone building on Django who wants apps to be independently deployable, whether the codebase is new or already running. The need shows up in many shapes: one app that needs its own scale or release pace, data that must be isolated or deployed separately, platforms that share capabilities across many portals or tenants, or a new system designed as services from day one. Neolith does not assume a team size, a company shape or a starting point.

## Goals

1. **An app boots alone.** `neolith run <app>` starts one Django app with only what it declares, with no hand-built project around it. This is the product. Everything else supports it.
2. **Same code, any mode.** An app runs *attached* (in a shared process) or *detached* (its own service) by configuration, with no code change in the app or its callers. The mode is applied at deploy time; operational settings such as log level can change at runtime, and runtime mode switching is future work.
3. **Both at once.** The same app may run attached and detached at the same time, for redundancy or migration, with the connector routing between them.
4. **Apps talk through contracts.** A contract is an app's only public surface. Another app may import the contract package natively (its interface and types), never the app's internals. A connector carries each call over a transport: in-process, HTTP, or others.
5. **Batteries travel with the app.** A detached app keeps its models, migrations, admin, management commands and tests.
6. **New and existing projects alike.** Neolith works for a system designed as services from the first commit, and for a running project that adopts it one app at a time. Neither is the primary case.
7. **Proven, not claimed.** One contract test suite runs against every mode and transport, and independence is shown by booting the app alone in CI.

## Non-goals

- **Not a hosted service.** Neolith is a library. Nothing in it depends on a platform we operate.
- **Not infrastructure.** No gateway, service mesh, broker or orchestrator of our own. Neolith integrates with the usual ones.
- **Not transparent RPC.** Neolith does not pretend a remote call is a local one. Contracts make network failure visible.
- **Not a rewrite tool.** Adopting neolith in an existing project never requires a big-bang migration.
- **Not a prescribed topology.** Neolith takes no position on how many apps should be attached or detached. That is each project's choice, per app.
- **Not a set of shared-service apps.** Identity, notifications, workflow and the like are ordinary apps behind contracts. Neolith ships none of them and gives them no special status.

## Further reading

- [Guiding principles](docs/principles.md): the tenets that settle trade-offs.
- [Decision records](docs/adr/README.md): what we decided and why.
- [Open questions](docs/open-questions.md): what still needs research or a decision.
