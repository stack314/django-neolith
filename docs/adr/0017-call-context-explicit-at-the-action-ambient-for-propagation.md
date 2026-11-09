# 0017. The call context is explicit at the action and ambient for propagation

- **Status:** Proposed
- **Date:** 2026-11-09

## Context

Every call carries a **call context**, separate from the request. The draft specification (spec 00) gives it two kinds of field:

- **Propagated fields** pass from a call to the calls it makes: the principal on whose behalf the call is made, the trace identifier and the deadline.
- **Per-call fields** belong to one outbound call: the idempotency key. Each outbound call gets a fresh key, kept only across retries of that same call.

Over HTTP the context travels in headers (`Authorization`, `traceparent`, `Neolith-Timeout`, `Idempotency-Key`). The deadline crosses as a relative budget, and the receiving host computes its own local deadline from it (spec 03). Apps must not put it inside request types. The question, recorded as open question 6, is how it reaches the code that implements an action, and how it reaches the calls that code makes in turn.

Two directions have to work, and they pull different ways:

- **Inbound.** An implementation needs to read the context, mostly the principal ([0010](0010-pluggable-principal-resolution.md)), sometimes the deadline or which app is calling.
- **Outbound.** When an action calls another contract, the outgoing call must inherit the deadline (never extend it), continue the trace, carry the principal, and get its own idempotency key. If this depends on every author remembering to pass something along, it will sometimes be forgotten, and the failure is silent: a call runs past its caller's deadline, or the trace breaks.

Constraints from existing decisions and rules:

- **Parity** ([0007](0007-in-process-transport-serializes.md), principle 5). An implementation must see an equal context whether it was called in-process or over HTTP. The in-process transport serializes the request, so the context must also be a plain value, never a live object such as a user model instance ([0004](0004-each-app-owns-its-data.md)).
- **Typed public API** (CONTRIBUTING K1) and **fail at startup** (K2).
- **No hidden state** (K6). Process-wide state is owned by the app host and documented.
- **Python 3.12 floor, nothing deprecated** ([0012](0012-supported-python-and-django-versions.md)).
- **Concurrency.** Actions may be called from synchronous Django views, from async views, through Django's sync/async bridges (asgiref's `sync_to_async` and `async_to_sync`), from management commands, and from threads the app starts itself.

Both approaches already exist in Django. Views receive `request` explicitly. The active language, the current time zone and database connections are context-local state held in `asgiref.local.Local`. Each fits its job, so this ADR weighs them for this job rather than ruling either out.

## Options

The sketches use one implementation that reads the principal and calls a second contract.

### 1. Explicit parameter on every action

```python
@Inventory.implement
class InventoryImpl:
    def reserve(self, request: ReserveRequest, ctx: CallContext) -> ReserveReply:
        owner = ctx.principal.id
        quote = connect("pricing.v1").quote(QuoteRequest(sku=request.sku), context=ctx)
        ...
```

- **Typing:** the best of the four. The dependency is in the signature, and the type checker sees every use.
- **Testability:** a unit test passes a context built by a factory. No setup.
- **Parity:** straightforward. Each transport builds the value and passes it.
- **Propagation:** manual. Every outgoing call, and every helper between the action and that call, must take and pass `ctx`. Forgetting can be made an error by requiring `context=` on connector calls, but then every caller everywhere (views, commands, tasks) must build a context first.
- **Concurrency:** unaffected by threads or async, because nothing is implicit.
- **K6:** nothing hidden.
- **DX:** the most boilerplate. `ctx` spreads into domain code that has no other reason to know about calls.

### 2. Ambient context in a context variable owned by the app host

```python
from neolith import current_context

@Inventory.implement
class InventoryImpl:
    def reserve(self, request: ReserveRequest) -> ReserveReply:
        owner = current_context().principal.id   # raises outside a call
        quote = connect("pricing.v1").quote(QuoteRequest(sku=request.sku))  # inherits automatically
        ...
```

