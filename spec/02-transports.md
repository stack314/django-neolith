# 02. Transports and routing

**Status:** outline.

## The transport interface

The connector runs the caller's own checks (ADR 0017, Decision 9) before it hands a call to a transport, so the requirements below apply to calls that passed them.

A transport exposes one operation to a connector:

```
call(contract, version, action, request, context) -> reply | error
```

A transport MUST:

- **TRN-1.** serialize the request into the portable type system before it leaves the caller, and deserialize the reply before it reaches the caller, even when caller and provider share a process;
- **TRN-2.** carry the call context intact;
- **TRN-3.** map every failure it can observe onto a standard error kind ([01](01-contracts.md)); a transport MUST NOT surface its own exception types to the caller;
- **TRN-4.** enforce a deadline: a call whose deadline passes before the call ends fails with `timeout`.

Which kinds a transport produces is set by [ADR 0008](../docs/adr/0008-typed-contract-errors-in-both-modes.md): `unavailable` or `timeout` for a failure of its own in carrying a call, and `internal` for a reply or error from the provider that does not validate against the contract, apart from an error of a kind the caller does not know. A request that fails the caller's own check is never sent, and the caller and the provider run their checks in the order given under [The call context across calls](#the-call-context-across-calls), so a caller that checks its own requests gets the same result in every mode ([ADR 0007](../docs/adr/0007-in-process-transport-serializes.md)). An error the provider's host delivers otherwise passes through unchanged.

## Values across a call

Every value that crosses a call, whether a request, a reply or an error's detail record, crosses as JSON text, in every mode ([ADR 0007](../docs/adr/0007-in-process-transport-serializes.md)). A value's *declared type* is the record the contract names for it: the action's request or reply type, or the detail record of the error's kind.

**TRN-9.** A sender MUST refuse a value whose type is not exactly its declared type, such as a value of a subtype, or of another type with the same fields. The check covers the value itself. A record inside it crosses as the JSON text written for it, and only when that text passes the checks below.

**TRN-10.** A sender MUST write a value as JSON text by the rules of its declared type that the contract's description is derived from (CON-16), once converted as CON-35 and CON-36 say. Nothing a value or its type overrides, such as its own way of writing itself as JSON, changes the text.

**TRN-11.** A sender MUST refuse a value that holds, at any depth, something its declared type does not hold at that place, such as text in an integer field, rather than write it in another form. A place holds a value only when the JSON text written for it reads back as an equal value of the same form. So a boolean in an integer field is refused although it could be written as `1`, and so is an integer in a number field that no double equals. A value of a subtype of a place's type, such as a member of an integer enumeration in an integer field, is refused too, and a map's key is held only as exactly a string, since the reader hands on a value of the type itself. A date of a subtype other than a timestamp, and a timestamp of a subtype, are the one exception: each is held as the date or the instant it is.

**TRN-15.** A sender MUST NOT send a value, whether a request, a reply or an error's detail record, whose JSON text the contract's published description (CON-16) refuses, or that breaks a rule the description cannot state (CON-40).

**TRN-12.** A receiver MUST build the value it hands on by reading the JSON text with its declared type, ignoring unknown fields (CON-6, CON-7), so that caller and provider never share an object, in-process too.

**TRN-13.** Before a call is sent, the caller MUST check its request with the request check of [ADR 0017](../docs/adr/0017-call-context-explicit-at-the-action-ambient-for-propagation.md) Decision 9, the caller's step 2, in its order. Writing the request there follows TRN-9 to TRN-11, its description check is that of TRN-15, and reading it back follows TRN-12.

**TRN-14.** A request that fails that check MUST NOT be sent. The failure is a mistake in the calling code (CON-29), not a contract error, so a caller that checks its own requests never receives `invalid_argument` for one.

## In-process transport

**TRN-5.** Defined by this specification and REQUIRED in every implementation. Caller and provider share a process. The transport still serializes and deserializes ([ADR 0007](../docs/adr/0007-in-process-transport-serializes.md)), so that a provider cannot leak a live object and a caller cannot depend on one. Values cross as JSON text and are checked against the contract's published description (CON-16): once in-process, on the sending side, and on both sides over a network.

