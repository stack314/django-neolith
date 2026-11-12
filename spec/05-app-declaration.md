# 05. App declaration and lifecycle

**Status:** outline, except the form of a declaration, which is specified. Resolve, Validate and the rest of the lifecycle become requirements with the host (#11).

## Declaration

Every app declares, in a form the host can read without importing the app's implementation modules:

| Field | Meaning |
|---|---|
| `name` | The app's name |
| `provides` | Contract versions it implements |
| `requires` | Contract versions it calls |
| `publishes` | Event versions it emits (0.2) |
| `subscribes` | Event versions it consumes (0.2) |
| `resources` | Resources it needs: database, cache, queue, object store |
| `batteries` | Framework features it relies on, so the host can enable them (framework-specific) |

How a declaration is written is the implementation's choice. What each field holds is fixed here.

### Name

**APP-3.** An app's name MUST match the rule `segment` of the grammar in [01](01-contracts.md#identity-and-versioning), for example `inventory` or `order_desk`.

The name is how other apps and other hosts know the app: it is the calling app of a call ([02](02-transports.md)), the issuer of a shared-secret token ([03](03-http-transport.md)) and an entry of an allow-list (below). It uses lowercase ASCII letters, digits and underscores for the reason contract identities do.

**APP-4.** App names MUST be unique among the apps of all hosts that exchange calls. A host sees only the apps it holds, so no host can check this: it is a duty of deployment.

### Contracts

`provides` and `requires` each hold a set of contract versions, each known by its identity ([01](01-contracts.md)). An app may require a contract version it also provides.

**APP-5.** For each contract version an app provides, its declaration MUST state the allow-list of that contract version: the names of the calling apps that may assert a principal on a call to it ([ADR 0017](../docs/adr/0017-call-context-explicit-at-the-action-ambient-for-propagation.md), Decision 6).

An allow-list may be empty, and then no calling app may assert a principal. Each entry is one app's name; no entry stands for several apps. The providing app is a calling app like any other: it may assert a principal on a call to its own contract only when its own name is on the list. A listed app may run in another host, so a host does not check that it knows a listed name.

### Resources and batteries

In spec 0.1 a resource is named by its kind, and an app declares at most one resource of a kind. Where the resource lives is deployment configuration ([00](00-overview.md)). A battery is named as the framework names it.

### What a host refuses

A declaration is *admitted* when all of these hold:

- its name meets APP-3;
- no contract version appears twice in `provides`, or twice in `requires`;
- every provided contract version has its allow-list, every entry of which is an app's name in the form of APP-3, none twice;
- every resource is of a kind the host can supply, none twice;
- no battery appears twice;
- `publishes` and `subscribes` are absent, since events are not part of spec 0.1 ([04](04-events.md));
- it has no field other than those of the table above, and every field has the form the implementation documents for it.

**APP-6.** A host MUST refuse a declaration that is not admitted, at Declare. The diagnostic names the app and the field.

**APP-7.** When one declaration is refused for several reasons, the host MUST give all of them in one diagnostic.

Decision records: [0002](../docs/adr/0002-django-app-is-the-unit-of-deployment.md), [0017](../docs/adr/0017-call-context-explicit-at-the-action-ambient-for-propagation.md), [0021](../docs/adr/0021-an-app-declares-itself-on-its-neolithappconfig.md).

## Lifecycle

A host takes a set of apps through these phases in order. **APP-1.** A failure in any phase before *serve* MUST stop the host with a diagnostic; nothing is deferred to the first request.

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

**APP-2.** Settings that do not change topology, such as log level, MAY be changed at runtime by the host. Mode and routing are deployment configuration ([00](00-overview.md)). Runtime mode switching is not specified; see [open questions](../docs/open-questions.md).
