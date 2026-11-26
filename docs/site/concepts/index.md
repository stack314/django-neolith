# Concepts

This page introduces the ideas Neolith is built on. Spec 00 defines each term.

## Apps and contracts

An **app** is a Django app that owns its data. It is the unit of deployment (ADR 0002): a deployment decides where each app runs, and the app's code does not change.

Apps reach each other only through **contracts** (ADR 0003). A contract is a named, versioned set of **actions**, such as `inventory.v1`. Each action takes a typed request and returns a typed reply or one of its declared errors (spec 01). Requests and replies are Pydantic models restricted to the portable forms, so every value has one wire form in every language (ADR 0019).

An app that implements a contract is its **provider**. An app that calls it holds a **connector**, which it gets from `connect()` with the contract's identity.

## Modes

Where an app runs is its **mode**, and the mode belongs to the deployment, never to the app's code (spec 00, [OVR-5](../spec/00-overview.md#ovr-5)):

- An app is **attached** when it runs in the same host as its caller. Its connector reaches it over the in-process transport, which still serializes each request and reply, so a call means the same in both modes (ADR 0007).
- An app is **detached** when it runs in its own host, and its connector reaches it over a network transport.

The HTTP transport carries calls to a detached app (spec 03).

## The call context

Every call carries a **call context** beside its request: the principal on whose behalf the call is made, the trace, the deadline, the idempotency key and the calling app. An action reads it as an explicit parameter, and the calls it makes pass it on without the app's code handling it (ADR 0017).

## Errors

A failure that crosses a contract is a `ContractError` with a kind, in every mode (ADR 0008). A mistake in a declaration is found when the module is imported or when the host starts, and names its cause: a `ContractDefinitionError` for a contract, a `DeclarationError` for an app.