**TRN-6.** The in-process transport MUST run the provider's action in the caller's thread of control, so that attached mode has no hidden concurrency.

Because the action runs in the caller's thread, the in-process transport cannot stop waiting at the deadline, as a caller does over a network, and it never interrupts an action. It checks the deadline instead ([ADR 0008](../docs/adr/0008-typed-contract-errors-in-both-modes.md), Decision 2).

**TRN-19.** The in-process transport MUST check a call's deadline before it dispatches the call, at the step the provider's order gives it (TRN-35), and again when the action has ended and its outcome is ready to return.

**TRN-20.** A call whose deadline has passed at either check MUST fail with `timeout`. At the second check the action's reply, an error it produced or a failure it ended with is discarded, and the provider's host still records a discarded error or failure. The action might have run, so `timeout` leaves the outcome unknown, as it does over a network. This comes before what CON-43, CON-44, CON-48 or TRN-18 would have the provider's host deliver.

**TRN-21.** The in-process transport MUST NOT discard a signal to stop, such as cancellation or the process exiting, whatever the deadline. It is not a failure of the action (CON-45).

## Connectors

A *connector* is what calling code holds to call one contract version. It runs the caller's checks (ADR 0017, Decision 9), then hands the call to a transport.

**TRN-16.** A connector MUST let its caller call each action of its contract version by name, with one request of the action's request type, and return the action's reply. A call to an action the contract version does not have, or without exactly one request, is a mistake in the calling code (CON-29).

**TRN-17.** A connector with the `single` policy MUST NOT send a call more than once, or to any provider but its one.

**TRN-18.** A reply that its provider cannot send under TRN-9 to TRN-11 and TRN-15, or that the caller cannot read under TRN-12, MUST reach the caller as `internal`. That includes a reply to an action whose reply is empty, and a missing reply to one whose reply is not.

## Providers and routing

A connector holds an ordered set of providers for a contract version. Each provider names a transport and the configuration that transport needs.

A **routing policy** chooses the provider for each call. Defined policies:

- `single`: exactly one provider; required in 0.1;
- `local-first`: prefer an in-process provider, and fall back to the others under the fallback rule below; required in 0.1;
- `remote-first`: the reverse;
- `weighted`: distribute by configured weights;
- `explicit`: chosen by a function of the call context.

~~**TRN-7.** Fallback after a failed call MUST happen only for idempotent actions and only on `unavailable` or `timeout`.~~ Withdrawn in spec 0.1: `timeout` leaves the outcome unknown, so it is no longer grounds for fallback (ADR 0017).

**TRN-8.** A retry or a fallback after a failed call MUST happen only for an idempotent action, only on `unavailable`, and only within the call's deadline.

Decision records: [0014](../docs/adr/0014-pluggable-connector-transports.md), [0016](../docs/adr/0016-an-app-may-run-attached-and-detached-at-once.md).

## The call context across calls

How the call context of [00](00-overview.md) moves from call to call follows [ADR 0017](../docs/adr/0017-call-context-explicit-at-the-action-ambient-for-propagation.md).

A host opens a *scope* for each root and around each action it runs. A scope holds a context, and the calls made in it derive theirs from it. A *root* is a scope that starts a context: the provider side of a transport, a request the host serves, a scope opened explicitly for a command or background work, or a test harness. A transport never fabricates a context. A request root for an ordinary request the host serves gets the host's default deadline, 30 seconds unless the host is configured otherwise, or none if it is configured so. A call that arrives over a transport without a deadline has none. A command or background scope, and a test harness, have no deadline unless they are given one. The request root is under [Request roots](#request-roots), and the explicit root for commands and background work under [Explicit roots](#explicit-roots).

### The parent scope

**TRN-22.** A connector MUST derive each call's context from one open scope of its host, the call's *parent*: the scope the calling code names or, when it names none, the scope the call is made in.