The transport sets a `contextvars.ContextVar` for the duration of the action and resets it with the returned token afterwards.

- **Typing:** the value is typed, but its presence is not. Reading it outside a call fails at run time, not at type-check time.
- **Testability:** tests must enter a scope (`with neolith.testing.call_scope(...)`). A forgotten scope gives a clear error, but it is one more thing to know.
- **Parity:** achievable. Each transport sets an equal value.
- **Propagation:** automatic, which is the main strength. Deadlines and traces cannot be dropped by omission. OpenTelemetry's Python context API is built the same way, on context variables.
- **Concurrency:** asyncio tasks copy the current context when they are created, and `asyncio.to_thread` copies it too. asgiref's `sync_to_async` and `async_to_sync` carry context variables across the switch. A `concurrent.futures` executor or a `threading.Thread` started by app code does not: on Python 3.12 the new thread starts without the value unless the code uses `contextvars.copy_context().run(...)`.
- **K6:** the state is not process-wide, but it is implicit. A function's dependency on the context is not visible in its signature. This is the strongest argument against the option.
- **DX:** the least boilerplate.

### 3. A request-like call object

```python
@Inventory.implement
class InventoryImpl:
    def reserve(self, call: Call[ReserveRequest]) -> ReserveReply:
        owner = call.principal.id
        sku = call.request.sku
        quote = connect("pricing.v1").quote(QuoteRequest(sku=sku), parent=call)
        ...
```

One object bundles the request and the context, much as Django's `HttpRequest` bundles the body and the user. `Call[T]` is a PEP 695 generic.

- **Typing:** good, and generic over the request type.
- **Testability:** good. The test builds a `Call`.
- **Parity:** straightforward.
- **Propagation:** manual, as in option 1, through `parent=call`.
- **Concurrency:** unaffected.
- **K6:** nothing hidden.
- **DX:** familiar to Django authors. However, the implementation's signature no longer mirrors the contract's (`reserve(self, request)`), so the import-time check and the reader must translate between two shapes. It also places a transport-carried value and a contract-typed value in one object, which works against keeping them separate.

### 4. Hybrid: explicit where the action reads it, ambient where calls propagate it

```python
@Inventory.implement
class InventoryImpl:
    def reserve(self, request: ReserveRequest, ctx: CallContext) -> ReserveReply:
        owner = ctx.principal.id
        quote = connect("pricing.v1").quote(QuoteRequest(sku=request.sku))  # child context derived by the host
        ...

    def release(self, request: ReleaseRequest) -> None:   # does not read the context, so does not declare it
        ...
```

- An action that reads the context declares a second parameter annotated `CallContext`, and receives it explicitly. An action that does not read it omits the parameter. The import-time check (K2) accepts exactly these two shapes.
- The app host also holds the same immutable value in one context variable for the duration of the action. Connectors read that variable only to derive the outgoing call's context. App code has no public way to read the variable in 0.1.
- A connector call may pass `context=` explicitly. It is treated as the parent in place of the ambient value, and the outgoing context is derived from it by the same rules. It covers the cases where the ambient value is absent, such as a thread started by app code.

Evaluation:

- **Typing:** as in option 1 for everything the app reads.
- **Testability:** as in option 1 for unit tests. Tests that make outgoing calls use the harness scope from option 2.
- **Parity:** as in options 1 and 2.
- **Propagation:** automatic, as in option 2.
- **Concurrency:** as in option 2 for propagation, but a lost context fails loudly (see Decision) instead of silently.
- **K6:** the variable is state owned by the app host and documented, and app code never reads it directly. Any helper that calls a connector still depends on it, though, and fails without it. This is the same implicit dependency as option 2, with one difference: Decision 5 makes a missing context an error instead of a silent gap. It is not removed, only made visible.
- **DX:** close to option 2. Only actions that read the context mention it.

## Decision

Option 4.

