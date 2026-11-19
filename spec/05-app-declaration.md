# 05. App declaration and lifecycle

**Status:** outline, except the form of a declaration, Resolve and Validate, the checks before Serve, Configure, Boot, Serve and Drain, the schema step, an app's database and a request's principal, which are specified.

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
4. **Validate.** Check every provided contract against its implementation: every action implemented, with the declared request and reply types. Declared errors are checked when the contract is loaded ([01](01-contracts.md)).
5. **Boot.** Initialise apps. Check that each app's database schema is current, and stop if it is not.
6. **Serve.** Accept calls and, where configured, HTTP requests.
7. **Drain.** On shutdown, stop accepting calls, finish in-flight calls up to their deadlines, then stop.

Schema changes are not part of the lifecycle. They are an explicit step, run once per database before hosts that serve the app start. Several hosts can serve the same app at once ([ADR 0016](../docs/adr/0016-an-app-may-run-attached-and-detached-at-once.md)), and they must never race on its schema.

A host running one app is a detached deployment. A host running several is a shared process. The host is the same program in both cases.

### Before Serve

**APP-16.** A host MUST stop before it serves when an input of its configuration is outside the set it admits, with a diagnostic that names the input.

**APP-17.** When it loads its apps, a host MUST warn about each registration of the framework's generic data views, such as an administration site, whose code is in no app, since a call made from those views has no calling app and is refused ([02](02-transports.md), TRN-58).

**APP-26.** When it boots, a host MUST warn about each such registration it has not warned about yet, such as one made while it loaded the code that maps requests to views.

Other code in no app that serves requests is found by the check before deployment, which a later step adds ([ADR 0022](../docs/adr/0022-the-app-host-builds-settings-and-checks-before-it-serves.md) Decisions 5 and 7).

### Configure

The host builds its configuration from two inputs: the declarations of the apps it hosts, and what the deployer supplies. A part of the configuration that the declarations determine is the host's to build, never the deployer's to write.

**APP-27.** A host MUST derive from the declarations which apps it installs, and which database each app's records live in.

**APP-28.** A host MUST stop before it serves when the deployer's input states a part of the configuration that the host derives.

**APP-29.** A host MUST stop before it serves when the configuration it would serve with differs, in a part it derives, from what it derived.

**APP-30.** A deployer's instruction to silence a diagnostic MUST NOT silence a refusal that stops the host.

The deployer's configuration names the remote providers of each contract version that the host calls over a network, and may name a routing policy for it ([02](02-transports.md), *Providers and routing*).

**APP-63.** A host MUST route a contract version by `single` unless its configuration names another routing policy for it.

**APP-65.** A host MUST stop before it serves when its configuration names a routing policy that this specification does not define.

### Boot

Boot reads the state of each database the host was configured with, and of each app's schema in its own database. It reads only; schema changes are the separate step of the lifecycle above.

**APP-31.** A host MUST stop before it serves when one of its SQLite databases holds no schema, such as a file that nothing has created yet.

**APP-32.** A host MUST stop before it serves when one of its databases is held in memory, since it is empty for a new connection or gone once its last connection closes.

**APP-33.** A host MUST stop before it serves when a schema change of one of its apps has not been applied to the app's own database.

**APP-34.** A host MUST stop before it serves when an app that declares a database resource has no schema changes, when its applied changes are inconsistent with the changes it ships, or when two of its changes conflict.

**APP-35.** A host MUST stop before it serves when a schema change of an app depends on a schema change of another app.

**APP-36.** A host MUST NOT write to a database while it checks the database and its schemas, beyond what the deployer's own connection settings run on each new connection.

**APP-37.** A database that is ahead of an app's schema changes, holding changes the app does not ship, MAY be admitted, so that a host of the previous release can start during a rolling deployment.

### Serve and Drain

A host serves once every phase before Serve has ended. Drain begins when the host is asked to shut down, and ends when it stops.

**APP-38.** A host MUST NOT begin a phase before every phase above it in the lifecycle has ended without a refusal.

**APP-39.** When Drain begins, a host MUST stop accepting connections.

**APP-40.** A host MUST NOT serve a request that begins after Drain has begun, whether it arrives on a new connection or on one that was open before.

**APP-41.** A host MUST let each request that is in flight when Drain begins run until it ends, or until the host's request deadline ([02](02-transports.md), TRN-59) has passed since Drain began, whichever comes first.

**APP-42.** When the host has no request deadline, Drain MUST still end, after a bound the host documents.