**TRN-23.** A call made in no scope, and naming none, MUST NOT be sent, or given a context of its own. It is a mistake in the calling code (CON-29).

**TRN-24.** A host MUST close a scope when what it was opened for ends, however that ends.

**TRN-25.** A call made in a closed scope, or naming one, MUST be refused as TRN-23 refuses a call made in none, so work that outlives its call cannot go on calling under that call's principal and deadline.

**TRN-26.** When calling code names the parent by a context value, the connector MUST use the value only to find the open scope that issued it. The call's context comes from what that scope holds. So a copy of the value names the same scope, a copy changed to carry a later deadline, another principal or another key derives the same call, and a value this host did not issue names no scope.

### Explicit roots

Commands and background work run outside any request or call, so their code opens a root of its own, naming the app it runs for ([ADR 0017](../docs/adr/0017-call-context-explicit-at-the-action-ambient-for-propagation.md) Decision 4, [ADR 0022](../docs/adr/0022-the-app-host-builds-settings-and-checks-before-it-serves.md) Decision 8). Work that outlives its call, which TRN-25 refuses, continues this way.

**TRN-44.** A host MUST let code open an explicit root that names the app it runs for, with a principal and a timeout that are each optional.

**TRN-45.** A host MUST refuse to open an explicit root that names an app the host does not host, or that is opened before Validate has ended. The refusal is a mistake in the calling code (CON-29), and no root opens.

**TRN-46.** An explicit root MUST belong to the app it names, so the calls made in it carry that app as their calling app (TRN-31) and are checked against that app's declaration (TRN-39).

**TRN-47.** An explicit root MUST hold the principal it is given, and no principal when it is given none.

**TRN-48.** An explicit root MUST have a deadline only when it is given a timeout, and that deadline is the timeout counted from when the root opens. A timeout of zero or less gives a root whose deadline has passed (TRN-28); a budget above the cap counts as the cap (TRN-29).

**TRN-49.** An explicit root MUST start a new trace. It has no idempotency key, so the keys of its calls are random (TRN-33).

**TRN-50.** An explicit root opened inside another scope MUST NOT take anything from that scope: not its app, its principal, its deadline or its trace.

**TRN-51.** When an explicit root opened inside another scope closes where it is the scope calls are made in, calls MUST be made in that enclosing scope again.

An explicit root is opened and closed in one flow of execution, such as a thread or an asyncio task, and closes the roots opened in it first; an `await` inside it stays in that flow. A root held across a generator's `yield` can close in another flow, or after the code around the generator has made another scope current, and the enclosing scope cannot then be made current there.

**TRN-53.** A host MUST report an explicit root that closes outside the flow of execution it was opened in, or where it is not the scope calls are made in, as a mistake in the calling code (CON-29). The root still closes (TRN-24), so a call made where it was left current is refused (TRN-25). The report replaces an ordinary error the code ended with, which stays attached to the report, and gives way to TRN-54.

**TRN-54.** The report of TRN-53 MUST NOT replace a signal to stop that the code ends with, such as cancellation or the process exiting. The signal goes on, carrying the report, as TRN-21 and [ADR 0008](../docs/adr/0008-typed-contract-errors-in-both-modes.md) Decision 3 keep such a signal. The host also logs the report, since closing a generator swallows the signal that closes it.

**TRN-52.** A host MUST refuse to open one explicit root a second time, while it is open or after it has closed, as TRN-45 refuses.

An explicit root closes when the code it was opened for ends, by return or by an error (TRN-24), and a call made in it afterwards is refused (TRN-25).

### Request roots

A host that serves ordinary requests, such as web pages, opens a root for each one ([ADR 0017](../docs/adr/0017-call-context-explicit-at-the-action-ambient-for-propagation.md) Decision 4, [ADR 0022](../docs/adr/0022-the-app-host-builds-settings-and-checks-before-it-serves.md) Decision 6). The code that handles the request runs in it, and so does a body the response produces after that code has returned. The root's principal and trace are the subject of the principal backends ([ADR 0023](../docs/adr/0023-a-request-principal-comes-from-ordered-backends.md)).

