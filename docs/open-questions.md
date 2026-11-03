# Open questions

Things that need research or a decision before code depends on them. When one is settled, it becomes a decision record and is removed from here.

1. **Which shared capabilities deserve a standard contract?**
   Identity, notifications, workflow, configuration and analytics were named in early drafts, but the list was taken from reference material, not derived. Needs research into what tried and tested platforms (ABP, Encore, Spring Modulith, Dapr building blocks) standardise, and which of those are worth a neolith contract versus leaving to each project. Related: [ADR 0005](adr/0005-small-core-optional-hub-apps.md).

2. **Runtime mode switching.**
   Mode (attached or detached) is applied at deploy time for now. Operational settings such as log level should be changeable at runtime from the start. Whether and how mode itself can change at runtime, without a redeploy, is open: it needs a configuration source, connection draining and a story for in-flight calls. Related: [ADR 0016](adr/0016-an-app-may-run-attached-and-detached-at-once.md), which gives the connector multiple providers and may be the mechanism.

3. **Terminology, final check.**
   The docs use *contract* (what an app exposes), *connector* (how a caller reaches a contract) and *transport* (how a connector carries a call). Hexagonal *port* and *adapter* are mentioned once as equivalents. Confirm the three terms hold up once the spec is drafted, and that nothing else (for example *interface*, *client*, *binding*) creeps in.
