# 0010. Pluggable principal resolution in core

- **Status:** Accepted
- **Accepted:** 2026-11-11
- **Date:** 2026-11-03

## Context

A detached app has no shared user table, so `request.user` needs another source. Some projects will have an identity app or an external identity provider (OIDC, tokens, permission checks). Principle 12 (*small core over broad features*) keeps such capabilities out of the core: they are ordinary apps behind contracts.

A call between apps involves two parties. The calling app sends the call and can be authenticated from what the transport carries. The principal is the party on whose behalf the call is made, often an end user, and the calling app vouches for it. OAuth 2.0 Token Exchange ([RFC 8693](https://www.rfc-editor.org/rfc/rfc8693)) calls these the actor and the subject. Service meshes such as Istio authenticate the workload with mutual TLS and carry the end user in a separate token.

A principal crosses hosts, so it has to mean the same thing on each of them. A model instance, or a primary key on its own, means something only in one database ([0004](0004-each-app-owns-its-data.md)).

## Options

1. **Core depends on a specific identity app.** Simple, but it makes that app mandatory and blocks v0.1 on building one.
2. **Core defines a resolution seam.** Pluggable backends, tried in order, turn the credentials of an incoming call into the calling app and, optionally, a principal. Each action decides what they may do.
3. **The same seam with a permission hook**, an `is_allowed()` that the host calls before each action. Authorization sits in one place, but the core then defines a permission model, which principle 12 leaves to apps, and the hook decides without the request the action receives.

## Decision

Option 2.

1. **Two identities.** The seam resolves the calling app, authenticated from the transport's credentials, and separately an optional principal on whose behalf the calling app calls. RFC 8693 separates the actor from the subject in the same way (sections 1.1 and 4.1), and Istio separates a peer's `principals`, from mutual TLS, from `requestPrincipals`, from an end-user token. Spec 03 fixes how each crosses HTTP.
2. **A principal is a portable value.** It is frozen and made of portable types: an issuer and a subject, both strings and stable across hosts, plus optional claims. It is never a model instance, in any mode, and a primary key on its own is never a subject ([0004](0004-each-app-owns-its-data.md)).
3. **The principal travels apart from the credential.** On a detached call, `Authorization` carries only the calling app's credential, and the principal travels in a separate header as an assertion by the calling app, as [0017](0017-call-context-explicit-at-the-action-ambient-for-propagation.md) Decision 6 proposes. The end user never goes inside the shared-secret token. A provider accepts an asserted principal only from calling apps on its allow-list of apps that may assert one, and refuses it from any other calling app with `permission_denied`. A later backend can replace the bare assertion with a signed delegation token in the style of RFC 8693, in the same header, without changing `CallContext`.
4. **No permission hook.** Actions authorize themselves and refuse with `permission_denied` (principle 12).
5. **Shared-secret tokens, for development only.** v0.1 ships one backend. Each calling app has its own secret, named by a key id. A token identifies the calling app and nothing else: the issuing app, a single audience, a short expiry, the key id and an explicit type, `neolith+jwt`, so that it cannot be taken for a JWT of another kind (RFC 8725, section 3.11). SPIFFE JWT-SVIDs also carry an audience and a short lifetime, and also set `typ` (section 2.3), though only to the generic `JWT` or `JOSE`, and they are signed with keys only the issuer holds. Spec 03 fixes the format, so that implementations interoperate.
6. **Backends combine in order.** A host tries its backends in a configured order. The first that recognizes a call's credentials decides: it resolves them, or it refuses the call. A backend that does not recognize them abstains. Django's `authenticate()` also moves on when a backend returns nothing, but a backend there cannot tell "not mine" from "wrong", so a later backend can still grant what an earlier one rejected; only `PermissionDenied` stops the loop. Spring Security's `ProviderManager` skips providers that do not support a credential type, and also tries the next provider after one fails. Here the backend that recognizes the credentials has the final word, so a weaker backend later in the list cannot grant what it refused.
7. **Fail closed.** A call over a network transport whose credentials are missing, are recognized by no backend, or do not resolve, or that reaches a host with no backend, is refused with `unauthenticated` ([0008](0008-typed-contract-errors-in-both-modes.md)). In-process, the host knows the calling app without credentials.
8. **Other backends.** An identity app or an external provider (OIDC, user tokens) plugs into the seam as a backend. In attached mode, the Django user is resolved through the same seam, into the same portable value.

Follows principles 12 (*small core over broad features*) and 13 (*integrate over own*).

## Consequences

- v0.1 can demo detached calls without building identity first.
- Adopters can plug in their existing auth, such as an OIDC provider or an internal token service.
- The shared-secret backend is for development and getting started, not a production recommendation. The docs must say so. A secret is held by the calling app and by every provider it calls, and any holder can act as that app. A token is a bearer token, so anyone who sees one can act as that app for as long as providers accept it, and assert any principal wherever that app is on the allow-list. An implementation enables this backend only when configured to (CONTRIBUTING K9).
- The audience is the contract being called, which both sides know without extra configuration. It keeps a token from being used at another contract, but not from being replayed at another provider of the same contract, such as the attached and detached providers of one app. Naming the provider would need more configuration on every caller and would still leave the shared secret, which every provider that checks a calling app's tokens holds.
- The only credential every implementation must support is therefore one for development. Interoperable authentication for production needs a later record ([open questions](../open-questions.md)).
- Authorization is code in each action. A policy shared by several actions is a function or library the app chooses.
- A principal compares equal on every host, which [0017](0017-call-context-explicit-at-the-action-ambient-for-propagation.md) relies on. A project that resolves Django users in attached mode needs an identifier for each user that is stable across hosts, which the primary key alone is not.
- The allow-list of calling apps that may assert a principal belongs in the provider's declaration (spec 05), with the app host in roadmap step 4 (#11). [0017](0017-call-context-explicit-at-the-action-ambient-for-propagation.md) Decision 6 proposes one list per provided contract.
- Backends declare which inputs of a request they read to resolve a principal, such as a header or a cookie, so that the host can vary responses on them. [0017](0017-call-context-explicit-at-the-action-ambient-for-propagation.md) needs this declaration.
- Shared-secret tokens expire, so this backend needs the hosts' clocks to agree within a small margin. Spec 03 allows 60 seconds each way, so a token is accepted for at most 420 seconds.

## References

- [RFC 8693](https://www.rfc-editor.org/rfc/rfc8693), OAuth 2.0 Token Exchange: subject and actor (sections 1.1 and 4.1).
- [Istio authorization policy, `Source`](https://istio.io/latest/docs/reference/config/security/authorization-policy/#Source): `principals` from the peer's mutual TLS identity, `requestPrincipals` as `iss/sub` from an end-user token.
- [SPIFFE JWT-SVID](https://github.com/spiffe/spiffe/blob/main/standards/JWT-SVID.md): workload tokens with an audience and a short lifetime; section 2.3 on `typ`.
- Django [`authenticate()`](https://github.com/django/django/blob/stable/6.1.x/django/contrib/auth/__init__.py) and Spring Security [`ProviderManager`](https://github.com/spring-projects/spring-security/blob/main/core/src/main/java/org/springframework/security/authentication/ProviderManager.java): how each combines several authentication backends.
- [RFC 7519](https://www.rfc-editor.org/rfc/rfc7519) (JSON Web Token) and [RFC 8725](https://www.rfc-editor.org/rfc/rfc8725) (JSON Web Token Best Current Practices).
