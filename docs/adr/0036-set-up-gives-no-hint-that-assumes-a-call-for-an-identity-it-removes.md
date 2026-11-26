# 0036. Set-up gives no hint that assumes a call for an identity Resolve tells the deployer to remove

- **Status:** Accepted
- **Accepted:** 2026-11-26
- **Date:** 2026-11-26
- **Narrows:** [0031](0031-a-contract-version-is-routed-by-single-unless-local-first-is-named.md) (two refusal messages of the builder's rows and the sentence that introduces them, Decision 1 here; the set-up sentence on 0027 Decision 9's refusal, Decision 2 here) and [0027](0027-a-caller-backend-names-the-calling-app-over-http.md) (the set-up refusal of Decision 9, Decision 2 here). 0031 gains `Narrowed by 0036 (Decisions 1 and 2)` under its status and 0027 gains `Narrowed by 0036 (Decision 2)` (D2). Nothing else in either changes.

## Context

When Resolve refuses the host, set-up reports `NEOLITH`'s own problems and each hosted app with no signing key together with Resolve's refusals, so that one refusal hides none behind it. For one identity, those lines can give opposite instructions (#155, #176):

- `NEOLITH["REMOTE_PROVIDERS"]` lists two entries for `couriers.v3`, which an app of the host provides. Resolve says `... would never be called; remove the entry`, and 0031's `REMOTE_PROVIDERS` row says `... to try them in order, name local-first for couriers.v3 in NEOLITH["ROUTING_POLICIES"]`. Following the second is refused by Resolve in turn.
- `NEOLITH["ROUTING_POLICIES"]` names `fast` for an identity no hosted app requires. Resolve says `... remove it`, and 0031's value row says `the one policy to name is local-first`.
- An app requires `couriers.v3`, which another app of the host provides, and `REMOTE_PROVIDERS` names it. Resolve says to remove the entry, and 0027 Decision 9's set-up refusal says the app `cannot call couriers.v3 over HTTP` for want of a signing key, a call that never happens.

Resolve refuses two kinds of identity whatever their value: one that no hosted app requires, and one that an app of the host provides (0031, the Resolve rows). Each refusal ends with "remove it" or "remove the entry". A third Resolve refusal, `local-first` with exactly one entry, depends on the value and says to remove the policy, not the identity; it is not one of them.

## Options

1. **Leave the messages as they are.** Every line is true of the settings it reads, but the deployer gets two instructions and one of them leads to another refusal.
2. **For an identity Resolve tells the deployer to remove, leave out every hint that assumes the identity will be called.** The identity is still refused, with Resolve's message; only the advice that contradicts it goes. The builder's rows then read the declarations for these two messages, as Resolve does.
3. **Drop the settings' problems whenever Resolve refuses.** Simpler, but a deployer would then fix the settings one refusal at a time, where set-up today reports every problem together.

## Decision

Option 2. An identity Resolve "removes" is one that `NEOLITH["REMOTE_PROVIDERS"]` or `NEOLITH["ROUTING_POLICIES"]` names and that no hosted app requires or an app of the host provides.

### 1. The builder's rows give no `local-first` advice for an identity Resolve removes

0031 says: "**The builder's rows** (0022 Decision 1; these read the settings only)". This record narrows "these read the settings only" to: these read the settings, and, for the two messages below, which identities Resolve removes.

0031's value row refuses, "for any other `str`: `NEOLITH["ROUTING_POLICIES"]["<id>"]: is not a routing policy of this host; the one policy to name is local-first, and leaving <id> out means single`". For an identity Resolve removes, the message is `NEOLITH["ROUTING_POLICIES"]["<id>"]: is not a routing policy of this host`, and Resolve's refusal says to remove it.

0031's `REMOTE_PROVIDERS` row admits two or more entries "only when `ROUTING_POLICIES` names `local-first` for the identity", and refuses: "For two or more entries without `local-first`: `NEOLITH["REMOTE_PROVIDERS"]["<id>"]: lists <n> providers; with the single routing policy a contract has one provider; to try them in order, name local-first for <id> in NEOLITH["ROUTING_POLICIES"]`". For an identity Resolve removes, this row adds no message; Resolve's refusal, which says to remove the entry, reports it.

Every other message of both rows, and every message for an identity Resolve does not remove, is unchanged.

### 2. No app is refused for a signing key to call an identity Resolve removes

0027 Decision 9 says: "The second case is also refused before Serve, where the host can see it (K2): for each contract identity in the remote provider configuration, every hosted app that requires it needs a `SIGNING_SECRETS` entry, and the refusal names the app and the identity." 0031 says: "0027 Decision 9's refusal of a hosted app that requires a remote identity and has no signing key is unchanged; it covers every identity in `REMOTE_PROVIDERS`, whatever its policy."

This record narrows both to: every identity in `REMOTE_PROVIDERS`, whatever its policy, except one Resolve removes. That identity is never called over HTTP, so no app needs a key to call it. The refusal at the call, Decision 9's second bullet, is unchanged.

## Consequences

- One identity gets one instruction: remove it. A deployer who follows it gets no further refusal for that identity.
- `neolith.E021` as a system check is unchanged in effect: it runs once set-up has passed, when Resolve removes no identity.
- A test generates hosts, `REMOTE_PROVIDERS` lists of one to three entries and policies, and checks that each `local-first` hint and each missing-key line appears exactly where Resolve does not remove the identity. A set-up test reproduces #176.
- No sentence of the specification states these messages, and no requirement ID changes. APP-64 still holds: the host stops before it serves.
