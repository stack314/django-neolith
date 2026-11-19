# Open questions

Things that need research or a decision before code depends on them. When one is settled, it becomes a decision record and is removed from here.

1. **Which shared capabilities deserve a standard contract?**
   Identity, notifications, workflow, configuration and analytics were named in early drafts, but the list was taken from reference material, not derived. Needs research into what tried and tested platforms (ABP, Encore, Spring Modulith, Dapr building blocks) standardise, and which of those are worth a neolith contract versus leaving to each project. Related: [ADR 0005](adr/0005-small-required-core.md).

2. **Runtime mode switching.**
   Mode (attached or detached) is applied at deploy time for now. Operational settings such as log level should be changeable at runtime from the start. Whether and how mode itself can change at runtime, without a redeploy, is open: it needs a configuration source, connection draining and a story for in-flight calls. Related: [ADR 0031](adr/0031-a-contract-version-is-routed-by-single-unless-local-first-is-named.md), whose open question 4 asks for an in-process attempt that can end with `unavailable`, so that `local-first` could try an app of the host first and its remote providers after it.

3. **Terminology, final check.**
   The docs use *contract* (what an app exposes), *connector* (how a caller reaches a contract) and *transport* (how a connector carries a call). Hexagonal *port* and *adapter* are mentioned once as equivalents. The vocabulary is now fixed in [spec 00](../spec/00-overview.md), which also lists the words not used. Stays open until the 0.1 spec documents are complete, as a reminder to check that nothing else creeps in.

4. **How Django's contrib apps are hosted per app.**
   [ADR 0004](adr/0004-each-app-owns-its-data.md) counts `auth`, `admin`, `contenttypes` and `sessions` as apps, so no app holds foreign keys into them. Open: whether each host runs its own copy of the contrib apps it declares, how admin login works across hosts, and whether `GenericForeignKey` to an app's own models can be kept safe across a detach (ContentType IDs differ per database).

5. **The outbox and event delivery guarantees.**
   [ADR 0004](adr/0004-each-app-owns-its-data.md) relies on events for cross-app consistency, and [ADR 0016](adr/0016-an-app-may-run-attached-and-detached-at-once.md) relies on the outbox to de-duplicate. Neither is recorded as a decision yet: outbox table shape, at-least-once delivery, ordering, subscriber idempotency, and the in-process versus broker paths. Needs its own ADR before events are built.

6. **A host-side idempotency record.**
   Spec 01 makes idempotency the action author's promise, so a retried parent still runs a non-idempotent child twice. Open: whether the provider host keeps a record of completed calls by idempotency key, which actions it covers, what it stores (key, request hash, reply or error), how long it keeps entries, a distinct error for a key reused with a different request, and that keys are scoped per caller and principal, so a derived key from one call chain cannot be replayed by another caller. Raised by [ADR 0017](adr/0017-call-context-explicit-at-the-action-ambient-for-propagation.md); Stripe's idempotent requests are the model. Needs its own ADR.

7. **Authentication for production interop.**
   The only credential every implementation must support is the shared-secret token of [spec 03](../spec/03-http-transport.md), which is for development only ([ADR 0010](adr/0010-pluggable-principal-resolution.md)). Two implementations that interoperate in production need a common way to authenticate the calling app and to carry the principal with proof, not just as an assertion. Candidates include mutual TLS with workload identities (SPIFFE), OAuth 2.0 client credentials, and signed delegation tokens ([RFC 8693](https://www.rfc-editor.org/rfc/rfc8693)). Needs its own ADR. Related: [ADR 0017](adr/0017-call-context-explicit-at-the-action-ambient-for-propagation.md), which separates the calling app from the principal.
