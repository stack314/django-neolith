# 0014. Connector transports are pluggable

- **Status:** Proposed
- **Date:** 2026-11-07

## Context

The design describes two ways to reach a contract: a local call when attached, and REST when detached. REST is a reasonable default, but it is not the only fit. Some deployments want gRPC for efficient service-to-service calls. Others want calls to travel over a message queue. Wiring HTTP into the connector would make every other protocol a fork.

Seen this way, attached mode is not special. It is one more way of carrying a call: in-process.

## Options

1. **HTTP only.** One remote path, so it is simple to build and test. Any other protocol needs changes to the core.
2. **A pluggable transport (strategy pattern).** A connector delegates each call to a transport chosen in configuration. In-process and HTTP ship with the core. Others are plugins.
3. **gRPC as the default remote transport.** Efficient and strongly typed, but less familiar to Django teams, and harder to call from a browser or with curl.

## Decision

Option 2.

- A connector delegates to a **transport**, chosen per provider in configuration. The calling code does not change when the transport does.
- The core ships two transports. **In-process** is attached mode, and serializes per [0007](0007-local-connector-serializes.md). **HTTP/JSON** is the default for detached apps, described by OpenAPI.
- Other transports, such as gRPC or a message queue, can be added as separate packages behind the same interface.
- Contracts stay transport-neutral. They are defined once in Python types, and each transport projects them into its own schema: OpenAPI for HTTP, protobuf for gRPC.
- Every transport must pass the same contract test suite. This extends principle 8 (*one test suite over two code paths*) from two modes to any number of transports.

GraphQL is not planned as a transport. Contracts are named actions, while GraphQL is a query language over a data graph. A GraphQL API over several apps' contracts fits better as a gateway in front of them than as a way for apps to call each other.

## Consequences

- A connector may hold more than one provider for a contract, each over its own transport (see [0016](0016-an-app-may-run-attached-and-detached-at-once.md)).
- v0.1 ships only the in-process and HTTP transports. The transport interface is the only extra cost now.
- Contract types must stay within a portable subset that both JSON Schema and protobuf can express (no arbitrary Python objects).
- Typed errors ([0008](0008-typed-contract-errors-in-both-modes.md)) must map onto every transport's error model.
- [0006](0006-contracts-are-typed-and-published-as-openapi.md) still holds. OpenAPI becomes the projection for the HTTP transport, not the only one.
