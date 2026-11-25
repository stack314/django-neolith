# 0017. The call context is explicit at the action and ambient for propagation

- **Status:** Accepted
- Narrowed by [0027](0027-a-caller-backend-names-the-calling-app-over-http.md) (Decision 11)
- Narrowed by [0033](0033-eight-points-of-the-http-transport-settled-for-0-1.md) (Decision 1)
- Narrowed by [0030](0030-the-boundary-check-uses-the-standard-library-only.md) (Decision 9)
- **Accepted:** 2026-11-11
- **Date:** 2026-11-09

## Context

Every call carries a **call context**, separate from the request. The draft specification (spec 00) gives it two kinds of field:

- **Propagated fields** pass from a call to the calls it makes: the principal on whose behalf the call is made, the trace identifier and the deadline.
- **Per-call fields** belong to one outbound call: the idempotency key. Each outbound call gets a fresh key, kept only across retries of that same call.

Over HTTP the context travels in headers (`Authorization`, `Neolith-Principal`, `traceparent`, `Neolith-Timeout`, `Idempotency-Key`). The deadline crosses as a relative budget, and the receiving host computes its own local deadline from it (spec 03). Apps must not put it inside request types. The question, recorded in the open questions, is how it reaches the code that implements an action, and how it reaches the calls that code makes in turn.

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
        if ctx.principal is None:
            raise ContractError(kind="permission_denied", message="reserving needs a principal")
        owner = (ctx.principal.issuer, ctx.principal.subject)
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
        owner = (current_context().principal.issuer, current_context().principal.subject)   # raises outside a call
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
        owner = (call.principal.issuer, call.principal.subject)
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
        if ctx.principal is None:
            raise ContractError(kind="permission_denied", message="reserving needs a principal")
        owner = (ctx.principal.issuer, ctx.principal.subject)
        quote = connect("pricing.v1").quote(QuoteRequest(sku=request.sku))  # child context derived by the host
        ...

    def release(self, request: ReleaseRequest) -> None:   # does not read the context, so does not declare it
        ...
