# 0017. The call context is explicit at the action and ambient for propagation

- **Status:** Proposed
- **Date:** 2026-11-09

## Context

Every call carries a **call context**, separate from the request: at least the principal on whose behalf the call is made, a trace identifier, an idempotency key and a deadline. Over HTTP it travels in headers (`Authorization`, `traceparent`, `Idempotency-Key`, `Neolith-Deadline`). Apps must not put it inside request types. The question, recorded as open question 6, is how it reaches the code that implements an action, and how it reaches the calls that code makes in turn.

Two directions have to work, and they pull different ways:

- **Inbound.** An implementation needs to read the context, mostly the principal ([0010](0010-pluggable-principal-resolution.md)), sometimes the deadline.
- **Outbound.** When an action calls another contract, the outgoing call must inherit the deadline (never extend it), continue the trace, and carry the principal. If this depends on every author remembering to pass something along, it will sometimes be forgotten, and the failure is silent: a call runs past its caller's deadline, or the trace breaks.

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

1. **`CallContext` is a frozen, typed value**, not a live object. It holds the principal as a principal value resolved through [0010](0010-pluggable-principal-resolution.md), never a user model instance. It also holds the trace identifier, the idempotency key and the absolute deadline. An implementation sees an equal value over every transport, apart from the span identifier and, for now, the principal. Over HTTP the principal is whatever the provider's 0010 backend resolves from `Authorization`. With the v0.1 shared-secret backend that is the calling app, while in-process it is the original principal. Closing that gap is listed under Consequences as a decision still to make.
2. **Inbound is explicit.** An implementation receives the context by declaring a second parameter annotated `CallContext`. An action that does not read the context omits the parameter. These are the only two shapes accepted. A mismatch fails at import time with a message naming the action.
3. **Outbound is ambient.** The app host keeps one private context variable that holds a call scope. Transports set it around each action, mark the scope closed when the action returns or raises, and then reset the variable. Connectors reject a closed scope as they reject a missing one (Decision 5). A connector call may instead pass `context=`. That value is treated as the parent in place of the ambient scope, never sent as is. Either way, the outgoing context is derived from the parent:
   - the deadline is the earlier of the parent's deadline and the call's own timeout;
   - the trace continues from the parent;
   - the principal is carried over unchanged.
4. **Roots are created only at the app host's entry points:**
   - the provider side of each transport (the HTTP transport builds the context from its headers);
   - the host's handling of ordinary Django requests (the principal from the request, through 0010);
   - an explicit scope for management commands and background work;
   - the test harness.

   Transports never fabricate a context.
5. **A connector call with no active context and no `context=` argument fails** with a contract error that says where a context is expected. It does not silently start a new, unauthenticated trace. This turns the known weakness of context variables, a context lost across a thread started by app code, into a visible failure (principle 6).

   The closed scope covers the opposite weakness, a context that is kept too long. From Python 3.14, a new `threading.Thread` copies the caller's context when `sys.flags.thread_inherit_context` is set, which is the default on free-threaded builds. Executor workers keep the context they were started with for their whole life. A worker started during call A would otherwise make later calls under A's principal and deadline. With the scope closed after A returns, those calls fail instead. One case remains that the host cannot detect: a long-lived worker, started during A, that runs work for another call while A is still in progress. The documentation must say that a shared executor is used only with `context=` or with a per-task copy of the context (see Consequences).
6. **No public accessor in 0.1.** `current_context()` from option 2 is not provided. If a real need appears, adding it later is a compatible change. Removing it after release would not be.

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
  - a connector call made through a scope that has already closed fails the same way.
- The roadmap's public surface changes in one place: the `ctx` parameter becomes optional per action. CONTRIBUTING K6 is reworded to name this context variable as host-owned state once this record is Accepted.
- The specification needs:
  - the derivation rules for a child context (deadline, trace, principal);
  - the list of places a root context may be created.
- Left for later records or the specification:
  - **Idempotency keys for child calls.** A child call needs its own key. Whether it is derived deterministically from the parent's key, so that a retried parent reproduces its children's keys, belongs to the spec for transports.
  - **The principal over HTTP.** In-process, the provider sees the original principal. Over HTTP with the v0.1 shared-secret backend of 0010, it sees the calling app. There are two ways to close the gap, and they have different trust costs:
    - the HTTP transport forwards the original principal, and the service token asserts it on the caller's behalf. Parity holds, but anyone holding the shared secret can then assert any principal;
    - the provider sees the calling app, and parity for the principal is narrowed to attached mode until a backend can carry delegated identity.

    To be decided before this record is Accepted, or in a record of its own.
  - **Calling under a different principal**, for example as the calling app's own principal. Not decided here.
  - **Interop with OpenTelemetry**, if installed, as the source of the trace. Not decided here, and no new runtime dependency is implied (K5).
