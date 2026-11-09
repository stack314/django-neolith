# 05. App declaration and lifecycle

**Status:** outline.

## Declaration

Every app declares, in a form the host can read without importing the app's implementation modules:

| Field | Meaning |
|---|---|
| `name` | The app's identifier |
| `provides` | Contract versions it implements |
| `requires` | Contract versions it calls |
| `publishes` | Event versions it emits (0.2) |
| `subscribes` | Event versions it consumes (0.2) |
| `resources` | Named resources it needs: database, cache, queue, object store |
| `batteries` | Framework features it relies on, so the host can enable them (framework-specific) |

## Lifecycle

A host takes a set of apps through these phases in order. A failure in any phase before *serve* MUST stop the host with a diagnostic; nothing is deferred to the first request.

1. **Declare.** Read every app's declaration.
2. **Resolve.** For every `requires`, find a provider: an app in this host that `provides` it, or a configured remote provider. An unsatisfied requirement is an error here, not at call time.
3. **Configure.** Build the host's configuration from the declarations plus environment: resources, transports, routing policies.
4. **Validate.** Check every provided contract against its implementation: every action implemented, types match, declared errors exist.
5. **Boot.** Initialise apps. Check that each app's database schema is current, and stop if it is not.
6. **Serve.** Accept calls and, where configured, HTTP requests.
7. **Drain.** On shutdown, stop accepting calls, finish in-flight calls up to their deadlines, then stop.

Schema changes are not part of the lifecycle. They are an explicit step, run once per database before hosts that serve the app start. Several hosts can serve the same app at once ([ADR 0016](../docs/adr/0016-an-app-may-run-attached-and-detached-at-once.md)), and they must never race on its schema.

A host running one app is a detached deployment. A host running several is a shared process. The host is the same program in both cases.

## Operational settings

Settings that do not change topology, such as log level, MAY be changed at runtime by the host. Mode and routing are deployment configuration ([00](00-overview.md)). Runtime mode switching is not specified; see [open questions](../docs/open-questions.md).
