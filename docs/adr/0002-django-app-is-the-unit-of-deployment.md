# 0002. The Django app is the unit of deployment

- **Status:** Accepted
- **Date:** 2026-11-03

## Context

Django's app is a great unit of modularity, and its unit of deployment is the project. That works well until a codebase outgrows a single deployment. At that point, splitting usually means creating several full projects (duplicated settings, auth and admin, plus hand-written HTTP between them) or moving some services off Django. Both carry real costs. See [VISION.md](../../VISION.md).

## Options

1. **Project as the unit** (status quo). Each service is a full Django project.
2. **App as the unit.** Each app declares what it needs, and a neolith *app host* boots it alone or together with other apps.
3. **Module as the unit**, below or beside Django apps (as in modulith). This loses Django's batteries.

## Decision

The Django app is the unit. Each app declares its needs on a `NeolithAppConfig` (contracts provided and required, contrib apps, resources). The **app host** builds settings from those declarations and boots one app or many. Each app runs in a mode, chosen per app in configuration:

- **Attached:** in a shared process, inside a host running several apps. A conventional Django project is just such a host.
- **Detached:** booted alone by `neolith run <app>`, reached over the network (HTTP by default; see [0014](0014-pluggable-connector-transports.md)).

The same app may also run in both modes at once ([0016](0016-an-app-may-run-attached-and-detached-at-once.md)).

Follows principles 1 (*the app over the project*) and 3 (*configuration over code changes*).

## Consequences

- `neolith run <app>` is the central capability. If it doesn't work, nothing else matters.
- Anything Django treats as project-global (settings, users, admin, signals, the migration graph) needs an app-scoped answer. Most of the hard work is there.
- A project can run any mix of attached and detached apps, from the first commit or after adopting neolith later. Neolith prescribes no default.