1. **`CallContext` is a frozen, typed value**, not a live object. Its fields:
   - **Propagated:** the *principal* on whose behalf the call is made, as a principal value resolved through [0010](0010-pluggable-principal-resolution.md), never a user model instance; the trace identifier; the deadline, as an absolute time on the local host.
   - **Per-call:** the idempotency key of this call, and the *caller*, the app that made this call. The caller is authenticated by the transport, not asserted, and is absent on a root context (Decision 4).

   An implementation sees an equal value over every transport, apart from the span identifier. Keeping the principal and the caller apart is what makes that possible; Decision 6 says how they cross HTTP.
2. **Inbound is explicit.** An implementation receives the context by declaring a second parameter annotated `CallContext`. An action that does not read the context omits the parameter. These are the only two shapes accepted. A mismatch fails at import time with a message naming the action.
3. **Outbound is ambient.** The app host keeps one private context variable that holds a call scope. Every scope, whether a root (Decision 4) or the scope a transport opens around an action, is marked closed when it exits, by return or by exception, and the variable is then reset. Connectors reject a closed scope as they reject a missing one (Decision 5). A connector call may instead pass `context=`. That value is treated as the parent in place of the ambient scope, never sent as is. Either way, the outgoing context is derived from the parent:
   - the deadline is the earlier of the parent's deadline and the call's own timeout;
   - the trace continues from the parent;
   - the principal is carried over unchanged;
   - the caller is the app making the call: the app the parent scope belongs to, or the app a root was opened for (a principal value of the kind 0010 calls a service);
   - the idempotency key is not inherited. The outgoing call gets its own key, which the connector reuses only when it retries that same call. When the parent has a key, the child's key is derived from it deterministically: a hash of the parent's key, the contract identity and version, the action name, and the ordinal of this call among the parent scope's calls to that action. When the parent has none (a root that was not itself a call), the key is random. A retried parent therefore reproduces its children's keys, so a child that completed during the failed attempt is answered from the provider's record instead of running twice. If a retried parent sends a different request under a reproduced key, the provider answers `conflict`, as the IETF draft prescribes for a reused key with a different payload.
4. **Roots are created only at the app host's entry points:**
   - the provider side of each transport (the HTTP transport builds the context from its headers);
   - the host's handling of ordinary Django requests (the principal from the request, through 0010);
   - an explicit scope for management commands and background work;
   - the test harness.

   Transports never fabricate a context.
5. **A connector call with no active context and no `context=` argument fails** with a contract error that says where a context is expected. It does not silently start a new, unauthenticated trace. This turns the known weakness of context variables, a context lost across a thread started by app code, into a visible failure (principle 6). OpenTelemetry starts a new root trace when no context is active, which suits it because a trace carries no authority. Our context carries the principal and the deadline, so a silent root would drop both. Go's `context` package makes the same distinction: a root is chosen explicitly (`context.Background()`), never inferred. Our explicit root is the scope from Decision 4.

   The closed scope covers the opposite weakness, a context that is kept too long. From Python 3.14, a new `threading.Thread` copies the caller's context when `sys.flags.thread_inherit_context` is set, which is the default on free-threaded builds. Executor workers keep the context they were started with for their whole life. A worker started during call A, or during a view request that the host rooted, would otherwise make later calls under that principal and deadline. Because every scope closes on exit, those calls fail instead. One case remains that the host cannot detect: a long-lived worker, started during A, that runs work for another call while A is still in progress. The documentation must say that a shared executor is used only with `context=` or with a per-task copy of the context (see Consequences).