**APP-43.** A host MUST stop once no request is in flight, or once the bound has passed.

**APP-44.** A host that stops while a request is still in flight MUST name each such request in its diagnostic, and end with an outcome that differs from a stop with none in flight.

**APP-45.** A host that is asked to shut down a second time MAY stop at once.

Every request in flight began before Drain did, so a call made while serving it, under the request root's deadline, ends before the bound. Work under a root that app code opens itself, with a deadline of its own or none, is not bounded by the request it runs in, and Drain can cut it. A call that arrives over a transport carries its caller's deadline, or none; what Drain gives it is open ([ADR 0022](../docs/adr/0022-the-app-host-builds-settings-and-checks-before-it-serves.md), open question 13).

### The schema step

Schema changes are applied by a step of their own, which the deployer runs before hosts that serve the apps start.

**APP-46.** A host MUST NOT apply schema changes while it starts, serves or drains.

**APP-47.** The schema step MUST refuse, before it changes any database, an app that declares a database resource and has no schema changes.

**APP-48.** The schema step MUST refuse, before it changes any database, an app with a schema change that depends on a schema change of an app whose records live in another database.

**APP-49.** When applying one app's schema changes fails, the schema step MUST stop before the next app, with a diagnostic that names the apps whose changes it applied and those whose changes it did not.

**APP-50.** The schema step MUST offer a check that reports what Boot would refuse in each database and schema, and what the schema step would refuse before it changes a database, and writes to no database beyond what the deployer's own connection settings run on each new connection.

### An app's database

An app that declares a database resource owns its data in every host ([ADR 0004](../docs/adr/0004-each-app-owns-its-data.md)). Apps attached in one host therefore meet the same database behaviour they meet when each runs alone: no transaction, relation or schema joins two of them ([ADR 0009](../docs/adr/0009-one-database-alias-per-app.md)). Below, an app's *own database* is the database its host gives it for that resource.

**APP-18.** A host MUST give each app that declares a database resource a database of its own, apart from every other app's.

**APP-19.** A read or write of an app's records that names no database MUST go to the app's own database.

**APP-20.** A transaction opened on one app's own database MUST NOT cover a write to another database.

**APP-21.** A database operation that is not about an app's records and names no database MUST NOT reach any app's own database.

**APP-22.** A host MUST refuse to set a relation from one record to another record that lives in a different database.

A relation declared in an app's data model to another app's records is found by the check before deployment ([ADR 0004](../docs/adr/0004-each-app-owns-its-data.md)), not when the host sets a relation: a record can also hold it by the other record's identifier alone, and an update of many records at once sets it without the host being asked.

**APP-23.** A host MUST create an app's schema in the app's own database only, and nothing else there.

An app's table names are the same in every host, so that the app finds its tables when it moves.

**APP-24.** A host MUST stop before it serves when an app that declares no database resource defines stored records.

**APP-25.** A host MUST stop before it serves when the name of one of an app's tables, derived by the framework rather than stated by the app, was shortened to fit a database other than the app's own.

### Resolve and Validate

**APP-8.** A host MUST find a provider for every contract version that one of its apps requires, before it serves.

**APP-62.** A host MUST NOT refuse to serve because a remote provider cannot be reached. Resolve finds a remote provider in the host's configuration, and the host does not contact it before it serves, so a provider that is down is found by the first call to it, which fails at the caller with `unavailable`, or with `timeout` when its deadline passes first ([02](02-transports.md), TRN-3 and TRN-4).

**APP-64.** A host MUST stop before it serves when its configuration lists more than one remote provider for a contract version it routes by `single`, or names `local-first` for one with fewer than two remote providers.

**APP-9.** A host MUST NOT hold two apps that provide one contract version.

**APP-10.** All apps of a host that name one contract version, as provided or as required, MUST name the same contract declaration of it.

**APP-11.** A host MUST check every contract version its apps provide against its implementation before it serves: every action implemented, taking the declared request and returning the declared reply.

**APP-12.** When Resolve and Validate find several refusals, the host MUST give all of them in one diagnostic, each naming its app or its call site.

### Connectors before Serve

Calling code asks the host for a connector by contract identity ([ADR 0003](../docs/adr/0003-apps-communicate-through-contracts.md)), and may do so in any phase. A host *knows* a contract version when one of its apps provides or requires it.

**APP-13.** A call made through a connector before Validate has ended without a refusal MUST fail as a mistake in the calling code ([01](01-contracts.md), CON-29), and is not sent.

