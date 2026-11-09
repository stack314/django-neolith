# Guiding principles

These are the tenets that settle disagreements. Each one is written as **"X over Y"**. Y is not bad, but when the two conflict, we choose X. If a proposal breaks a tenet, it needs a decision record that says why.

They are modelled on Django's own [design philosophies](https://docs.djangoproject.com/en/stable/misc/design-philosophies/).

## Architecture

1. **The app over the project.**
   Everything neolith adds (contracts, events, settings, deployment) is declared per Django app. The project is just a host that runs one or more apps.

2. **Contracts over imports.**
   An app may import Django, neolith and another app's *contract package* (its interface and types), but never another app's internals: models, services or signals. The contract is the only shared surface, and CI enforces it. (In hexagonal terms the contract is the port and the transport is the adapter; the docs use *contract*, *connector* and *transport* throughout.)

3. **Configuration over code changes.**
   Where an app runs (attached or detached) is configuration, applied at deploy time. If changing modes requires editing the app or its callers, it is a bug.

4. **Owned data over shared data.**
   Every app owns its tables, in every mode. There are no cross-app foreign keys, joins or transactions. Apps store other apps' IDs and ask through contracts.

5. **Same semantics over same mechanics.**
   The code and the meaning of a call are identical in every mode. Only the transport differs: a native call when attached, a network call when detached. Anything an app shares with another must therefore be a contract or an event, even in-process, so that switching modes changes the transport and nothing else.

## Honesty

6. **Visible failure over transparent calls.**
   A contract call might cross a network, so its errors, timeouts and retries are part of its type. We do not hide the network to make code look simpler.

7. **Running over linting.**
   An app counts as independent only when it boots alone and passes its tests. A quiet linter is not enough.

8. **One test suite over many code paths.**
   Every transport is tested by the same contract tests. Drift between modes is a failing build, not a production surprise.

## Adoption

9. **New and existing projects alike.**
   Every feature must work for a system built as services from the first commit and for a running project that adopts neolith one app at a time. Neither case is privileged, and neither requires a rewrite.

10. **Django-native over framework-new.**
    Use Django's own mechanisms (AppConfig, routers, migrations, admin, management commands) before inventing ours. A neolith app should still read as a Django app.

11. **Specification over implementation.**
    Neolith is defined by a specification that is independent of Django and Python. django-neolith is its reference implementation. Contracts publish in neutral formats such as OpenAPI, so services on other stacks can implement the same specification and interoperate.

## Scope

12. **Small core over broad features.**
    The required core is contracts, connectors and transports, the app host, events and checks. Shared capabilities such as identity or notifications are ordinary apps behind contracts, not part of neolith. We would rather ship a working detach story early than a wide one late.

13. **Integrate over own.**
    Gateways, brokers, OIDC providers, tracing: use what exists and document the seam. Neolith builds only what nobody else does for Django.

14. **Recorded over remembered.**
    Decisions that shape the architecture get a decision record in [`docs/adr/`](adr/README.md). Undocumented decisions get re-argued.