**TRN-55.** A host MUST open a request root for each ordinary request it serves when it starts to handle the request, before any other code that handles the request runs.

**TRN-56.** A request root MUST belong to no app until the host has found the code that serves the request, so a call made before then has no calling app (TRN-31) and is refused (TRN-42).

**TRN-57.** Once the host has found the code that serves the request, the request root MUST belong to the app that code is in: the app whose own code runs for the request, not the app of code that only wraps it. Where the framework serves an app's data through generic code of its own, with no code of an app, the root belongs to the app whose data it serves.

The host finds the code that runs by following each link a wrapper keeps to what it wraps, while the link leads into code of an app the host holds, never into the framework's own code or code the view only inherits. A wrapper whose link is not followed counts as the code that runs, so a view wrapped by another app's code that keeps no link serves as that app. Generic code of the framework includes the framework's own apps, so an administration class that the framework or an app with no declaration supplies serves the app whose data it shows.

**TRN-58.** A request root whose serving code is in no app MUST belong to no app, so a call made in it is refused as TRN-56 says.

**TRN-59.** A request root MUST have the host's request deadline, counted from when the request starts: 30 seconds unless the host is configured with another duration, or none when the host is configured with none.

**TRN-60.** A request root MUST stay open until the response is done, including while a body is produced after the code that handles the request has returned. A body whose production runs no code, such as a file the server sends as it is, needs no root.

**TRN-61.** A call made while the response's body is produced, or while the body is closed, MUST be made in the request root, wherever the server does either.

A server may stop an asynchronous body before its end without closing it, as it may when a client disconnects. The body's own clean-up then runs when its event loop finishes it, after the response is done, and a call made there is refused (TRN-25).

**TRN-62.** A host MUST close a request root when the response is done, however it ends, a client that disconnects included (TRN-24).

**TRN-63.** Where the server gives the host no signal that the response is done, the host MUST close the request root when it next detects that the response will not be produced.

### The context of a call

**TRN-27.** A call's deadline MUST be the earlier of its parent's deadline and the call's own timeout, if it is given one. The timeout is counted from when the calling code asks the connector for the call, so the time the caller's own checks take comes off it. A timeout therefore never extends a deadline. A call with neither has no deadline.

The deadline crosses as a *budget*: the time that remains when the call is sent, in whole milliseconds, rounded down ([03](03-http-transport.md)).

**TRN-28.** A caller MUST fail a call whose budget is less than 1 ms with `timeout`, without sending it.

**TRN-29.** A budget above 9999999999 ms MUST count as 9999999999, in every mode.

**TRN-38.** A caller MUST NOT receive a reply or an error that comes back after the call's deadline, as the caller's own clock holds it: the call fails with `timeout`, in every mode. Over a network the caller stops waiting at its deadline. In-process the connector checks the caller's deadline when the outcome is back, since the provider's side counts its deadline from the budget when the call arrived (TRN-36), which can be later. A `timeout` that a transport or a provider's host produced is passed on as it is. This comes before what TRN-18, CON-28, CON-48 or CON-49 would have the caller receive.

**TRN-30.** A call MUST carry its parent's principal, trace identifier and `tracestate`, unchanged.

**TRN-31.** A call's calling app MUST be the app its parent scope belongs to, the app that scope's root was opened for or whose action it runs. A call from a scope that belongs to no app carries none.

**TRN-32.** When its parent has an idempotency key, a call's key MUST be derived from it, as the value of this expression.

```
base64url(SHA-256(JCS([parent_key, contract_id, action, base64url(SHA-256(JCS(request))), count])))
```