```

- An action that reads the context declares a second parameter annotated `CallContext`, and receives it explicitly. An action that does not read it omits the parameter. The import-time check (K2) accepts exactly these two shapes.
- The app host also holds the same immutable value in one context variable for the duration of the action. Connectors read that variable only to derive the outgoing call's context. App code has no public way to read the variable in 0.1.
- A connector call may pass `context=` explicitly. The value selects the parent scope in place of the ambient one, and the outgoing context is derived from that scope by the same rules. It covers the cases where the ambient value is absent, such as a thread started by app code.

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
   - **Propagated:** the *principal* on whose behalf the call is made, as a principal value resolved through [0010](0010-pluggable-principal-resolution.md), never a user model instance; the trace identifier, with the W3C `tracestate` that goes with it; the deadline, as an absolute time on the local host. A principal is *valid* when it has a sent form, which spec 03 defines, and that form passes spec 03's whole check of a bare assertion in `Neolith-Principal`. In every mode, the principal a provider receives is the one decoded from that form, so an implementation sees the same principal in-process as over HTTP. A call whose principal is not valid, or cannot be resolved, fails at the caller with `internal`, in every mode and before any transport (Decision 9, the caller's step 1), so no provider receives one from a neolith caller. As for a provider's `internal` ([0008](0008-typed-contract-errors-in-both-modes.md) Decision 3), the error's message is fixed text with the trace identifier, and the caller's host logs the cause with that identifier. This is a failure of the caller's host. It is not a usage error, because the principal comes from the 0010 backend and the end user's data, not from the calling code, and it is not `unauthenticated`, which says the calling app could not be identified (0008 Decision 7). gRPC's Go client likewise fails a call with `INTERNAL` when its outgoing metadata is invalid or larger than the server accepts. The principal is optional. It is absent on a root opened for an anonymous Django request (and on any request root before its app is bound, Decision 4), and on a command or background scope opened without one (Decision 4), and every call derived from such a root carries none.
   - **Per-call:** the idempotency key of this call, and the *caller*, the app that made this call. The caller is authenticated by the transport, not asserted, and is absent on a root that is not itself a call (Decision 4).
   - **Host-local:** an opaque *scope id*, naming the scope on this host that issued the value (Decision 3). It is never transmitted.

   An implementation sees an equal value over every transport. Equality here means equality of the propagated and per-call fields, compared with the trace's span identifier and `tracestate`, the deadline's value and the scope id left out: the span identifier changes at each hop by design, W3C lets each tracing system on the path update its own `tracestate` entry, the deadline is an absolute time on each host's own clock that crosses as a shrinking budget, and the scope id is local to one host. Whether a deadline is present is compared. `CallContext.__eq__` follows the same rule, so tests can compare values across transports directly. Keeping the principal and the caller apart is what makes that possible; [0010](0010-pluggable-principal-resolution.md) Decision 3 says how they cross HTTP.
2. **Inbound is explicit.** An implementation receives the context by declaring a second parameter annotated `CallContext`. An action that does not read the context omits the parameter. These are the only two shapes accepted. A mismatch fails at import time with a message naming the action.
3. **Outbound is ambient.** The app host keeps one private context variable that holds a call scope. Every scope, whether a root (Decision 4) or the scope a transport opens around an action, is marked closed when it exits, by return or by exception, and the variable is then reset. A request root's extent is fixed in Decision 4, including the one path where it can close late. Connectors reject a closed scope as they reject a missing one (Decision 5). A connector call may instead pass `context=`. That value only selects the parent scope, in place of the ambient one; it is never sent as is, and its fields are never read. Either way, the outgoing context is derived from the fields the parent scope issued:
   - the deadline is the earlier of the parent's deadline and the call's own `timeout=`, if given. A call's timeout can only shorten the deadline, never extend it, as with gRPC's per-call deadlines. A call whose remaining budget rounds down to 0 ms fails with `timeout` before it is sent. Over HTTP the budget crosses as `Neolith-Timeout`, in milliseconds: 1 to 10 ASCII digits, the first not `0` (`%x31-39 0*9DIGIT`). That is stricter than Connect's `Connect-Timeout-Ms`, a positive integer of at most 10 digits, which does not rule out a leading zero. It is absent when there is no deadline, and a call that arrives without it has none. A provider's host fails a call whose header is present but does not match, such as `0050` or `0`, with `invalid_argument`. A budget above 9999999999 ms counts as 9999999999, and a provider's host may cap it lower, at a maximum of its own, as Connect allows servers to. Both caps apply in every mode, at the steps Decision 9 names, so the same caps bound the budget in-process and over HTTP;
   - the trace continues from the parent;
   - the principal is carried over unchanged;
   - the caller is the app making the call: the app the parent scope belongs to, or the app a root was opened for;
   - the idempotency key is not inherited. The outgoing call gets its own key, which the connector reuses only when it retries that same call. When the parent has a key, the child's key is derived from it deterministically:

     ```
     child_key = base64url(SHA-256(JCS([parent_key, contract_id, action, base64url(SHA-256(JCS(request))), count])))
     ```

     Here `JCS` is the JSON Canonicalization Scheme ([RFC 8785](https://www.rfc-editor.org/rfc/rfc8785)), `base64url` is RFC 4648 section 5 without padding, `contract_id` is the contract identity, such as `<contract>.v1`, `request` is the request's JSON wire form, which this derivation requires to follow the I-JSON encodings of [0019](0019-wire-encodings-of-the-portable-forms.md), and `count` is an occurrence count: how many calls with the same contract, action and request hash the parent has made before this one.

     The derivation does not depend on the order in which calls reach the connector, so it gives the same keys under concurrent fan-out (`asyncio.gather`, or an executor running each task in its own copy of the context). Only identical requests share a count, and those are interchangeable. When the parent has no key, the child's key is random: 128 bits from a cryptographically secure source, base64url-encoded. A parent has no key when its root is not itself a call, or when it arrived over HTTP without `Idempotency-Key`. In that second case a retry of the parent gets new child keys, so its children lose the protection the derivation gives. Over HTTP the key travels in `Idempotency-Key`, which the IETF draft defines as a structured-field Item whose value is a String (RFC 9651). This record narrows it to a String of 1 to 255 characters, counted after unescaping, with no parameters. A provider's host fails a call whose header is present but does not match with `invalid_argument`.

     The occurrence counts belong to the parent's scope, not to a context value. The host keeps them on the scope it opened for the parent call, behind a lock. A value passed as `context=` is matched to its scope by the value's scope id (Decision 1), looked up in the host's table of open scopes. Matching never uses object identity, so a copy of the value still matches. The scope id proves only which open scope is meant; the parent's deadline, trace, principal and key are read from that scope, never from the passed value. A value rebuilt with `dataclasses.replace` to carry a later deadline, another principal or another key therefore changes nothing, and a child's deadline is never later than its parent's (spec 00). Ambient calls and explicit `context=` calls from any thread therefore share one set of counts and one set of parent fields. A `context=` value whose scope has closed, or whose scope id names no open scope on this host, is rejected as in Decision 5.

     A retried parent therefore reproduces its children's keys, and an idempotent child keeps its own promise (spec 01) across the parent's retry. The specification does not make a host remember completed calls, so this record promises nothing more: a non-idempotent child still runs again when its parent is retried. A host-side idempotency record would close that gap; see Consequences.
4. **Roots are created only at the app host's entry points:**
   - the provider side of each transport (the HTTP transport builds the context from its headers, spec 03). It follows W3C Trace Context for `traceparent`, including its rules for versions above `00`. When `traceparent` is absent or invalid, it starts a fresh trace and drops `tracestate`; otherwise it passes `tracestate` on;
   - the host's handling of ordinary Django requests. Each request gets a *request root*, which follows four rules:
     - **App.** The root is bound to the app that supplies the resolved view, meaning the app whose code runs for it, not a module that merely wraps it, or, for a registered admin view with no app code of its own, the app whose model it serves. A view whose code is in no installed app, including a project-level admin class, gives a root with no app, so calls from it have no caller and are refused (0010 Decision 7), and `neolith check` warns about such views at startup (K2). Code that calls a contract on its own app's account from a root bound to another app opens its own scope naming its app. This covers middleware, error views, and code reached through another app's view.
     - **Principal and trace.** The root's principal is the request's principal, resolved through 0010. The principal and the trace identifier are each fixed once per root, at the latest by the root's first connector call, and concurrent first calls share them. A failure to resolve the principal is recorded on the root once and fixed for the root's lifetime, never retried: every contract call from that root then fails at the caller's step 1 with `internal` (Decision 9). A call made before the root is bound to its app has neither a caller nor a principal, and is refused (0010 Decision 7).
     - **Deadline.** A request root, for an ordinary Django request, gets the host's default deadline: 30 seconds unless the host is configured otherwise, and the configuration can set it to none.
     - **Extent.** The root is open from the start of the request until the response is done, including a streamed body, and is closed then, or, on a path where the server gives the host no signal that the response is done, when the host next detects it. The host closes it through its own reference to it, never through the context variable. Consequences names the one such path known on Django 5.2 and its bound.
     - **Cost.** The host adds no per-request cost that the app does not use. A request that makes no connector call causes no principal resolution on the host's account, and so no read of the request inputs the 0010 backend uses (for the session-based Django user: no session read, no user query). A streamed body is the one exception, because the host cannot know in advance whether the body will call; there the principal is resolved before the response is returned, and a failure to resolve it there is only recorded, with no call in flight: the response is unchanged, and the body's first contract call, like every later one, fails with `internal`. Whenever the principal may shape the response, the response varies on the request inputs the 0010 backend reads (`Cookie` for the session-based Django user).

     How the Django host meets these rules, and the edge cases it handles, is described in django-neolith's [Django host notes](../django-host.md), which are not part of this decision;
   - an explicit scope for management commands and background work. Opening one requires naming the app it runs for, and optionally a principal and a deadline. Without one, it has no deadline;
   - the test harness. It opens a scope for the app under test, with no deadline unless the test gives one. The context factory that unit tests use registers each value it builds as its own scope inside the harness scope, carrying the fields the test chose. A test can therefore vary the principal or the deadline, and a `ctx` it hands to an action, and passes on as `context=`, was issued by an open scope whose fields match it.

   Transports never fabricate a context.
5. **A connector call with no active context and no `context=` argument fails** with an error that says where a context is expected. The error is a usage error, not a `ContractError` ([0008](0008-typed-contract-errors-in-both-modes.md) Decision 6): it reports a bug in the calling code, not the outcome of a call. It does not silently start a new, unauthenticated trace. This turns the known weakness of context variables, a context lost across a thread started by app code, into a visible failure (principle 6). OpenTelemetry starts a new root trace when no context is active, which suits it because a trace carries no authority. Our context carries the principal and the deadline, so a silent root would drop both. Go's `context` package makes the same distinction: a root is chosen explicitly (`context.Background()`), never inferred. Our explicit root is the scope from Decision 4.

   The closed scope covers the opposite weakness, a context that is kept too long. From Python 3.14, a new `threading.Thread` copies the caller's context when `sys.flags.thread_inherit_context` is set, which is the default on free-threaded builds. Executor workers keep the context they were started with for their whole life. A worker started during call A, or during a view request that the host rooted, would otherwise make later calls under that principal and deadline. Because every scope closes on exit, those calls fail instead, apart from the late-close path of Decision 4, which Consequences bounds. One case remains that the host cannot detect: a long-lived worker, started during A, that runs work for another call while A is still in progress. The documentation must say that a shared executor is used only with `context=` or with a per-task copy of the context (see Consequences).
6. **The allow-list of [0010](0010-pluggable-principal-resolution.md) applies on every transport.** How the calling app and the principal cross, which calling apps may assert a principal, and what a call from no app gets are 0010 Decisions 3 and 7. The principal travels in its own header as the calling app's assertion (0010 Decision 3). 0010 Decision 3 cites this decision as proposed; with this record Accepted, that reference is to this Decision 6. This record adds three things:

   - **One list per provided contract.** The provider's declaration holds, for each contract it provides, the calling apps that may assert a principal. The list governs only principal assertions, as `may_act` does in RFC 8693, not who may call at all.
   - **In-process too.** The provider host checks the list on every transport. In-process, the host knows the caller and the propagated principal, and treats a present principal as asserted by the caller, as over HTTP. Otherwise an attached caller that is not listed would succeed with the end user's principal and start failing the day it is detached ([0007](0007-in-process-transport-serializes.md), principle 5). With [0016](0016-an-app-may-run-attached-and-detached-at-once.md), the same call could also pass or fail depending on which provider the connector picks.
   - **No principal, no assertion.** A call whose root had no principal (Decision 1) asserts none, so the list does not apply. Whether the action allows a call with no principal is the action's own check; one that needs a principal refuses with `permission_denied`.
7. **No public accessor in 0.1.** `current_context()` from option 2 is not provided. Code below an action that needs the context takes it as a parameter, as option 1 does. If a real need appears, adding it later is a compatible change. Removing it after release would not be.
8. **`ctx` is optional per action** (Decision 2), rather than required on every action. A required parameter that most actions ignore is noise in exactly the code authors read most, and the import-time check already makes the two shapes unambiguous.

9. **Order of checks.** On the caller's side, in both modes:

   1. The call context must be present; a call without one is a usage error. A principal not yet resolved is resolved now (Decision 4). If resolving it raises an `Exception`, or the principal is not valid (Decision 1), the call fails with `internal`.
   2. The request is checked: its exact type, the serializer, the description check, and the declared type's validator run on the dumped JSON text. A failure is a usage error, so a neolith caller never receives `invalid_argument` for its own request.
   3. The idempotency key is derived, and the parent's occurrence count goes up by one.
   4. A remaining budget that rounds down to 0 ms fails with `timeout`, and one above 9999999999 ms is capped at 9999999999.
   5. The call is sent.

   A call counts as made once it passes steps 1 and 2, whatever the deadline does next. The connector runs these steps before the transport, so the transport's requirements in spec 02 apply only to calls that passed steps 1 and 2. On the provider's side:

   1. The calling app is authenticated (`unauthenticated`).
   2. If the call carries a principal and the calling app is not on the contract's allow-list, the call fails with `permission_denied`, whatever the principal header holds.
   3. Over HTTP, the neolith headers (the principal, the timeout and the idempotency key) are parsed and validated (`invalid_argument`).
   4. The budget is capped at the host's own maximum, if it has one, and the deadline is checked (`timeout`).
   5. The request gets the receive check (`invalid_argument`).
   6. The call is dispatched.

   A connector retries only an idempotent action, only on `unavailable`, and only within the call's one deadline, as in gRPC's retry design (A6). It never retries or falls back on `timeout`, whose outcome is unknown.

Follows principles 5 (*same semantics over same mechanics*), 6 (*visible failure over transparent calls*) and 10 (*Django-native over framework-new*): explicit arguments where code reads them, as views do, and context-local state where the framework carries it, as Django does with language and time zone.

## Consequences

- Action signatures stay close to the contract's. The context appears only where it is used.
- Deadlines and traces propagate without author effort, and a lost context is an error, not a silent gap.
- App code that starts its own threads or executors passes the context on in one of two supported ways:
  - inside an action, by passing `ctx` as `context=`;
  - where no `ctx` is at hand, such as a view rooted by the host, by running each task with `contextvars.copy_context().run(...)`. That is one copy per task, because a `Context` cannot be entered by two threads at once.

  Work that outlives the call that started it opens its own explicit scope (Decision 4), because the inherited scope is closed by then. The documentation must show all three cases.
- The conformance suite gets six call-context tests, listed in spec 06's outline. The lost-context test runs its target inside a fresh, empty `contextvars.Context()`, so its result is the same on every build. The Django host's request-root cases are tests of the reference implementation, in the test plan of roadmap step 4 (#11), not part of conformance.
- The roadmap's public surface changes in one place: the `ctx` parameter becomes optional per action. CONTRIBUTING K6 names this context variable as host-owned state.
- Spec 02 and 03 describe the derivation of a child context, the key derivation, the roots, the deadline header and the trace header in outline. They become requirements with tests in roadmap steps 3 (#10) and 5 (#12). The per-contract list joins the app declaration (spec 05) in step 4 (#11).
- One path closes a request root late on Django 5.2's ASGI handler: a streaming response with status 400 or above whose client disconnects while Django logs the response, before the body is sent. The server then gives the host no signal that the response is done, and the root closes only when the host detects it, at the next cyclic garbage collection that reaches the body wrapper, with no fixed bound; in a long-running worker that can be minutes. The body never runs, but an executor that inherited the root can still call through it until then, and each such call is bounded only by its own deadline (spec 02). The documentation warns about this window.
- 0010 backends declare which request inputs they read to resolve a principal, so that the host can vary responses on them (Decision 4, Cost). 0010 records this; the specification still needs it.
- Apps that call each other on behalf of users list each other in their declarations. The roadmap's 0.1 skeleton, where two apps call each other, includes those entries. This is intended least privilege, not a workaround: delegation is granted per contract, not implied by being able to reach it.
- Trust cost of Decision 6: a calling app that a provider lists can assert any principal to it. With the v0.1 shared-secret backend, anyone holding a calling app's secret, which every provider it calls holds, can also claim to be that app, which is why 0010 says that backend is for development only. A production backend narrows this with per-app credentials or signed delegation tokens.
- Left for later records or the specification:
  - **A host-side idempotency record**, so that a retried parent does not run a non-idempotent child twice. It must say which actions it covers, what it stores (the key, the request hash, and the reply or error), how long it keeps an entry, a distinct error for a key reused with a different request, separate from `conflict`, which spec 01 reserves for a clash with current state, and that stored keys are scoped per caller and principal. Derived keys are predictable from the parent key, which the upstream caller chooses, so an unscoped record would let a caller replay another chain's key and be answered from its stored reply. Stripe's idempotent requests and the IETF draft below are the models. It is in the [open questions](../open-questions.md).
  - **Calling under a different principal**, for example as the calling app's own principal. Not decided here.
  - **Interop with OpenTelemetry**, if installed, as the source of the trace. Not decided here, and no new runtime dependency is implied (K5).

## References

- Python [PEP 567](https://peps.python.org/pep-0567/), context variables; [`threading`](https://docs.python.org/3.14/library/threading.html) on `thread_inherit_context` in 3.14.
- [asgiref](https://github.com/django/asgiref): `sync_to_async` and `async_to_sync` carry context variables across the switch.
- [OpenTelemetry Python context API](https://opentelemetry-python.readthedocs.io/en/latest/api/context.html), [W3C Trace Context](https://www.w3.org/TR/trace-context/) (an invalid `traceparent` is ignored and a new trace started) and [W3C Baggage](https://www.w3.org/TR/baggage/), which carries application context in its own header; neolith's context does not travel in it.
- [Connect protocol](https://connectrpc.com/docs/protocol): `Connect-Timeout-Ms`, at most 10 digits, which servers may clamp.
- [gRPC deadlines](https://grpc.io/docs/guides/deadlines/): relative timeout on the wire, propagation to outgoing calls; [gRPC A6, client retries](https://github.com/grpc/proposal/blob/master/A6-client-retries.md): retries within the call's one deadline; [grpc-go](https://github.com/grpc/grpc-go) 1.68: a client fails a call with `INTERNAL` when its outgoing metadata is invalid (`stream.go`) or larger than the server's header list limit (`internal/transport/http2_client.go`).
- [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785), JSON Canonicalization Scheme: the principal's sent form and the key derivation.
- [RFC 9651](https://www.rfc-editor.org/rfc/rfc9651), Structured Field Values for HTTP.
- Go [`context`](https://pkg.go.dev/context): explicit roots.
- [RFC 8693](https://www.rfc-editor.org/rfc/rfc8693), OAuth 2.0 Token Exchange: subject, actor (`act`) and `may_act`.
- [The Idempotency-Key HTTP header field](https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/) (IETF draft) and [Stripe idempotent requests](https://docs.stripe.com/api/idempotent_requests).
