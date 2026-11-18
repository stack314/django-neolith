# 0031. A contract version is routed by `single` unless `NEOLITH["ROUTING_POLICIES"]` names `local-first`, which tries its listed providers in order and moves an idempotent call on `unavailable`

- **Status:** Proposed
- **Date:** 2026-11-19
- **Replaces:** [0016](0016-an-app-may-run-attached-and-detached-at-once.md) (Proposed). When this record is accepted, 0016's status becomes `Superseded by 0031`, with a link back. The template lists `Superseded by NNNN` among a record's statuses, and `docs/adr/README.md` states the rule for a record that a later one reverses; this record reads that rule as covering a Proposed record too, because it keeps 0016's goal and replaces its form, which `Rejected` would not say. This record keeps 0016's three constraints as written, idempotent actions "so that a fallback after a failed call is safe" among them. It keeps 0016's goal, an app running attached and detached at once, at the level of the deployment (Decision 3), not within one host: 0016's Option 2 has "the connector for a contract" know "every available provider (in-process and remote)", and in 0.1 a connector never holds an in-process provider beside remote ones. It narrows 0016's four policies to two, and gives the settings form and the tests that 0016 left open. [0002](0002-django-app-is-the-unit-of-deployment.md) (Accepted) links "both modes at once" to 0016; that link stays, and leads from 0016 to this record.
- **Narrows, once accepted:** [0014](0014-pluggable-connector-transports.md) (its admitted set's `providers` line and the value row's admitted cell; three refusal messages: the value row's third, the Resolve row's for an identity provided in the host, and the entry row's first, whose `[0]` every entry-level message carries; `neolith.W002`'s message; three sentences of Decision 2 and two of its alternatives; and one sentence of its Consequences; Decision 2 here) and [0028](0028-the-http-transport-uses-the-standard-library-only.md) (the connect phase's bound and its message, the replacement connection's bound, and the `timeout` a call ends with after a move; Decision 5 here). Each gains `Narrowed by 0031 (Decision k)` under its status (D2), with the Decision it names above.
- **Alongside:** [0032](0032-the-conformance-suite-runs-each-case-once-per-transport.md) (Proposed), the conformance suite, which says how the failover cases of Decision 9 are driven, named and reported, and is the "conformance record" below.
- **Leaves unchanged:** 0017 Decision 9's closing paragraph; 0028 Decision 5's paragraph on step 6 ("When step 6 brings TRN-8, the retry is the connector's, decided by the action's `idempotent` flag, on `unavailable`, within the one deadline, the same over every transport"); spec 01's CON-4 and its kinds table; spec 00's OVR-4, which holds at the level of the deployment: the connector of a host that calls the app holds both instances as providers and routes between them under `local-first` (Decision 3); and spec 02's TRN-8, TRN-17, TRN-65 and TRN-66 (step 5's task 5: it speaks only of `single`, which this record leaves as it is). This record builds what those sentences already say. TRN-28, TRN-38, TRN-64 and spec 03's HTTP-17 keep their words; where a call that has moved departs from them, TRN-new-r5 and TRN-new-r7 say so in the form spec 02 already uses ("This comes before ..."). Step 5's TRN-67 is withdrawn and restated for every policy (Decision 10).

Requirement IDs below are named `TRN-new-r1` and on, and `APP-new-r1` and on, until that pull request numbers them (P5).

## Context