`JCS` is RFC 8785, `base64url` is RFC 4648 section 5 without padding, `contract_id` is the contract identity, such as `inventory.v1`, `request` is the request's JSON wire form under the I-JSON encodings of [ADR 0019](../docs/adr/0019-wire-encodings-of-the-portable-forms.md), and `count` is how many calls with the same contract, action and request hash the parent made before this one. A call counts as made once it passes the caller's context and request checks, whatever becomes of it then. The key does not depend on the order in which the parent's calls are made, so a retried parent reproduces its children's keys, under concurrent fan-out too.

**TRN-33.** When its parent has no key, a call's key MUST be random: 128 bits from a cryptographically secure source, base64url-encoded. A retry of such a parent does not reproduce its children's keys.

**TRN-34.** A caller MUST run its checks in the order ADR 0017 Decision 9 gives the caller, so a call that would fail two of them fails at the earlier one, in every mode. A call that fails one of them is never sent, so nothing comes back for it, and the rules for a reply or an error that arrives (TRN-18, CON-28, CON-48, CON-49) do not apply to it.

### The provider's side

**TRN-35.** A provider's host MUST run its checks in the order ADR 0017 Decision 9 gives the provider. Its checks of the calling app are those under [The calling app](#the-calling-app).

**TRN-36.** The provider's side of a transport MUST run the action in a scope that holds the context of the call as it arrived: the principal read from its sent form ([03](03-http-transport.md)), the trace identifier and `tracestate`, a deadline counted from the budget when the call arrived, the call's idempotency key and the calling app.

**TRN-37.** An action that takes the call context MUST receive the context of that scope.

Two contexts are equal when their propagated and per-call fields are equal, leaving out the trace's span identifier, which changes at each hop, its `tracestate`, which each tracing system on the path may update, and the deadline's value, which each host holds on its own clock. Whether a deadline is present is compared. A transport carries the context intact (TRN-2) when the context an action receives is equal, in this sense, to the context its caller derived for the call.

A connector retries only an idempotent action, only on `unavailable` and only within the call's one deadline. It never retries or falls back on `timeout`, whose outcome is unknown (TRN-8).

### The calling app

An app's declaration names the contract versions it requires, and, for each contract version it provides, the allow-list of calling apps that may assert a principal on a call to it ([05](05-app-declaration.md), APP-5). The caller checks the first. The provider's host checks that the call has a calling app, then the second ([ADR 0017](../docs/adr/0017-call-context-explicit-at-the-action-ambient-for-propagation.md) Decision 6, [ADR 0021](../docs/adr/0021-an-app-declares-itself-on-its-neolithappconfig.md) Decision 9).

**TRN-39.** A call whose parent scope belongs to an app whose declaration does not require the called contract version MUST NOT be sent. It is a mistake in the calling code (CON-29), in every mode. An app with no declaration requires nothing. A scope that belongs to no app has nothing to check here.

**TRN-40.** The caller MUST make the check of TRN-39 once it has found the parent scope and before it resolves the call's principal. A call that fails it resolves no principal and does not count as made (TRN-32).

**TRN-41.** An implementation SHOULD offer a check, runnable before deployment, that reports each place in an app's code that asks for a connector to a contract version the app does not require.

**TRN-42.** A provider's host MUST fail a call that has no calling app with `unauthenticated`. In-process the calling app is not authenticated but known: it is the app the call's parent scope belongs to (TRN-31), so a call made from a scope of no app has none.

**TRN-43.** A provider's host MUST fail a call that carries a principal, from a calling app not on the allow-list of the called contract version, with `permission_denied`, whatever the principal holds.

A call with no principal asserts none, so the allow-list does not apply to it; whether the action admits a call with no principal is the action's own check. TRN-42 and TRN-43 are the provider's first two steps of ADR 0017 Decision 9: a call they refuse is refused before its deadline is checked (TRN-19) or its request read, so its action never runs and the transport's check after the action does not apply. A caller whose own deadline has passed when the refusal comes back gets `timeout` instead (TRN-38).

## Other transports

A transport not defined here (gRPC, a message queue) is conformant when it satisfies the transport interface and passes the conformance tests ([06](06-conformance.md)). Its schema projection of contracts is its own concern.