**APP-14.** A connector asked for before Validate has ended, for a contract version the host does not know, MUST stop the host before it serves.

**APP-15.** A connector asked for after Validate has ended, for a contract version the host does not know, MUST be refused as a mistake in the calling code when it is asked for.

Decision records: [0003](../docs/adr/0003-apps-communicate-through-contracts.md), [0014](../docs/adr/0014-pluggable-connector-transports.md), [0021](../docs/adr/0021-an-app-declares-itself-on-its-neolithappconfig.md), [0022](../docs/adr/0022-the-app-host-builds-settings-and-checks-before-it-serves.md), [0031](../docs/adr/0031-a-contract-version-is-routed-by-single-unless-local-first-is-named.md).

### A request's principal

A host that serves ordinary requests, such as web pages, opens a request root for each one ([02](02-transports.md), Request roots). The principal of the calls made from that root is the principal of the request: the party on whose behalf the request acts, such as a logged-in user. The host finds it through *principal backends*, which the deployer configures in an order ([ADR 0010](../docs/adr/0010-pluggable-principal-resolution.md) Decision 6, [ADR 0023](../docs/adr/0023-a-request-principal-comes-from-ordered-backends.md)). A backend, given a request, gives a principal, abstains, or fails. Each backend declares the request inputs it reads: request header names, or that it reads something outside the URL and the headers. The host trusts the declaration and does not check it.

Resolving the principal means asking the backends, which is the *search*. A call made from the root before the host has found the code that serves the request, or from a root whose serving code is in no app the host holds, has no principal and starts no search, at any time: it has no calling app and is refused (TRN-56, TRN-58), or it belongs to an app the host does not hold, which requires nothing here, and is not sent (TRN-39). The requirements below are about the other request roots, those that belong to an app the host holds.

**APP-51.** A host MUST resolve a request root's principal by asking its configured backends one at a time, in the configured order.

**APP-52.** When a backend gives a principal, that principal MUST be the request root's principal, and no later backend is asked.

**APP-53.** When every backend abstains, or the host has no backend, the request root MUST have no principal.

**APP-54.** When a backend fails, gives something other than a principal or an abstention, or makes a call from the request root whose principal it is resolving, the host MUST record a failure to resolve on the request root, and no later backend is asked.

**APP-55.** A call from a request root on which a failure to resolve is recorded MUST fail with `internal` at the caller's first step ([ADR 0017](../docs/adr/0017-call-context-explicit-at-the-action-ambient-for-propagation.md) Decision 9), and is not sent.

**APP-56.** The first search of a request root to complete MUST fix its outcome, a principal, none or a failure, for every call from the root. A search that completes later does not change it.

Two first calls made at once may each run a search, so a backend may be asked more than once for one request, and it gives the same answer each time.

**APP-57.** A host MUST NOT start a search for a request root before the root's first call, except as APP-58 requires.

**APP-58.** When the body of a response is produced inside its request root after the code that handles the request has returned, and no search has fixed the root's outcome, the host MUST search before it returns the response. A failure found then is only recorded: the response is unchanged.

**APP-59.** In a host with a backend, a call made from a request root after the host has finished the headers of its response, while no search has fixed the root's outcome, MUST fail as APP-55 says, and starts no search. That failure is the root's outcome: it comes before APP-56, so a search still running then does not change it.

**APP-60.** When a search has started for a request root, the host MUST add every request header name that its configured backends declare to the `Vary` header of the response, before the response leaves the host, or make the header `*` when a backend declares that it reads something outside the URL and the headers.

**APP-61.** A host MUST NOT add to the `Vary` header of a response when no search has started for its request root.

The host adds these names whatever the response holds, so a cache that keys on `Vary` keeps the responses of two principals apart. A cache placed inside the host, between the code that handles the request and the point where the host adds the names, stores a response before they are added; the host cannot reach it.

Decision records: [0010](../docs/adr/0010-pluggable-principal-resolution.md), [0017](../docs/adr/0017-call-context-explicit-at-the-action-ambient-for-propagation.md), [0023](../docs/adr/0023-a-request-principal-comes-from-ordered-backends.md).

## Operational settings

**APP-2.** Settings that do not change topology, such as log level, MAY be changed at runtime by the host. Mode and routing are deployment configuration ([00](00-overview.md)). Runtime mode switching is not specified; see [open questions](../docs/open-questions.md).