Roadmap step 6 (#13) delivers `local-first` routing and a failover test with two providers, on top of step 5 (#12). Step 5 ships one routing policy, `single`: each contract version has one provider, the one in the host when an app there provides it, otherwise the one remote provider configured under `NEOLITH["REMOTE_PROVIDERS"]` (0014 Decision 2). Several records left their routing questions to this step:

- **0016** (Proposed) makes "both modes at once" a goal: "The connector may have more than one provider for a contract and chooses among them by a configured policy: local first with remote fallback, remote first, weighted, or explicit." Its constraints are one owner of the data, idempotent actions "so that a fallback after a failed call is safe", and the same serialization everywhere. Its consequences ask for a failover case in the contract suite.
- **0014** (Accepted with #128) admits a list of exactly one entry per identity, so that "Admitting more entries later widens the row". It expected `local-first` to pair the in-host provider with a remote one: "Step 6's `local-first` needs only one remote provider beside the in-host one" (Decision 2, alternatives), and "Choosing per call, with the in-host provider preferred when it exists. That is `local-first`, which spec 02 defines with a fallback rule that step 6 builds and tests (TRN-8)." Its Decision 7 leaves to step 6 `local-first` ("step 6's record gives the policy its form"), "an identity provided in the host and configured remotely at once, which the table refuses with a message that names step 6", more than one provider per identity, retry and fallback ("TRN-8 limits them to idempotent actions, `unavailable` and the call's deadline"), and the failover test. Its open question 3 asks for the form of a routing policy and whether a second entry is another provider or another replica.
- **0028** (Accepted) has the HTTP client never resend, with one replacement connection for a first write that a reused connection refused before taking a byte (Decision 5). It places step 6's retry with the connector, "decided by the action's `idempotent` flag, on `unavailable`, within the one deadline", and leaves to step 6 "Whether a try gets a timeout of its own inside the deadline". Its Consequences note that the 10 second connect cap "leaves a call with the default 30 s deadline time for a retry in step 6".
- **#12** defers to step 6 "`local-first`, failover and retries (TRN-8), more than one provider per identity, a per-try timeout, and the failover test".
- **The roadmap** promises, in step 8, example apps "showing attached, detached and both-at-once".

The specification already says:

- Spec 02, *Providers and routing*: the connector holds "an ordered set of providers"; five policies are listed under "Defined policies:", `single` and `local-first` "required in 0.1" and the other three with no requirement, `local-first` as "prefer an in-process provider, and fall back to the others under the fallback rule below". TRN-8: "A retry or a fallback after a failed call MUST happen only for an idempotent action, only on `unavailable`, and only within the call's deadline." TRN-7, which also admitted `timeout`, was withdrawn because `timeout` leaves the outcome unknown.
- Spec 02, TRN-17: "A connector with the `single` policy MUST NOT send a call more than once, or to any provider but its one." TRN-65: "A transport MUST NOT send a call again on its own; retrying is the connector's alone (TRN-8). A call counts as sent once the network has accepted any byte of its request."
- Spec 02, TRN-4, TRN-27, TRN-28, TRN-38 and TRN-64: one deadline per call; the budget is what remains when the call is sent; under 1 ms the call fails with `timeout` unsent; over a network the caller stops waiting at its deadline in every phase. TRN-38 adds: "A `timeout` that a transport or a provider's host produced is passed on as it is." TRN-64 adds: "A call whose deadline passes before the caller has a connection ready and the call's budget read fails as one not sent (TRN-28); from then on, as one whose action might have run." Spec 03's HTTP-17: "A budget under 1 ms then fails the call with `timeout`, unsent (TRN-28)." Each speaks of a call that has one attempt.
- Spec 02, TRN-67 (step 5's task 5, #135): "A host MUST stop before it serves when its configuration names a remote provider for a contract version that none of its apps requires, or, under the `single` policy, for one that one of its apps provides." 0014's Consequences fix those words.
- Spec 01, CON-4: "An action is **non-idempotent** unless it is declared idempotent. Connectors MUST NOT retry or fall back on a non-idempotent action. Forgetting the declaration therefore costs a retry, never a duplicated effect." CON-5 makes an idempotent action the author's promise for repeated calls with the same idempotency key. The kinds table marks `unavailable` "retry safe: yes, if idempotent", and `unauthenticated` "no".
- Spec 06, item 4: "`single` and `local-first` policies. Failover happens on `unavailable` for idempotent actions only, within the call's deadline, and never on `timeout` or for non-idempotent ones."
- 0017 Decision 9 (Accepted) closes with: "A connector retries only an idempotent action, only on `unavailable`, and only within the call's one deadline, as in gRPC's retry design (A6). It never retries or falls back on `timeout`, whose outcome is unknown."

### Facts this record rests on

Code and specification are read at `origin/release/0.1.0a3`, which holds #128's connector over HTTP, #129's provider endpoints and #135's remote providers (step 5's task 5). Django is read at 5.2.19 and 6.1.3.

1. **An in-process call never ends with `unavailable`.** The provider's host delivers an `unavailable` or `timeout` that an action raised as `internal` (CON-44; 0008 Decisions 1 and 3; `Provider._deliver` in `src/neolith/_connector.py`, which returns `None` for a kind in `_TRANSPORT_KINDS`). The in-process transport itself produces `timeout` (TRN-19, TRN-20), `unauthenticated` and `permission_denied` (`InProcess.call` through `_refusal`), and nothing else of its own. A failure of the action, a database that cannot be reached included, is `internal`.
2. **The HTTP client knows in which phase a call failed; the transport drops it.** `neolith._http.ClientError` carries `phase`, one of `"before writing"`, `"writing"` and `"reading"` (`Phase` in `src/neolith/_http.py`). Resolution, connecting, the TLS handshake (a certificate that does not verify included) and the connect cap end in `"before writing"` (`_open`, `_unreachable`). A first `send` on a reused connection that takes no byte raises `_Unsent`, and the client opens one new connection inside the same attempt (`_exchange`, `_send`). `HTTPTransport.call` turns a `ClientError` into `Failure(kind, message)` without the phase (#128).
3. **The connector holds one transport, and the call's context is fixed once.** `Connector.__init__` takes one `Transport`; `_call` runs 0017 Decision 9's steps 1 to 4, derives the idempotency key once at step 3 (`scope.child_key`), builds one `Sent`, calls `_carry` once and then discards an outcome that came back after the deadline (`src/neolith/_connector.py`, #128). Its two `timeout` texts are `_NOT_SENT`, "the deadline passed before the call was sent", and `_LATE`, "the deadline passed before the call ended; the action might have run". The host builds one transport per identity at set-up and `connect()` looks it up by identity on each call (0014 fact 3 and Decision 2).
4. **Each HTTP attempt gets its own credential, span and budget.** `HTTPTransport._fields` is called once a connection is ready, and again for the one replacement connection; it reads the budget then, mints a fresh token (`Callers.mint`) and a new span identifier (`_span`), and writes the same principal, trace identifier, `tracestate` and idempotency key from `Sent` (#128; 0027 Decision 9; 0028 Decision 2). Before it reaches the client, `HTTPTransport.call` refuses a call with no calling app, or from an app with no signing key, with `unauthenticated` (0027 Decision 9, HTTP-18), so these refusals come from the transport on each call it is given. Its `_outcome` maps a final response, and its `timeout` texts come from the client: `_NOT_SENT` for every failure before writing, the budget under 1 ms included (`exchange` and `_open` in `src/neolith/_http.py`), and `_LATE` once writing has started. The connector passes a transport's `timeout` on as it is (`Connector._call`).
5. **Connections belong to a pool per origin, reset at fork.** Transports whose `url` is the same origin share one `Pool` from `Pools`; `os.register_at_fork(after_in_child=_after_fork)` empties every pool and renews every lock in a child, and a pool built in another process starts empty (`src/neolith/_http.py`; 0028 Decision 4). An idle connection is dropped after 1 second.
6. **Connecting is bounded by the cap or the deadline, whichever comes first.** `_open` sets `until = min(now + CONNECT_CAP, deadline)`, with `CONNECT_CAP` 10 seconds for resolution, connecting and the TLS handshake together. When the deadline is the earlier, the phase ends with `timeout`; otherwise with `unavailable`. Inside the phase each resolved address gets `max(left / addresses left, 2 s)`, never more than what is left (`PER_ADDRESS`; 0028 Decision 2, after Go's `partialDeadline`), so "An address that drops packets silently then costs its share, not the whole phase".
7. **The HTTP transport logs most of its `unavailable` outcomes, not all.** `HTTPTransport.call` logs one `WARNING` for a `ClientError` of kind `unavailable`, naming the client's message, its phase and its cause; `_outcome` logs one for a non-success status without a neolith error body, naming the provider's origin and the status only. A neolith error body of kind `unavailable` reaches `raise Failure(*error)` with no line from the caller's side; the provider's host logs that response itself (#129).
8. **A draining neolith server closes a request it will not serve without a response.** In `src/neolith/_server.py`, Drain shuts every idle connection and closes the listening socket, so a new connection is refused. A request line read on a busy keep-alive connection after the mark (`Connections.begin` returning `False`), or a connection accepted in the race before the socket closed (`_Handler.setup`, `opened` returning `False`), is closed with no response (APP-40). The client has written its request by then, so it fails in the `"reading"` phase.
9. **Django's own ordered lists move on for one outcome only.** `django.contrib.auth.authenticate` tries each backend in order; a backend that returns `None` passes to the next, and one that raises `PermissionDenied` stops the loop: "This backend says to stop in our tracks" (`contrib/auth/__init__.py`, lines 128 to 134 in 6.1.3, 112 to 118 in 5.2.19). 0023's principal backends abstain or decide in the same way.
10. **Prior art separates a call that never left from one that did, and moves a sent call by a declared rule.** gRPC's retry design ([gRFC A6](https://github.com/grpc/proposal/blob/master/A6-client-retries.md#transparent-retries)) has three cases: an RPC that never left the client "can transparently retry until a success occurs, or the RPC's deadline passes"; one that reached the server library but not the app's logic is retried "once", immediately; every other failure is left to the declared policy. Transparent retries "do not count toward the limit of configured RPC attempts" nor as failures for throttling. Go's `net/http` (`shouldRetryRequest` in `src/net/http/transport.go`) retries to the same host, not to another server: on a reused connection it replays a request of which nothing was written (`nothingWrittenError`), which is the case 0028 Decision 5's replacement connection covers; it also replays a request the server never processed (an HTTP/2 `GOAWAY`), the case open question 5 names, and a replayable (idempotent) request after a write when the server closed an idle connection or the read failed, a sent request retried for its idempotency alone, which is Decision 4's rule. The closest precedents for moving a call to another endpoint are Envoy's `retry_on` (`connect-failure`, `reset-before-request`) with the `previous_hosts` predicate ([router filter](https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_filters/router_filter)), and HAProxy's default `retry-on conn-failure` with `option redispatch`. RFC 9110 section 9.2.2 says a client "SHOULD NOT automatically retry a request with a non-idempotent method unless it has some means to know that the request semantics are actually idempotent, regardless of the method, or some means to detect that the original request was never applied".
11. **Prior art moves priority traffic on remembered health, not per call.** Envoy's priority levels shift traffic to a lower level "as endpoints for the highest priority level become unhealthy" ([priority levels](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/load_balancing/priority)); HAProxy uses a `backup` server only when no active server is up by health check; Consul's service-resolver `Failover` acts on health checks and outlier detection; Envoy's outlier detection and Finagle's failure accrual eject a host after consecutive failures; gRPC's `pick_first` stays on the address that last connected. Each holds mutable state per client, a timer or a probe, and an ejection rule. Envoy's per-request equivalent of an ordered list, which needs no such state, is `retry_priority` `envoy.retry_priorities.previous_priorities` ("target priorities in subsequent retry attempts that haven't been already used") with the `previous_hosts` predicate.

## Options

1. **0016 as written.** Four policies (`local-first`, `remote-first`, `weighted`, `explicit`), fallback by TRN-8. It covers the most ground. Three of the four have no settings form, no tests and no user in 0.1, and `explicit` needs a public function type over the call context.
2. **Two policies, with a move by TRN-8 as it stands; nothing remembered between calls.** Only an idempotent action ever moves, on `unavailable`, within the deadline. It builds what spec 02, spec 06, CON-4, 0017 Decision 9 and 0028 Decision 5 already say, and changes none of them. A non-idempotent call to a provider that cannot be reached fails with `unavailable` although no provider saw it.
3. **Option 2, and a call also moves on `unavailable` whatever its action when its attempt was not sent.** It helps a non-idempotent call when the provider's name does not resolve, its port refuses, its host drops packets or its certificate fails, on a direct connection. It does not help behind a load balancer, which answers `502` or `503` after the request left, nor in the Drain race (fact 8), nor with a rebooted host that resets a pooled connection, all of which fail after sending. It changes CON-4, TRN-8, 0017 Decision 9 and 0028 Decision 5, and needs every transport to report whether an attempt was sent.
4. **Option 2 or 3, plus remembered health:** a provider that failed is skipped or tried last for a while. Calls stop paying for a provider that is down. It adds process-wide mutable state, a clock, a rule for trying the provider again, and routing that depends on history.

## Decision

Option 2, in the admitted set and Decisions 1 to 10 below. Option 3 stays open (open question 5): admitting more moves later widens what a caller sees, while narrowing after a release would break callers that rely on them.

### The admitted set

This record adds one key to 0022's table of `NEOLITH` keys and widens one row of 0014's. Anything else stops the host before Serve with `ImproperlyConfigured`, whose message names the input in 0022's form `<input>: expected <what the row admits>, got <type found>`. Every container is exactly the type named; a subclass or a tuple is refused with the row's first message. No message of this record prints a `url` or a policy value that was refused.

```
ROUTING_POLICIES = dict of 1 or more  contract-id: policy        ; a key of NEOLITH; absent means none
contract-id      = str, CON-10's rule, as connect() takes it
policy           = "local-first"                                  ; the one name written; Decision 1
                                                                   ; single is what an absent entry means

REMOTE_PROVIDERS = as 0014, except:
providers        = list of 1 or more entries, no url twice         ; 0014: exactly 1
                                                                   ; 2 or more only when ROUTING_POLICIES
                                                                   ;   names local-first for the identity
```

For example, for a host whose apps require `<contract>.v1`, provided by two remote hosts:

```python
NEOLITH = {
    "ROUTING_POLICIES": {"<contract>.v1": "local-first"},
    "REMOTE_PROVIDERS": {
        "<contract>.v1": [
            {"BACKEND": "neolith.transports.HTTPTransport", "OPTIONS": {"url": "https://<host-a>"}},
            {"BACKEND": "neolith.transports.HTTPTransport", "OPTIONS": {"url": "https://<host-b>"}},
        ],
    },
}
```

A step 5 host's settings, with one entry per identity and no `ROUTING_POLICIES`, are admitted unchanged and route the same way.

**The builder's rows** (0022 Decision 1; these read the settings only):

| Input | Admitted | Refused, with its message |
|---|---|---|
| `NEOLITH["ROUTING_POLICIES"]` | Absent, or a `dict` with one or more keys. | `NEOLITH["ROUTING_POLICIES"]: expected a dict of contract identities, got <type>`; `NEOLITH["ROUTING_POLICIES"]: is empty; leave the key out`. |
| A key | A `str` that matches `contract-id`. | `NEOLITH["ROUTING_POLICIES"]: key <repr>: expected a contract identity, such as <contract>.v1`. |
| A value | Exactly the `str` `"local-first"`, compared as written. | `NEOLITH["ROUTING_POLICIES"]["<id>"]: expected a str, got <type>`; for `"single"`: `NEOLITH["ROUTING_POLICIES"]["<id>"]: single is what leaving <id> out means; leave it out`; for any other `str`: `NEOLITH["ROUTING_POLICIES"]["<id>"]: is not a routing policy of this host; the one policy to name is local-first, and leaving <id> out means single`. |
| A `REMOTE_PROVIDERS` value (0014's row, widened) | A `list` of one or more entries; two or more only when `ROUTING_POLICIES` names `local-first` for the identity. | 0014's first two messages, unchanged. For two or more entries without `local-first`: `NEOLITH["REMOTE_PROVIDERS"]["<id>"]: lists <n> providers; with the single routing policy a contract has one provider; to try them in order, name local-first for <id> in NEOLITH["ROUTING_POLICIES"]`. |
| Two entries of one identity | Different `url` values, compared as written. Equal strings are one entry listed twice. Two spellings that reach one server, such as a name and its address, are two entries: a host cannot see that they meet, and TRN-new-r2 counts entries, not servers. | `NEOLITH["REMOTE_PROVIDERS"]["<id>"][<j>]["OPTIONS"]["url"]: is the url of entry <i> too; an entry is listed once`. |
| Each entry, its `BACKEND`, `OPTIONS` and `url` | 0014's rows, applied to every entry, with the entry's index in each path. | 0014's messages, with `[<i>]` for the entry. |

**Rows that need the declarations** (Resolve, in the builder and again at set-up, 0021 Decision 8; refusals reported together, sorted):

| Input | Admitted | Refused, with its message |
|---|---|---|
| An identity in `ROUTING_POLICIES` | Required by at least one hosted app, provided by none, with two or more `REMOTE_PROVIDERS` entries. | `NEOLITH["ROUTING_POLICIES"]: <id> is required by no hosted app; remove it`; `NEOLITH["ROUTING_POLICIES"]: <id> is provided by app <label> in this host, which is its one provider; remove it`; with exactly one entry: `NEOLITH["ROUTING_POLICIES"]: <id> has one entry in NEOLITH["REMOTE_PROVIDERS"], and local-first orders two or more; remove it, since one provider is routed by single`. With no entry, 0014's refusal of the `requires` entry ("`<id>` has no provider") reports it, and this row adds no message. |
| An identity in `REMOTE_PROVIDERS` provided in the host (0014's row; its message replaced) | Never, under any policy in 0.1 (Decision 3). | `NEOLITH["REMOTE_PROVIDERS"]: <id> is provided by app <label> in this host, which answers every call to it; an in-process call never ends with unavailable, so a remote provider listed beside it would never be called; remove the entry`. |
| A `requires` entry | 0014's row, unchanged. | 0014's message. |

**At set-up, as a system check:** `neolith.W002` (0014) runs for every entry, and its message names the entry's index: `NEOLITH["REMOTE_PROVIDERS"]["<id>"][<i>]: <url> is plain HTTP ...`. 0027 Decision 9's refusal of a hosted app that requires a remote identity and has no signing key is unchanged; it covers every identity in `REMOTE_PROVIDERS`, whatever its policy.

**At Boot:** nothing for routing (0014 Decision 5). No provider is reached before Serve.

**Under `override_settings`** (0022 Decision 3): `ROUTING_POLICIES` cannot change after set-up, with 0022's message `NEOLITH["ROUTING_POLICIES"]: cannot change after set-up`. Decision 9 says how a test routes instead.

**What a call admits:** nothing new. A connector call takes the same request and options as in step 5; routing has no per-call input (Decision 1's alternatives).

### 1. Two policies exist in 0.1: `single`, which absence means, and `local-first`, which is named

`single` is the policy of every identity that `ROUTING_POLICIES` does not name. It is step 5's policy, unchanged: one provider, one attempt, at most one send (TRN-17). `local-first` is the one name a deployer writes. It orders two or more providers and moves a call along them under Decision 4's rule.

The names are spec 02's, the roadmap's, spec 06's and #13's. `remote-first`, `weighted` and a policy chosen by the call context (0016's `explicit`) are not required in spec 0.1: spec 02 lists them today among its "Defined policies", without a requirement and without "required in 0.1", and Decision 10's rewrite moves them to "a later version may define". The host refuses them with the value row's message, which does not print the value.

`"single"` is not admitted as a value, so that one behaviour has one spelling, as 0022 has an empty key left out and 0014 has the absence of a policy mean `single`.

Alternatives weighed:

- **Keep 0016's four policies.** Each named policy would need a settings form, a conformance item and tests. `weighted` needs weights and a random source the tests must seed; `explicit` makes a function over the call context public API, which 0017 Decision 7 keeps out of 0.1 (no public accessor of the context). The roadmap defers "routing policies beyond `single` and `local-first`" past 0.1; adding one later is a widening of the value row.
- **Rename `local-first` to `ordered` or `failover`.** In 0.1 an identity under `local-first` never has a provider in the host (Decision 3), so `ordered` reads more plainly in a settings file. The name `local-first` states the order the policy keeps once an in-process attempt can end with `unavailable` (open question 4), and it is the name spec 02, spec 06, the roadmap, VISION's "both at once" and #13 already use. Kubernetes' KEP 3015 shows the cost of the other path: it deprecated `PreferClose` for `PreferSameZone` and `PreferSameNode` because the old name's meaning could not grow without surprising someone. Here the name already says what it will mean when the local member arrives; renaming now would cost five documents and a later rename back. This record keeps the name and states plainly what it does in 0.1.
- **Accept `"single"` as an explicit value.** It lets a settings file state the default. It is a second spelling of absence.

### 2. The policy is named per identity in `NEOLITH["ROUTING_POLICIES"]`, and `REMOTE_PROVIDERS` admits more than one entry only under `local-first`

`ROUTING_POLICIES` maps a contract identity to its policy. `REMOTE_PROVIDERS` keeps 0014's form; its list widens from exactly one entry to one or more, and a list of two or more is admitted only for an identity that `ROUTING_POLICIES` names. Each entry is a distinct provider, never a replica of another: 0014's open question 3 expected this, and replicas behind one name belong to a load balancer or a Service, which is one `url` here.

In 0.1 the key carries nothing the list's length does not already show: two or more entries are admitted only with `local-first`, `local-first` only with two or more entries, and `"local-first"` is the one value. The deployer writes the same fact twice, and the host has five messages for the ways the two can disagree. This record pays that cost for three reasons. A list of endpoints means load balancing in Envoy clusters, HAProxy backends and Kubernetes Services; HAProxy asks for an explicit `backup` marker before it treats a server as a fallback, and the key plays that part here. A second url added by mistake then stops the host with a message instead of turning on failover silently (principle 6). And a later policy, such as `remote-first`, needs a key anyway; adding it later would make an absent key beside two entries a second spelling of `local-first`.

This narrows 0014. It quotes the sentences it narrows:

- From 0014's admitted set: `providers = list of exactly 1 entry ; single routing; Decision 2`. In its place: `providers = list of 1 or more entries, no url twice`, two or more only under `local-first`.
- From 0014's builder rows, the value row's admitted cell: "A `list` of exactly one entry." In its place: "A `list` of one or more entries; two or more only when `ROUTING_POLICIES` names `local-first` for the identity", as in this record's table.
- From 0014's builder rows, the entry row's first refusal: "`NEOLITH["REMOTE_PROVIDERS"]["<id>"][0]: expected a dict with the keys BACKEND and OPTIONS, got <type>`", whose `[0]` the row's other messages and the `BACKEND`, `OPTIONS` and `url` rows carry through their `...`. In its place, `[<i>]`, the entry's index, in each of those messages; their text is otherwise unchanged.
- From 0014 Decision 2: "A connector looks the transport up by identity on each call (0021 Decision 10), so a connector never picks per call and never changes transport between attempts: under `single` there is one attempt (TRN-17)." In its place, for a contract version under `local-first`: the set-up table holds a tuple of transports for the identity, in the order listed; the connector looks the tuple up by identity on each call and uses one transport for each attempt, moving to the next as Decision 4 says. Under `single` the sentence stands as written.
- From 0014 Decision 2: "The host builds one HTTP transport per identity; ..." In its place: the host builds one HTTP transport per entry of an identity's list, so one per identity under `single` as before; the rest of the sentence (connections belong to the client's pool for the origin, so identities on one origin share them) is unchanged.
- From 0014 Decision 2: "The value is a list of exactly one entry." In its place: the value is a list of one or more entries, two or more only under `local-first`. The rest of that paragraph, that "Admitting more entries later widens the row", is what this record does.
- From 0014's builder rows, the value row's third refusal: "`...: lists <n> providers; with the single routing policy a contract has one provider, and routing between several arrives with step 6 (ADR 0016)`". In its place, the message in this record's table.
- From 0014's Resolve rows: "`NEOLITH["REMOTE_PROVIDERS"]: <id> is provided by app <label> in this host; a contract provided in the host and remotely at once needs a routing policy other than single, which arrives with step 6 (ADR 0016)`". In its place, the message in this record's table. The refusal itself stays (Decision 3).
- From 0014's system-check row: "`neolith.W002`: `NEOLITH["REMOTE_PROVIDERS"]["<id>"]: <url> is plain HTTP to a host that is not loopback; ...`". In its place, the same message with the entry's index, `NEOLITH["REMOTE_PROVIDERS"]["<id>"][<i>]: <url> is plain HTTP ...`, run for every entry.
- From 0014 Decision 2's alternatives: "Step 6's `local-first` needs only one remote provider beside the in-host one", and "Choosing per call, with the in-host provider preferred when it exists. That is `local-first`". In 0.1, `local-first` orders two or more remote providers and has no in-host member (Decision 3); the in-host provider is preferred in the sense that it is the only one, as under `single`.
- From 0014's Consequences, the specification gains "that a host stops before it serves when its configuration names a remote provider for a contract version none of its apps requires, or, under `single`, one of its apps provides". In its place, the refusal holds under every policy: step 5's TRN-67, which carries those words, is withdrawn and restated as TRN-new-r8 (Decision 10).

0014's Decision 2 already says that "Admitting more entries later widens the row", and its Decision 7 that "step 6's record gives the policy its form". 0014 gains `Narrowed by 0031 (Decision 2)` under its status.

Alternatives weighed:

- **The policy inside the `REMOTE_PROVIDERS` value**, as `{"POLICY": "local-first", "PROVIDERS": [...]}`. Everything about an identity's routing sits in one place. A step 5 host's list would then be a second form of the same value, or a breaking change.
- **The policy implied by the list's length**: two or more entries mean `local-first`. One line less per identity, and no fact written twice. The policy would be invisible in settings, a list would read as load balancing to anyone who knows Envoy or HAProxy, a second url added by mistake would turn on failover silently, and a third policy later would need a key anyway.
- **One policy for the whole host**, as `NEOLITH["ROUTING_POLICY"] = "local-first"`. One line. 0014 keys every remote provider by identity, and Resolve, `connect()` and every message speak of identities; a host-wide value would apply to identities with one provider, where it has no meaning, and could not later hold two policies.
- **A policy inside each entry.** A policy describes the set of providers, not one of them.
- **The key name `ROUTING`.** Shorter. `ROUTING_POLICIES` says what the values are, as `REMOTE_PROVIDERS`, `PRINCIPAL_BACKENDS`, `CALLER_BACKENDS` and `SIGNING_SECRETS` do.
- **No limit, or a limit, on the number of entries.** This record sets none. The call's deadline bounds the time spent, and Decision 5's share keeps one silent provider from taking all of it; a call with no deadline and several providers that cannot be reached spends up to the connect cap on each (Consequences). A number such as 3 or 8 would be a guess.

### 3. `local-first` tries the provider in the host first, then the remote providers in the order listed; in 0.1 an identity provided in the host has no remote provider beside it

Under `local-first` the connector's order is: the provider in the host, when an app there provides the identity; then each `REMOTE_PROVIDERS` entry, in the list's order.

An identity provided in the host and also configured remotely stays refused in 0.1, under every policy, with the message in the table. That is step 5's TRN-67, which names the refusal "under the `single` policy"; Decision 10 restates it for every policy. The reason is fact 1: an in-process call never ends with `unavailable`. The provider in the host would answer every call, a failure of its action included (`internal`), and Decision 4 moves a call on `unavailable` only. A remote provider listed beside it would never be called, so admitting the entry would admit a setting with no effect, which a deployer could read as redundancy the host does not have. 0021's APP-9, one providing app per identity in a host, is unchanged. Erlang's `pg` reaches the same result by a different road: a caller that asks for local members first uses remote ones only when no local member exists, so with a provider always present in the host the remote ones are never used.

Running an app attached and detached at once (0016's goal, VISION's "both at once") still works, at the level of the deployment:

- the host that holds the app attached calls it in-process, and also serves its contracts over HTTP, as every host serves the contracts it provides (0011);
- the app runs detached in its own host as well;
- every other host lists both under `local-first`, in the order it prefers, so that an idempotent call moves to the second when the first answers `unavailable`.

A migration in progress is a change of that order in the other hosts' settings, then the removal of the app from the first host. Roadmap step 8's both-at-once example shows this arrangement: one app attached in one host and detached in another, and a third host that lists both. It does not show one host calling an app both in-process and remotely, which 0.1 does not admit.

When a later record lets an in-process attempt end with `unavailable`, as runtime mode switching might (the repository's open question 2), the refused row widens to admit an identity provided in the host with remote providers after it, and `local-first` gains its local member without a change of form (open question 4).

Alternatives weighed:

- **Admit the remote entries beside the provider in the host, and document that they are never called in 0.1.** It is the configuration 0016 describes, and it would admit one settings module shared by hosts with different apps. It admits an input with no behaviour, the opposite of what 0014 and 0022 do for an empty key or an explicit default. A shared module is also refused already, by 0014's row for an identity no hosted app requires.
- **Move a call from the provider in the host to a remote one on `internal` or `timeout`.** It would make the remote entries reachable. `internal` means the action failed or its outcome could not be read, after the action may have run; `timeout` leaves the outcome unknown (TRN-38, 0008). Both providers of one app share the app's one database (0004, 0016's first constraint), so a failure of that database fails both. Moving would send a call twice after its action may have run, which TRN-8 and CON-4 rule out.
- **Let the in-process transport produce `unavailable` during Drain.** Drain lets requests in flight finish (APP-41), and the calls they make in-process are part of finishing them. Refusing them would turn a clean shutdown into failed requests.

### 4. A call moves to its next provider only on `unavailable`, and only for an idempotent action

An *attempt* is one provider's part in a call. The connector moves a call to its next provider only when all of these hold, which is TRN-8 as it stands:

1. the attempt ended with `unavailable`;
2. the action is declared idempotent (CON-4);
3. an entry is left in the policy's order;
4. the call's budget, read before the next attempt starts, is at least 1 ms (Decision 5).

The test is the kind and the action's declaration alone, as spec 01's kinds table states it ("retry safe: yes, if idempotent" for `unavailable`, "no" for every other kind). It does not ask why a provider failed, and it does not ask in which phase. So every `unavailable` is treated alike, whether its cause is a fault of the deployment or of the network:

| The attempt ended with | Moves on? | Why |
|---|---|---|
| `unavailable`, from any cause: the name did not resolve, the connection was refused or reset, the TLS handshake failed or the certificate did not verify, the connect cap or Decision 5's share passed, a reset or close after the request left (the Drain race of fact 8 included), a response outside the admitted HTTP forms, a non-success status without a neolith error body (a proxy's `502` or `503`, a `404` from a provider on an older contract version), or a neolith error body of kind `unavailable` | Only for an idempotent action | The provider may have run the action. An idempotent action promises the same effect for the same key (CON-5), and every attempt carries the same key (Decision 6). A certificate that did not verify moves too: no request was written to the peer that failed the check, the next provider's certificate is verified on its own, and the transport logs the failure at `WARNING` (fact 7). |
| `timeout`, from the transport, the provider's host or the connector's own check | Never | The outcome is unknown, and the call's one deadline has passed (TRN-38, spec 01's table). |
| `unauthenticated` from the provider's host, a credential it refused included (0027 Decisions 7 and 8) | Never | Spec 01 marks the kind not retry safe, and the provider's host answered. The AWS SDKs likewise never retry a non-retryable error such as `AccessDeniedException` ([retry behavior](https://docs.aws.amazon.com/sdkref/latest/guide/feature-retry-behavior.html)). |
| `permission_denied`, `invalid_argument`, `not_found`, `conflict`, `internal`, or a kind the action declares or the caller does not know | Never | Not retry safe in spec 01's table: the provider answered, or its answer could not be read after it may have run. |
| A refusal before sending: every refusal of 0017 Decision 9's steps 1 to 4, and a call with no calling app or from an app with no signing key (0027 Decision 9, HTTP-18) | Never | Every provider would refuse it the same way. Steps 1 to 4 run once per call, before the first attempt. The other two are the HTTP transport's own refusals, made on each attempt before it sends (fact 4); the first attempt ends with `unauthenticated`, which never moves, so the call ends there. |

So a non-idempotent call makes one attempt, to the first provider, under every policy; an idempotent one at most one attempt per entry. TRN-17 is unchanged for `single`.

A refused credential and a certificate that does not verify are both deployment faults, and the rule treats them differently because their kinds differ: the first is the provider's own answer, the second is no answer. Both are logged at `WARNING`, at the caller for the certificate and at the provider for the credential (0027 Decision 8), so neither is hidden by a move. A credential missing at the first provider still shows only in that provider's log while the call succeeds at the second; Consequences name this.

Alternatives weighed:

- **Also move a call whose attempt was not sent, whatever its action (Option 3).** No provider can have seen such a call, and RFC 9110 section 9.2.2 allows an automatic retry of a non-idempotent request when the client can tell it "was never applied"; gRPC, Envoy (`connect-failure`, `reset-before-request` with `previous_hosts`) and HAProxy (`retry-on conn-failure` with `option redispatch`) all do this (fact 10). The gain is real on a direct connection whose provider refuses, does not resolve or drops packets. It is smaller than it looks: a load balancer in front answers after the request left, a rebooted host resets a pooled connection after the write, and the Drain race of fact 8 fails in `"reading"`, so each of those is sent. It changes four Accepted or specified statements (CON-4, TRN-8, 0017 Decision 9, 0028 Decision 5), and every transport must then report, without a default that could be wrong, whether an attempt was sent. Widening to it later is compatible; open question 5 keeps it together with the next alternative, since the two cover one class.
- **Treat a request the provider refused before dispatch as not run** (gRPC A6's case 3, HTTP/2's `REFUSED_STREAM` and `GOAWAY` in RFC 9113 section 8.7, Envoy's `refused-stream`, Finagle's nack: "no work has been done"). A neolith host refusing a request during Drain would answer with a marker, and the caller would move the call whatever its action. It is the piece that would make a rolling restart of the first provider invisible to non-idempotent callers (Consequences). It is a wire change to spec 03 and to `_server.py`'s Drain, with its own tests, and it only matters together with the previous alternative. Open question 5.
- **Move on a credential refused by the provider (`unauthenticated`).** The provider's host authenticates the calling app before it reads the body (spec 03), so the action did not run and a move is safe for the data. Spec 01 marks the kind not retry safe, and changing that is outside this record; a key missing at the first provider would also go unnoticed at the caller until the second is down too.
- **Exclude a certificate that does not verify from moving**, since it may be a sign of interception. The call never reached the peer that failed the check, and moving sends it to a provider whose certificate is verified on its own, so the move exposes nothing; refusing it would only fail the call. The `WARNING` line keeps the failure visible.
- **Move on `unavailable` for every action.** Simplest for redundancy. A reset after the request left, or a proxy's `502` after the provider received it, would send a non-idempotent call twice, the duplicated effect CON-4 exists to prevent.
- **Move on a `404` only for an idempotent action, and never on other non-success statuses.** 0011 open question 7 notes that `404` from an older provider and `503` from a proxy are both `unavailable` today. Separating them needs a new kind (`unimplemented` is reserved by CON-23) and is outside this record (open question 9).

### 5. One deadline spans every attempt; a call ends with its last attempt's outcome; each entry is tried at most once; a silent provider costs its share

The call's deadline is fixed at 0017 Decision 9's step 4, once (TRN-27). Each attempt is written with the budget that remains when it is written, read through the one helper 0028 Decision 2 defines.

**Ending a call.** When a call is not moved after an attempt, for any reason of Decision 4's list, it ends with that attempt's outcome, as it is, apart from the text of a `timeout` after a move (below). When the budget read before the next attempt is under 1 ms, the next attempt is not started and the call ends with the last attempt's `unavailable`: the deadline has not passed, so TRN-38 does not turn it into `timeout`. TRN-28 governs a call's first attempt, whose budget is read before anything is sent; TRN-new-r5 says that it comes before TRN-28 for an attempt after the first. The connector's check on the outcome (TRN-38) is unchanged and applies to the call's last outcome. The connector never writes an error of its own for routing, apart from the text below; earlier attempts are in the log (Decision 8).

**A `timeout` after a move.** Once a call has moved, an earlier attempt may have run the action. The connector, which alone knows that the call moved, gives a `timeout` the call then ends with the text `_LATE`, "the action might have run", whichever part produced it: the transport's connect phase, the budget read when the request is written (both `_NOT_SENT` from the client, fact 4), or the connector's own check. The transport is unchanged, apart from the connect bound below, and keeps writing 0028's texts; the connector replaces only the message of that `timeout`, never its kind. Only a call that has not moved can say it was not sent. This departs from three sentences of the specification for a call that has moved: TRN-38's "A `timeout` that a transport or a provider's host produced is passed on as it is", and the "not sent" of TRN-64 and HTTP-17 (after TRN-28). TRN-new-r7 states the departure and its precedence, so those three keep their words for a call that has not moved.

Alternatives weighed for that text:

- **The transport writes `_LATE`, told by a new input that the call has moved.** The text would come from where the phase is known. The `Transport` protocol and every transport would then take a second routing input beside the connect bound, for a fact the connector already holds.
- **Withdraw TRN-28, TRN-38, TRN-64 and HTTP-17, and add each again per attempt.** Each rule would read correctly on its own. Four requirements, three of them merged days ago, would be replaced for a case that one new requirement with a precedence sentence covers, as TRN-20 and TRN-38 already state theirs.

**Each entry once.** A call tries each entry at most once. The connector never sends a call to the same entry again, whatever the kind: the one replacement connection of 0028 Decision 5 is part of one attempt, not a second one. Attempts run one after another in the calling thread; none runs concurrently with another (TRN-6 in-process, 0028 Decision 8 over HTTP).

**A share of the connect phase.** When a call can still move after its attempt (the action is idempotent and an entry is left), the attempt's connect phase is bounded, besides the connect cap and the deadline, by the attempt's share of the time left: that time divided by the entries left, this one included, at least 2 seconds, or all the time left when that is less. It is the rule 0028 Decision 2 gives each resolved address, applied one level up. When the share ends the phase, the attempt ends with `unavailable` and the call moves; when the share is all the time left, the deadline ends the phase, and the attempt ends with `timeout`. A call with no deadline has no share, and a non-idempotent call, or the last entry, gets none either, since it cannot move and a share would only cut its one attempt short. Under `single` nothing changes.

Without the share, a provider that drops packets holds the connect phase until the earlier of the cap and the deadline; when the deadline comes first, the attempt ends with `timeout`, which never moves. Every call with 10 seconds or less left would then never reach its second provider: a call made after 20 seconds of a 30 second request root, or one given a timeout under 10 seconds. With the share, a call with 8 seconds left and two entries gives the first 4 seconds and the second the rest.

This narrows 0028, for an attempt under `local-first`. It quotes the sentences it narrows:

- Decision 2: "**The connect cap**, 10 seconds, bounds the three steps together, on every call that opens a connection, deadline or not (0014's A2). When the cap ends the phase, the call fails with `unavailable`; when the deadline does, with `timeout`." In its place, for an attempt that can still move: the earliest of the cap, the deadline and the attempt's share bounds the phase; when the deadline ends it, the attempt fails with `timeout`, otherwise with `unavailable`.
- The admitted set's constants: "`connect cap = 10 s ; resolve + connect + TLS handshake, on every call that opens a connection`". In its place, for that attempt, the earlier of the cap and the share.
- The admitted set's reasons: "The `<reason>` is the first of: `its name did not resolve`; `no connection within 10 seconds`; ...". In its place, `no connection within <n> seconds`, where `<n>` is the bound that ended the phase.
- Decision 5: "the transport closes it and opens one new connection, within the same deadline and connect cap, once". In its place, within the same deadline, connect cap and share: the share bounds the attempt, and the replacement connection is part of that attempt.
- The admitted set's failure table, *Before writing*: "`timeout`: `the deadline passed before the call was sent`"; Decision 2: "Under 1 ms, the call fails with `timeout`, unsent"; and the same words in Decision 5 for the replacement connection. In their place, after a move, the call ends with `_LATE`'s text, as above. The transport's failure is unchanged; the connector replaces the message the caller receives.

The narrowing form of D2 fits because each rule only shortens a bound 0028 set, or changes a message, for calls 0028 did not have: a call that can move. A call under `single`, a non-idempotent call, the last entry and a call with no deadline are bounded and worded as before. 0028 gains `Narrowed by 0031 (Decision 5)` under its status.

**After the request is written**, an attempt runs until it ends or the deadline passes. No attempt gets a timeout of its own once it is sent.

**No retry budget.** Each entry is tried at most once per call, so the extra load routing adds is bounded by the list's length, and every move lands on a different provider. gRPC exempts its not-sent retries from throttling for the same kind of reason. What a budget would prevent, and this record does not, is a first provider that answers `unavailable` because it is overloaded shifting all of its idempotent traffic to the second at once (Consequences, open question 6).

Alternatives weighed:

- **A per-try timeout after the request is written**, as Envoy's `per_try_timeout` and gRPC's per-attempt deadlines. A provider that accepts the call and never answers would cost a share of the deadline, not all of it, and an idempotent call could then move on. It is a second deadline beside the call's (TRN-27), "with its own rule for which wins", which is the reason 0014 Decision 3 gives for declining a per-provider `timeout` option; 0028 Decision 5 leaves "Whether a try gets a timeout of its own inside the deadline" to this step. A try cut after it was sent also leaves its outcome unknown, and the provider would go on running the action under the budget it received. It needs an input, per entry or per identity. Adding it later is a widening (open question 2). The share above bounds only the phase before anything is written, where cutting an attempt leaves nothing unknown.
- **No share, only the connect cap.** One rule fewer. On the default 30 s deadline with two providers the share is 15 s, more than the cap, so nothing changes; but calls nested under a request root often have less than 10 s left, and for them failover would never happen.
- **`timeout` with `_NOT_SENT`'s text when no time is left for the next attempt**, as TRN-28 says for a call that is never sent. The call was sent, or at least attempted, and its last attempt's outcome is known; reporting `timeout` would make an idempotent caller treat a known `unavailable` as an unknown outcome, and the text would be false for a call whose first attempt left.
- **Retry the same provider**, with a backoff and a budget, as TRN-8 allows and as Finagle and gRPC do. It helps with a provider that drops one connection. It needs a wait inside the deadline, which T3 makes hard to test without sleeping, a rule for how many times, and a budget so that retries do not multiply load during an outage, which is per-process state (Decision 7). A second entry with another url is the way to ask for another attempt in 0.1 (open question 3).
- **A retry budget on moves**, as Finagle's `RetryBudget` (about 20% of requests plus 10 per second), the AWS SDKs' retry quota (a 500-token bucket, 14 tokens per transient retry) and gRPC's per-server throttling keep. It protects a struggling provider from retries. Here a move goes to another provider, at most once per entry, so load on any one provider does not multiply; a budget would be process-wide mutable state with its own clock (K6). Open question 6.
- **Hedged requests**, sending to the second provider before the first has answered. Lower latency. It sends a call twice by design, which runs work concurrently in a synchronous caller and is not open to a non-idempotent action.

### 6. Every attempt carries the same call context; only the credential, the span and the budget are per attempt

Every attempt of one call carries the same principal, trace identifier, `tracestate` and idempotency key, the ones `Sent` holds (fact 3). The key is derived once, at step 3 of 0017 Decision 9, and the parent's occurrence count goes up once for the call, not once per attempt (TRN-32: "A call counts as made once it passes the caller's context and request checks, whatever becomes of it then"). So two providers of one app, which share its one database (0004), see the same key for the same call, and a host-side idempotency record (the repository's open question 6) could recognize the second attempt. The AWS SDKs do the same with a Smithy `@idempotencyToken` member such as `ClientToken`: filled once per operation and reused across retries ([idempotencyToken trait](https://smithy.io/2.0/spec/behavior-traits.html#idempotencytoken-trait)).

Per attempt, as step 5 already does per connection (fact 4): a fresh token, minted for the calling app and the identity (0027 Decision 9), a new span identifier in `traceparent`, and the budget read when the request is written.

Alternatives weighed:

- **A new idempotency key per attempt.** Each provider would see an unrelated call. That defeats the key's purpose, which spec 00 states: "The key stays the same only across retries of that same call and, when the parent has a key of its own, across retries of the parent that make the call again"
- **One token for every attempt.** One signature fewer. A token's `aud` is the identity, not the provider, so it would verify at each; 0027 Decision 9 mints one per attempt, and its 60 second life would then have to cover every attempt of a long call.
- **An attempt number on the wire**, as gRPC's `grpc-previous-rpc-attempts` (absent on the first attempt, 1 on the second) and Envoy's `x-envoy-attempt-count`. A provider could tell a second attempt from a first in its logs and in an idempotency record. It is a new field in spec 03, with its rules and tests, for a provider side that reads none of it in 0.1; the trace identifier and the distinct spans already tie and separate the attempts in logs, and the key alone is what an idempotency record needs. Open question 7. If adopted, it is never logged with the key.

### 7. Nothing is remembered between calls: every call starts from the first provider

Each call walks the policy's order from its first entry, whatever earlier calls met. The order is a tuple of transports that `build` makes once per set-up and keeps in the set-up's table beside each identity, as it keeps the one transport today (0014 Decision 2, 0021 Decision 8). It is never written after set-up.

This is the per-request ordered walk of Envoy's `previous_priorities` with `previous_hosts` (fact 11): each attempt goes to an entry the call has not used yet, in priority order, and no state outlives the call. Priority failover elsewhere, in Envoy's priority levels, HAProxy's backup servers and Consul's failover, is driven by remembered health instead; this record leaves that to open question 1, and states its cost below.

So routing adds no state that changes while the host serves:

- **Fork.** Nothing to reset. The pools under the transports are 0028's, emptied in a child by its `register_at_fork` hook (fact 5).
- **Threads.** Concurrent calls each read the same immutable tuple; no lock is taken for routing. This holds on a build without the GIL as well.
- **Async.** The connector is synchronous (0028 Decision 8). A call from async code through `sync_to_async` walks the order in the worker thread like any other call. The async connector of a later record walks the same order, one attempt at a time.
- **Tests.** A call's route depends only on the settings, the call and the providers' answers, so each case of Decision 9 is deterministic without a clock seam for routing (T3).

The cost, during an outage of the first provider: every idempotent call tries it first. One that refuses connections or does not resolve costs about one round trip. One that drops packets costs its share of the connect phase, at most the 10 second cap: with the default 30 s deadline and two entries, 10 s per call, and the second gets the remaining 20 s; with three entries and the first two silent, 10 s each, and the third gets 10 s. A non-idempotent call fails with `unavailable` after up to 10 s, or with `timeout` at a deadline under that.

Alternatives weighed:

- **Remember a failed provider and skip it, or try it last, for a while**, as Envoy's outlier detection and Finagle's failure accrual do (fact 11). During an outage of the first provider, calls stop paying for it. It is mutable process-wide state (K6) with a clock, a lock and a reset at fork; it needs a rule for trying the provider again, which sends a real call as the probe or adds the network check before serving that 0014 Decision 5 declined; it makes two identical calls take different routes, so a conformance item would have to fix the clock; and the specification would have to state it for two implementations to give the same results (principle 5). Adding it later changes no settings form; it replaces TRN-new-r1, which fixes the order for every call (open question 1).
- **Stay on the provider that last answered**, as gRPC's `pick_first` does (fact 11). One integer of state. It turns the listed order into a preference that a past failure overrides until the next failure, so a provider that comes back is not used again until the other fails; a migration would no longer follow the order the deployer wrote.

### 8. What is logged, and what never is

The HTTP transport keeps its logging as step 5 has it (fact 7): one `WARNING` for each attempt that ends with `unavailable` because the client failed or the provider answered a non-success status without a neolith error body, naming the identity, the action and the trace identifier, and then, for a failure of the client, its message (which names the provider's origin), the phase and the cause cut to 200 characters, or, for a status without a neolith error body, the provider's origin and the status. A neolith error body of kind `unavailable` is not logged at the caller by the transport: the provider's host chose that answer and logged it (#129). The connector's line below is then the caller's only record of that attempt.

The connector adds one line at `INFO` on the logger `neolith` for each routing decision after an attempt that ended with `unavailable` under `local-first`, and only then:

- **Moved:** `<id>, action '<action>': provider <i> of <n> at <origin> was unavailable, and <action> is idempotent; trying provider <i+1> at <origin> with <ms> ms left (trace <trace>)`, or `... with no deadline`.
- **Not moved, the action is not idempotent:** `<id>, action '<action>': provider <i> of <n> at <origin> was unavailable, and <action> is not idempotent, so the call is not moved to provider <i+1> (trace <trace>)`.
- **Not moved, no time left:** `<id>, action '<action>': provider <i> of <n> at <origin> was unavailable, and the call's deadline left under 1 ms to try provider <i+1> at <origin> (trace <trace>)`.

A call whose first attempt succeeds, a call whose last entry fails, and a call under `single` log nothing new. A provider in the host is named `in this host`; a remote one by its admitted `url`, which by 0014's grammar holds no credential.

Never logged, at any level, by routing: the request or reply text, an error's message or detail, the `Authorization` value or any part of a token, the principal or its sent form, the idempotency key, `tracestate`, and any `url` or policy value that set-up refused (0014 Decision 3's class rule, 0027 Decision 8). The trace identifier is what ties a call's lines together.

Alternatives weighed:

- **One `WARNING` per move.** A move is a failure the deployer should see. The transport has already logged most failures at `WARNING`; a second `WARNING` for the same attempt doubles the lines during an outage, which 0028 already counts as one per call.
- **A transport `WARNING` for a neolith error body of kind `unavailable` too.** One rule for every `unavailable`. The provider's host already logs each response it sends (#129), and an overloaded provider answering `unavailable` to every call would then double its operator's lines at every caller; the connector's `INFO` line records it where routing acted on it.
- **Nothing from the connector.** The transport's line says that a provider failed, not whether the call then moved or why it did not; "why did failover not happen" is the question an operator will ask of a non-idempotent call.
- **Log the idempotency key**, to match attempts across providers. The trace identifier does that. A derived key is predictable from its parent's (0017 Consequences), and logs travel further than the call.

### 9. Tests

The private `over_http` seam of 0014 Decision 6 is unchanged: one identity, one origin. Routing is tested through settings, where a deployer configures it (principle 3), and through the connector with fake transports; no routing seam is added. The conformance record keeps `over_http` private in 0.1 (its Decision 6).

**Unit tests**, deterministic and in one process:

- **The connector's walk**, with fake transports that return each outcome of Decision 4's table and record what they were given: an idempotent call moves on `unavailable`, a non-idempotent one does not, and no other kind moves; each entry is tried once and the call ends with the last attempt's outcome; under 1 ms left before the next attempt the call ends with `unavailable`, not `timeout`; a `timeout` after a move carries `_LATE`'s text, whichever part produced it; every attempt receives the same `Sent`; the connect bound each attempt is given matches Decision 5's share, and none is given to a non-idempotent call, the last entry or a call with no deadline; two calls in a row each start at the first entry; and each log line of Decision 8, with no key, token, principal or `tracestate` in it.
- **The client's connect bound**, with 0028 Decision 9's resolver stand-in holding resolution: the phase ends at the bound with `unavailable` and the message `no connection within <n> seconds`, at the deadline with `timeout` when the deadline comes first, and the replacement connection of 0028 Decision 5 stays inside the same bound.
- **The rows**: one refused value per row of the admitted set, each message checked, and the absence of every refused url and policy value from the message; the Resolve rows reported together with other refusals, and an identity in `ROUTING_POLICIES` with no entry reported only by 0014's `has no provider`; `ROUTING_POLICIES` refused under `override_settings`; `neolith.W002` on the second entry of a list; and a step 5 settings module, with one entry per identity and no `ROUTING_POLICIES`, admitted unchanged and routed by `single`: its identity's order holds one transport, and a call to a provider that answers `unavailable` makes one attempt (APP-new-r1, TRN-17).

**The failover cases**, in the conformance suite's routing part (`tests/conformance/routing/`). The conformance record says how they are driven, named and reported (its Decision 8): one host process of its own, configured through settings in Decision 2's form, against its scripted providers, bound on `127.0.0.1` and port 0 before the settings are written, each recording every request it receives. The letters in the table name its scripted providers, which its Decision 8 defines, together with each case's small test contract of `get` (idempotent) and `put` (not idempotent): **R** refuses connections, **U** answers `503` with no neolith error body, **N** a neolith error body of kind `unavailable`, **X** resets after reading the request, **E** an error body of the one kind its case gives it, **V** a reply that is not valid, **S** never answers, **B** a valid reply, and **D** is a second **U**. A `single` case leaves its identity out of `ROUTING_POLICIES` and lists one entry; every other case names `local-first` and lists two (Decision 2).

This table is the one list of the routing cases and their outcomes. Each case has the name its entry in `spec/conformance/cases/routing.json` gives it, under the conformance record's naming rule, and the conformance record, spec 06 item 4 and the tests cite these names. The last column lists the requirement IDs each case exercises; TRN-new-r6 (the connect share) and TRN-new-r7 (the text after a move) are exercised by the unit tests above, since no case on the local machine holds a connect phase open or lets a deadline pass after a move without waiting for it. APP-new-r1 is cited first by the rows' test of a step 5 settings module above, in the pull request that adds it, and again by the two `single` cases.

| Case | Entries | Call | Outcome | Requests recorded | Cites |
|---|---|---|---|---|---|
| `routing_single_one_attempt` | U, under `single` | `get` | `unavailable` | exactly one to U | TRN-17, APP-new-r1 |
| `routing_single_refused` | R, under `single` | `get` | `unavailable` | none | TRN-17, APP-new-r1 |
| `routing_moves_on_503` | U, B | `get` | the reply | one to U, then one to B, with the same idempotency key, principal, trace identifier and `tracestate`, a different span and token, and a `Neolith-Timeout` at B that is 5 less than at U | TRN-8, TRN-new-r1, TRN-new-r4 |
| `routing_moves_on_refused_connection` | R, B | `get` | the reply | one to B | TRN-8, TRN-new-r1 |
| `routing_moves_on_unavailable_body` | N, B | `get` | the reply | one to N, then one to B | TRN-8 |
| `routing_moves_on_reset_after_read` | X, B | `get` | the reply | one to X, then one to B with the same idempotency key | TRN-8, TRN-new-r4 |
| `routing_put_stays_after_503` | U, B | `put` | `unavailable` | one to U, none to B | CON-4, TRN-8 |
| `routing_put_stays_after_refused_connection` | R, B | `put` | `unavailable` | none to B | CON-4, TRN-8 |
| `routing_put_stays_after_reset_after_read` | X, B | `put` | `unavailable` | one to X, none to B | CON-4, TRN-8 |
| `routing_stays_on_timeout` | E (`timeout`), B | `get` | `timeout`, passed on | one to E, none to B | TRN-8, TRN-new-r5 |
| `routing_stays_on_unauthenticated` | E (`unauthenticated`), B | `get` | `unauthenticated`, passed on | one to E, none to B | TRN-8, TRN-new-r5 |
| `routing_stays_on_permission_denied` | E (`permission_denied`), B | `get` | `permission_denied`, passed on | one to E, none to B | TRN-8, TRN-new-r5 |
| `routing_stays_on_conflict` | E (`conflict`), B | `get` | `conflict`, passed on | one to E, none to B | TRN-8, TRN-new-r5 |
| `routing_stays_on_internal` | E (`internal`), B | `get` | `internal`, passed on | one to E, none to B | TRN-8, TRN-new-r5 |
| `routing_stays_on_declared_kind` | E (a kind `get` declares), B | `get` | that kind, passed on | one to E, none to B | TRN-8, TRN-new-r5 |
| `routing_invalid_reply_stays` | V, B | `get` | `internal` | one to V, none to B | TRN-8, TRN-18 |
| `routing_silent_provider_times_out` | S, B | `get`, timeout 500 ms | `timeout` | one to S, none to B | TRN-8, TRN-38 |
| `routing_no_time_left_ends_unavailable` | U, B | `get`, whose deadline has under 1 ms left, and has not passed, when U answers | `unavailable` | one to U, none to B | TRN-new-r3, TRN-new-r5 |
| `routing_last_entry_unavailable` | U, D | `get` | `unavailable` | one to each, in that order | TRN-new-r2, TRN-new-r5 |
| `routing_moves_with_no_deadline` | U, B | `get`, with no deadline | the reply | one to each, neither with `Neolith-Timeout` | TRN-8 |
| `routing_each_call_starts_first` | U, B | `get`, twice | the reply, twice | two to U, each before its call's request to B | TRN-new-r1 |

The suite's clock, patched in that process at `_context.now` and `_http._now`, stands still until U moves it from inside, before it answers: `routing_no_time_left_ends_unavailable` moves it past the deadline's last millisecond, and `routing_moves_on_503` by 5 ms, so that the second attempt's budget is exactly 5 less than the first's whatever the machine's speed (T3). `routing_silent_provider_times_out` is the one case that waits in real time, and only for its own deadline. A host that names a remote entry for an identity one of its apps provides is a row test above, not a failover case: it is refused before Serve.

The tests reach only the local machine and wait on events, never on `sleep`, apart from the silent provider's case and the client bound's own condition (T3).

### 10. What the specification says

In spec 02, the section *Providers and routing* reads, in implementation-neutral terms (P2):

> A connector holds an ordered set of providers for a contract version. Each provider names a transport and the configuration that transport needs. A **routing policy** chooses which providers the connector tries for each call, and in what order. Spec 0.1 defines two:
>
> - `single`: one provider;
> - `local-first`: two or more providers, tried in order: the provider in the caller's own host first, when there is one, then the others in the order the host's configuration lists them.
>
> A later version may define others, such as `remote-first`, a weighted choice, or a choice made from the call context.
>
> An *attempt* is one provider's part in a call. A connector *moves* a call when it starts an attempt to the next provider after an attempt that failed.
>
> A host learns of a provider in two ways: from its own apps, one of which may provide the contract version, and from its configuration, which may name a *remote provider* for it, reached over a network transport. (unchanged, step 5's task 5)
>
> **TRN-66.** Under the `single` policy, a host MUST choose each contract version's one provider before it serves: the app of the host that provides it, when there is one, and otherwise the remote provider its configuration names for it. (unchanged, step 5's task 5)
>
> ~~**TRN-7.** ...~~ (unchanged)
>
> **TRN-8.** (unchanged) A retry or a fallback after a failed call MUST happen only for an idempotent action, only on `unavailable`, and only within the call's deadline.
>
> **TRN-new-r1.** Under `local-first`, a connector MUST try a call's providers in the policy's order, starting from the first.
>
> **TRN-new-r2.** A connector MUST try each entry of the policy's order at most once for one call.
>
> **TRN-new-r3.** A connector MUST NOT start another attempt of a call whose budget, read before that attempt starts, is less than 1 ms.
>
> **TRN-new-r4.** Every attempt of one call MUST carry the same principal, trace identifier, `tracestate` and idempotency key.
>
> **TRN-new-r5.** When a connector does not move a call after an attempt, the call MUST end with that attempt's outcome, subject to TRN-38 and TRN-new-r7. A next attempt that is not started because the budget is under 1 ms (TRN-new-r3) so leaves the call with its last attempt's `unavailable`. This comes before TRN-28, which governs a call's first attempt.
>
> **TRN-new-r6.** Over a network, when a call that has a deadline can still move after an attempt (its action is idempotent and an entry of the policy's order is left after this one), a connector MUST end that attempt with `unavailable` if it has no connection ready within its share of the time left: that time divided by the entries left, this one included, and at least 2 seconds. When less than 2 seconds are left, the share is all of it, and the deadline ends the phase as TRN-64 says.
>
> **TRN-new-r7.** A call that has moved MUST NOT end with a `timeout` that says it was not sent, since an earlier attempt may have run its action. This comes before TRN-28, TRN-64 and [03](../../spec/03-http-transport.md)'s HTTP-17, which say a call is not sent when its budget or its deadline ends before its request is written, and before TRN-38's rule that a `timeout` a transport produced is passed on as it is.
>
> **TRN-new-r8.** A host MUST stop before it serves when its configuration names a remote provider for a contract version that none of its apps requires, or that one of its apps provides.
>
> So `timeout`, whose outcome is unknown, never moves a call, and neither does an error a provider's host delivered of another kind, such as `unauthenticated` for a credential it refused. A call refused before it is sent is refused for every provider alike. A non-idempotent call makes one attempt, under every policy.

Also in spec 02:

- TRN-17, TRN-65, TRN-66 and the paragraph after TRN-37 are unchanged. TRN-66 (step 5's task 5, with the paragraph "A host learns of a provider in two ways" before it) is worded for `single` alone, and `single` is routed as it is: the app that provides the contract version, otherwise its one remote provider. Under `local-first` the choice is the policy's order (Decision 3), so this record changes no meaning of TRN-66, and D2 and P5 need no withdrawal or restatement for it, as they do for TRN-67. The tests that cite TRN-66 keep citing it.
- TRN-67 (step 5's task 5) reads "or, under the `single` policy, for one that one of its apps provides", as 0014's Consequences fix it. The refusal holds for every policy in spec 0.1 (Decision 3), so TRN-67 is withdrawn, in P5's form ("~~**TRN-67.** ...~~ Withdrawn in spec 0.1: restated for every routing policy as TRN-new-r8."), and TRN-new-r8 states it without those words. The tests that cite TRN-67 cite TRN-new-r8 instead, in the same pull request.
- The decision-record line names this record and 0014, not 0016.

In spec 05, under *Configure* and *Resolve and Validate*, the host's side of the settings:

> **APP-new-r1.** A host MUST route a contract version by `single` unless its configuration names another routing policy for it.
>
> **APP-new-r2.** A host MUST stop before it serves when its configuration lists more than one remote provider for a contract version it routes by `single`, or names `local-first` for one with fewer than two remote providers.
>
> **APP-new-r3.** A host MUST stop before it serves when its configuration names a routing policy that this specification does not define.

Spec 01 is unchanged: CON-4 and the kinds table already state the rule.

In spec 06, item 4 reads: "**Routing.** `single` and `local-first`. Under `local-first`, a call moves to the next provider on `unavailable` and for an idempotent action only, within the call's one deadline, at most once to each entry, with the same principal, trace and idempotency key on every attempt, and never on `timeout` or another kind. Each call starts from the first provider. When a call is not moved after an attempt, it ends with that attempt's outcome. The failover cases run with two providers." Its cases are those of Decision 9's table, cited by name. Spec 06 is the conformance record's document: the step's pull request that completes spec 06 writes this item (#13's plan).

New IDs: TRN-new-r1 to TRN-new-r8 in spec 02 and APP-new-r1 to APP-new-r3 in spec 05. Withdrawn: TRN-67, restated as TRN-new-r8. The pull request that adds each numbers it (P5). The `[not-tested]` table gains nothing: each new ID is covered by a test of Decision 9, a failover case or a unit test, as its table's last column and the unit-test list say. Spec 02 and 05 are enforced, so each new ID is cited by a unit test or a row test in the pull request that adds it; the failover cases, which come later, cite some of them again.

Follows principles 3 (*configuration over code changes*), 5 (*same semantics over same mechanics*), 6 (*visible failure over transparent calls*), 8 (*one test suite over many code paths*), 10 (*Django-native over framework-new*) and 12 (*small core over broad features*), and K2, K5, K6 and K9.

No library is proposed, so K5 is not engaged: the connector's loop is a few lines over the transports step 5 builds. Retry libraries such as tenacity were not weighed further, because the loop has no wait, no backoff and no retry of one provider to hand to one.

## Consequences

- A deployer lists two or more remote providers for a contract version and names `local-first` for it. An idempotent call moves past a provider that answers `unavailable`; a non-idempotent call does not move, whatever the cause. An app author who wants failover for an action declares it idempotent and keeps CON-5's promise.
- A provider that refuses connections or whose name does not resolve costs an idempotent call about one round trip. One that drops packets costs its share of the connect phase, at most 10 seconds (Decision 7's worked numbers).
- A host that crashes or loses power while the caller holds a pooled connection to it (kept up to 1 second, fact 5) leaves a call written into that connection waiting until its deadline, then `timeout`, which never moves. The per-try timeout of open question 2 is what would bound it.
- A rolling restart of the first provider fails a non-idempotent call that races its Drain on a kept-alive connection (fact 8) with `unavailable`, instead of moving it; an idempotent one moves. New connections during Drain are refused, which fails non-idempotent calls the same way. Open question 5 names what would move them.
- A first provider that answers `unavailable` because it is overloaded shifts all of its idempotent traffic to the second at once, with nothing to slow it down; the second must have room for both (open question 6).
- During an outage of the first provider, every call tries it first: a `WARNING` and an `INFO` line per call. Remembered health is open question 1. A deployer who knows a provider is down for long reorders the list and restarts.
- A provider that accepts a call and never answers holds it until the call's deadline, and the next provider is not tried. With no deadline it holds the call indefinitely, as 0014 and 0028 state for `single`.
- In a host that holds an app attached, calls to it never leave the process; redundancy for that app comes from the other hosts' lists (Decision 3), which roadmap step 8's both-at-once example shows. A deployer who writes a remote entry beside the provider in the host is refused with the reason.
- Every provider in a list must accept the calling app's key and admit the url's host in its `ALLOWED_HOSTS`. A provider that does not answers `unauthenticated`, which never moves, or Django's `400`, which is `unavailable`. A credential missing at the second provider is found only when the first fails. 0027 Consequences and its open question 5 already name this; the guide says to check each provider once when it is added.
- Two providers of one app share its database (0004), so an idempotent call moved after it was sent may have run once already; CON-5 makes that the action's promise. Events that both instances publish need the outbox's de-duplication, which 0016 noted and which stays the repository's open question 5.
- The connector holds an ordered tuple of transports per identity in place of one transport, and passes the HTTP transport a connect bound when Decision 5's share applies. The `Transport` protocol gains that optional bound, which the in-process transport ignores; the connector gives a `timeout` after a move `_LATE`'s text. Nothing changes at run time for a step 5 host's settings.
- 0016 is superseded; 0014 and 0028 each gain a `Narrowed by` line; spec 02 and 05 gain the IDs of Decision 10, TRN-67 is withdrawn and restated, and spec 06's item 4 is rewritten by the pull request that completes spec 06. 0002's link to 0016 for "both modes at once" leads on to this record.
- The guide documents `ROUTING_POLICIES`, the move rule with its table, why an identity provided in the host has no remote provider in 0.1, the per-call cost of a provider that is down, the restart and overload cases above, and the log lines.
- The repository's open question 2 (runtime mode switching) gains a pointer to this record's open question 4 in place of 0016.

## Open questions

1. **Remembered health.** Skipping or demoting a provider after failures (Envoy's outlier detection and priority levels, Finagle's failure accrual), with its state owned by the set-up, reset at fork, and a rule for trying the provider again that sends no call twice. It would take a measured cost of the stateless rule on the reference machine (K10), and it replaces TRN-new-r1 with a requirement that says how history changes the order, so that implementations agree.
2. **A per-try timeout, or a connect timeout per entry.** Bounding an attempt that was sent, for idempotent actions, or the connect phase below the 10 second cap for a provider known to be near (Envoy's cluster `connect_timeout` defaults to 5 s). Each is an optional key in an entry's `OPTIONS` or beside the policy, a widening of this record's rows.
3. **Retries to the same provider**, with a backoff inside the deadline and a retry budget (Finagle's `RetryBudget`, gRPC's throttling), which TRN-8 allows and this record does not use.
4. **An in-process attempt that ends with `unavailable`**, for example when runtime mode switching (the repository's open question 2) turns an app off in a running host. The refused row of Decision 3 would then widen. Erlang's `pg` and Kubernetes' traffic distribution prefer a local member by its presence or readiness, not by a per-call `unavailable`, so the later record may need a presence rule as well as the widened row.
5. **Moving a call no provider ran, whatever its action.** Two parts that cover one class. First, an attempt not sent (fact 10; Option 3): spec 02 would need a requirement that a transport reports an attempt as not sent only when it knows that no byte of the request was accepted, and otherwise as sent, with the private flag defaulting to sent. Second, a request a provider's host refused before dispatch, as in Drain (fact 8), `_Handler.setup`'s race, or 0011 open question 8's early refusal: a marker in spec 03, read by the caller as not run, after gRPC A6's case 3, RFC 9113's `REFUSED_STREAM` and `GOAWAY`, Envoy's `refused-stream` and Finagle's nacks. Together they would change CON-4, TRN-8, 0017 Decision 9 and 0028 Decision 5, and could also tell a caller's own code that an `unavailable` was not sent (a change to spec 01's error shape).
6. **Back-pressure on moves**: a provider-side "do not move" signal, after gRPC's `grpc-retry-pushback-ms` (a negative value asks the client not to retry) and `Retry-After` or `x-amz-retry-after`; and a per-process budget for moves after a send.
7. **An attempt number on the wire**, after `grpc-previous-rpc-attempts` and `x-envoy-attempt-count` (Decision 6).
8. **`remote-first`, `weighted` and a policy chosen from the call context**, each with a form, a conformance item and a user.
9. **A distinct kind for a provider without the action** (0011 open question 7), so that version skew does not move an idempotent call.

## References

- Django 5.2.19 and 6.1.3: `contrib/auth/__init__.py` (`authenticate`), `test/signals.py` (`COMPLEX_OVERRIDE_SETTINGS`).
- django-neolith at `origin/release/0.1.0a3`, #128 and #135 merged: `src/neolith/_connector.py` (`Provider._deliver`, `InProcess.call`, `Connector._call`, `_carry`, `_NOT_SENT`, `_LATE`), `src/neolith/_http.py` (`ClientError`, `Phase`, `CONNECT_CAP`, `PER_ADDRESS`, `exchange`, `_exchange`, `_open`, `_send`, `Pools`, `_after_fork`), `src/neolith/_http_transport.py` (`HTTPTransport.call`, `_outcome`, `_fields`, `_warn`), `src/neolith/_server.py` (`Connections.begin`, `Connections.opened`, `Connections.drain`, `_Handler.setup`), `docs/adr/0014-pluggable-connector-transports.md`, `spec/02-transports.md` (TRN-66, TRN-67).
- Records: 0002, 0004, 0008, 0011, 0014, 0016, 0017, 0021, 0022, 0023, 0027, 0028; 0032, the conformance record (its Decision 8). Spec 00, 01 (CON-4, CON-5, CON-44, the kinds table), 02 (TRN-8, TRN-17, TRN-27, TRN-28, TRN-32, TRN-38, TRN-64, TRN-65, TRN-67), 03 (HTTP-17, HTTP-18), 05 (APP-40, APP-41), 06. The roadmap, step 8.
- [gRFC A6, client retries](https://github.com/grpc/proposal/blob/master/A6-client-retries.md): *Transparent Retries*, *Retry Throttling*, *Pushback*, `grpc-previous-rpc-attempts`; [gRPC load balancing](https://github.com/grpc/grpc/blob/master/doc/load-balancing.md) (`pick_first`).
- [Finagle clients guide](https://twitter.github.io/finagle/guide/Clients.html): *Retries* (`RequeueFilter`, `RetryBudget`) and *Failure accrual*; [Finagle glossary](https://twitter.github.io/finagle/guide/Glossary.html) (nack).
- Envoy: [router filter](https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_filters/router_filter) (`retry_on`, `connect-failure`, `reset-before-request`, `refused-stream`, `per_try_timeout`, `x-envoy-attempt-count`); [retry plugins](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/http/http_connection_management#retry-plugin-configuration) (`previous_priorities`, `previous_hosts`); [priority levels](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/load_balancing/priority); [outlier detection](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/outlier).
- HAProxy 2.8 `configuration.txt`: `retry-on`, `option redispatch`, `backup`. Consul [service-resolver](https://developer.hashicorp.com/consul/docs/reference/config-entry/service-resolver) (`Failover`). Erlang [`pg`](https://www.erlang.org/doc/apps/kernel/pg.html) (`get_local_members`). Kubernetes [KEP 3015](https://www.kubernetes.dev/resources/keps/3015/).
- AWS SDKs: [retry behavior](https://docs.aws.amazon.com/sdkref/latest/guide/feature-retry-behavior.html); Smithy [`@idempotencyToken`](https://smithy.io/2.0/spec/behavior-traits.html#idempotencytoken-trait).
- RFC 9110 section 9.2.2; RFC 9113 section 8.7.
- Go `net/http`, `src/net/http/transport.go` (`shouldRetryRequest`, `nothingWrittenError`); `src/net/dial.go` (`partialDeadline`).
