# 00. Overview and terms

## Terms

These terms are used with exactly these meanings throughout the specification and the reference implementation. No synonyms are used.

- **App.** A unit of code that owns its data and exposes zero or more contracts. The app is the unit of deployment.
- **Contract.** A named, versioned set of actions and the types they use. A contract is the only surface through which one app reaches another. Identity: `<name>.v<N>`, for example `inventory.v1`.
- **Action.** A named operation on a contract, with a typed request, a typed reply and a declared set of errors.
- **Contract package.** The distributable form of a contract: its interface and types, with no implementation. **OVR-1.** A caller MAY depend on a contract package natively. **OVR-2.** A caller MUST NOT depend on anything else of the providing app.
- **Provider.** A running implementation of a contract that can be reached over a transport.
- **Connector.** The object through which a caller invokes a contract. A connector holds one or more providers and a routing policy, and delegates each call to a transport.
- **Transport.** The mechanism that carries one call from a connector to a provider and the reply back. In-process and HTTP are defined by this specification. **OVR-3.** Others MAY exist.
- **Event.** A named, versioned fact that an app publishes without knowing who consumes it. Identity: `<name>.v<N>`, for example `order.placed.v1`.
- **Declaration.** What an app states about itself: the contracts it provides, the contracts it requires, the events it publishes and subscribes to, and the resources it needs.
- **App host** (*host* for short). The runtime that boots one or more apps from their declarations and serves their contracts.
- **Mode.** Whether an app runs *attached* or *detached* in a given deployment. Mode is a property of the deployment, never of the app's code.

Words this specification does not use as names for these concepts: *interface*, *client*, *adapter*, *port*, *binding*, *service*. (Ordinary uses, such as "the transport interface", are not affected.) Where a natural-language sentence needs "service", it means "an app deployed detached".

## The two modes

An app is **attached** when it runs in the same host as its caller. The connector reaches it over the in-process transport.

An app is **detached** when it runs in its own host. The connector reaches it over a network transport such as HTTP.

**OVR-4.** The same app MAY run attached and detached at the same time. The connector then holds several providers for the contract and routes between them ([02](02-transports.md)).

Mode is applied when a deployment is configured. **OVR-5.** Changing mode MUST NOT require any change to the app or to its callers.

## The call model

A call is the invocation of one action on one contract.

1. The caller obtains a connector for a contract by its identity.
2. The caller invokes an action with a request value of the action's request type.
3. The connector chooses a provider by its routing policy and hands the call to that provider's transport.
4. The transport carries the request, together with the **call context**, to the provider and returns either a reply of the action's reply type or an error.

The call context travels with every call and is separate from the request. **OVR-6.** Transports MUST carry the context. **OVR-7.** Apps MUST NOT smuggle it inside request types. It has two kinds of field:

- **Propagated fields**, which a call passes on to the calls it makes: the principal on whose behalf the call is made, the trace identifier with its W3C `tracestate`, and the deadline. The caller checks a call's principal before sending, in every mode, in the order of [ADR 0017](../docs/adr/0017-call-context-explicit-at-the-action-ambient-for-propagation.md) Decision 9, and a provider receives the principal decoded from its sent form ([03](03-http-transport.md)). A nested call's deadline, as its caller derives it ([02](02-transports.md), TRN-27), is never later than its parent's. The provider counts its own from the budget when the call arrives (TRN-36), so that one can be later, and the caller's clock decides (TRN-38).
- **Per-call fields**, which belong to one outbound call: the idempotency key and the calling app. Each outbound call gets a fresh key. The key stays the same only across retries of that same call and, when the parent has a key of its own, across retries of the parent that make the call again ([02](02-transports.md)), so a provider never mistakes two different nested calls for a repeat, unless a caller replays its parent's key.

Every call is synchronous from the caller's point of view: it ends in a reply or an error. Asynchronous communication is done with events ([04](04-events.md)).

## Invariants

These hold in every mode and over every transport.

1. **Same semantics.** A call has the same meaning, the same reply type and the same error set over every transport. Only latency and failure probability differ.
2. **Serialized boundary.** The request and reply cross the contract boundary as values of the portable type system ([01](01-contracts.md)), even in-process. No live object, reference or handle crosses.
3. **Visible failure.** Any call can fail with a transport error. The caller's code cannot assume otherwise, and the contract's error set always includes the standard errors.
4. **Owned data.** An app reads and writes only its own data. It holds other apps' identifiers, not references into their data.
5. **Typed context.** Transports carry the call context and never fabricate it. Propagated fields pass from a call to the calls it makes; per-call fields do not.
