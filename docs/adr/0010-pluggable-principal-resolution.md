# 0010. Pluggable principal resolution in core

- **Status:** Proposed
- **Date:** 2026-11-03

## Context

A detached app has no shared user table, so `request.user` needs another source. Some projects will have an identity app or an external identity provider (OIDC, tokens, permission checks), but [0005](0005-small-required-core.md) says the core depends on no particular app.

## Options

1. **Core depends on a specific identity app.** Simple, but it makes that app mandatory and blocks v0.1 on building one.
2. **Core defines a principal-resolution seam.** Pluggable backends turn an incoming request or call into a principal (user or service), with an `is_allowed()` hook.

## Decision

Option 2. The core defines a small interface for resolving principals and checking permissions:

- v0.1 ships a minimal backend: signed service tokens with a shared secret, for service-to-service calls between detached apps.
- An identity app or an external provider plugs into this seam as a backend (OIDC, user tokens, roles).
- In attached mode, the existing Django user can be resolved through the same interface.

Follows principles 12 (*small core*) and 13 (*integrate over own*).

## Consequences

- v0.1 can demo detached calls without building identity first.
- Adopters can plug in their existing auth (an OIDC provider, an internal token service).
- The shared-secret backend is for development and getting started, not a production recommendation. The docs must say so.