6. **Over HTTP the caller is authenticated and the principal is asserted, in separate headers.** `Authorization` authenticates the calling app through the provider's 0010 backend, which becomes `caller`. The principal travels in a separate header as an assertion made by that caller. The provider's declaration lists which calling apps may assert a principal for it; an assertion from any other caller is refused with `permission_denied`, and a call without one runs with the caller as its principal. In-process, the host already knows both, so the two modes see the same pair. This is the shape of OAuth token exchange ([RFC 8693](https://www.rfc-editor.org/rfc/rfc8693)), where a token names a subject and, in its `act` claim, the actor, and `may_act` limits who may act; and of service meshes, where mTLS authenticates the workload and a separate token carries the end user. A later 0010 backend can replace the bare assertion with a signed delegation token in the same header without changing `CallContext`.
7. **No public accessor in 0.1.** `current_context()` from option 2 is not provided. Code below an action that needs the context takes it as a parameter, as option 1 does. If a real need appears, adding it later is a compatible change. Removing it after release would not be.
8. **`ctx` is optional per action** (Decision 2), rather than required on every action. A required parameter that most actions ignore is noise in exactly the code authors read most, and the import-time check already makes the two shapes unambiguous.

Follows principles 5 (*same semantics over same mechanics*), 6 (*visible failure over transparent calls*) and 10 (*Django-native over framework-new*): explicit arguments where code reads them, as views do, and context-local state where the framework carries it, as Django does with language and time zone.

## Consequences

- Action signatures stay close to the contract's. The context appears only where it is used.
- Deadlines and traces propagate without author effort, and a lost context is an error, not a silent gap.
- App code that starts its own threads or executors passes the context on in one of two supported ways:
  - inside an action, by passing `ctx` as `context=`;
  - where no `ctx` is at hand, such as a view rooted by the host, by running each task with `contextvars.copy_context().run(...)`. That is one copy per task, because a `Context` cannot be entered by two threads at once.

  Work that outlives the call that started it opens its own explicit scope (Decision 4), because the inherited scope is closed by then. The documentation must show all three cases.
- The conformance suite gets two tests:
  - a connector call from a thread whose target runs inside a fresh, empty `contextvars.Context()` fails as described. The empty context makes the result the same on every build;
  - a connector call made through a scope that has already closed fails the same way, tested for both a closed action scope and a closed root scope.
- The roadmap's public surface changes in one place: the `ctx` parameter becomes optional per action. CONTRIBUTING K6 is reworded to name this context variable as host-owned state once this record is Accepted.
- The specification needs:
  - the derivation rules for a child context (propagated fields: deadline, trace, principal; per-call fields: the caller and the idempotency key, with the key derivation of Decision 3);
  - a header for the asserted principal, and `Authorization` redefined in spec 03 as authenticating the calling app;
  - in the app declaration (spec 05), the list of calling apps that may assert a principal for each contract;
  - the list of places a root context may be created.
- Trust cost of Decision 6: a calling app that a provider lists can assert any principal to it. With the v0.1 shared-secret backend, anyone holding the secret can also claim to be any app, which is why 0010 already says that backend is for development only. A production backend narrows this with per-app credentials or signed delegation tokens.
- Left for later records or the specification:
  - **Calling under a different principal**, for example as the calling app's own principal. Not decided here.
  - **Interop with OpenTelemetry**, if installed, as the source of the trace. Not decided here, and no new runtime dependency is implied (K5).

## References

- Python [PEP 567](https://peps.python.org/pep-0567/), context variables; [`threading`](https://docs.python.org/3.14/library/threading.html) on `thread_inherit_context` in 3.14.
- [asgiref](https://github.com/django/asgiref): `sync_to_async` and `async_to_sync` carry context variables across the switch.
- [OpenTelemetry Python context API](https://opentelemetry-python.readthedocs.io/en/latest/api/context.html) and [W3C Trace Context](https://www.w3.org/TR/trace-context/).
- [gRPC deadlines](https://grpc.io/docs/guides/deadlines/): relative timeout on the wire, propagation to outgoing calls.
- Go [`context`](https://pkg.go.dev/context): explicit roots.
- [RFC 8693](https://www.rfc-editor.org/rfc/rfc8693), OAuth 2.0 Token Exchange: subject, actor (`act`) and `may_act`.
- [The Idempotency-Key HTTP header field](https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/) (IETF draft) and [Stripe idempotent requests](https://docs.stripe.com/api/idempotent_requests).
