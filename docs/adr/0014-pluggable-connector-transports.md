# 0014. Connector transports are pluggable, and a host names each remote provider by contract in `NEOLITH["REMOTE_PROVIDERS"]`

- **Status:** Accepted
- **Accepted:** 2026-11-18
- **Date:** 2026-11-07; amended 2026-11-18 for roadmap step 5 (#12)

## Context

The design describes two ways to reach a contract: a local call when attached, and REST when detached. REST is a reasonable default, but it is not the only fit. Some deployments want gRPC for efficient service-to-service calls. Others want calls to travel over a message queue. Wiring HTTP into the connector would make every other protocol a fork.

Seen this way, attached mode is not special. It is one more way of carrying a call: in-process.

Roadmap step 5 (#12) adds the HTTP transport, so a host must now be able to reach a contract that no app in the host provides. Step 4 left four questions about that to this step:

- **Where the host learns that a provider is remote.** Spec 05's Resolve finds "an app in this host that `provides` it, or a configured remote provider". [0021](0021-an-app-declares-itself-on-its-neolithappconfig.md) refuses a `requires` entry that no hosted app provides, "in step 4", and its open question 2 leaves remote providers to step 5. [0022](0022-the-app-host-builds-settings-and-checks-before-it-serves.md)'s builder row for a `requires` entry says "Remote providers arrive in step 5 (#12)", and its Decision 3 says that each later record adding a `NEOLITH` key adds one row in one form.
- **How a connector picks its transport** under `single`, the one routing policy step 3 built (spec 02, TRN-17).
- **What Boot checks for a remote provider**, given that 0022 Decision 7 has Boot read each schema and stop before Serve (APP-1, K2).
- **What waits for step 6:** `local-first`, failover, and an app attached and detached at once ([0016](0016-an-app-may-run-attached-and-detached-at-once.md), Proposed).

Facts this record rests on:

1. **Mode is configuration, never app code.** Spec 00 defines mode as "a property of the deployment, never of the app's code", and OVR-5 says that changing mode MUST NOT require any change to the app or its callers. A declaration is app code: it is class attributes of the app's `NeolithAppConfig` (0021).
2. **A caller holds contract packages, not the providing app.** OVR-2: a caller MUST NOT depend on anything else of the providing app. A calling host therefore cannot read which app provides a contract elsewhere, or which other contracts that app provides.
3. **A connector holds a contract and finds its provider by identity.** Spec 02: "A connector holds an ordered set of providers for a contract version. Each provider names a transport and the configuration that transport needs." In the code at 0.1.0a2, `neolith._host.build` builds one `Connector` per provided identity with an `InProcess` transport, keeps it in the set-up's `Host.connectors`, and `connect()` finds it by identity on each call (`src/neolith/_host.py`, `build` and `_connector`). The `Transport` protocol is private: `call(action, request, context) -> bytes | None`, raising `Failure` (`src/neolith/_connector.py`). There is no `neolith.transports` module; private modules carry a leading underscore.
4. **Django opens database connections lazily.** A connection is opened by `ensure_connection()`, which `_cursor()` calls on first use (`db/backends/base/base.py`, lines 271 and 294 in 5.2.19 and 6.1.3); set-up and settings never connect. 0022's Boot is the one place the host itself reads a database before Serve, and it reads only the app's own data.
5. **A live server serves this process's own host, at a URL known only after set-up.** `LiveServerThread` serves `WSGIHandler()` of the running process, with its settings and its apps (`test/testcases.py`, line 1761 in 6.1.3). `LiveServerTestCase` binds port 0 by default and builds `live_server_url` as `"http://%s:%s" % (cls.host, cls.server_thread.port)`, with `host = "localhost"`, once `setUpClass` has started the thread, and appends that host to `ALLOWED_HOSTS` (lines 1794 and 1833 in 6.1.3; 1787 in 5.2.19). Apps are loaded, and the host set up, long before that. So a live server in one process can answer a contract path only for an app the host provides itself.
6. **`urllib.parse.urlsplit` is lenient.** Probe, on Python 3.12.13 and 3.13.5: it lower-cases the scheme, strips leading spaces, drops a tab inside the host, accepts port `0`, a port written `08000`, an empty port (`https://x:`), userinfo, and a non-ASCII host. It cannot be the definition of a URL with one form. `ipaddress.IPv4Address` refuses leading zeros, `str(IPv6Address(...))` gives the compressed lower-case form, and `IPv6Address` accepts a zone id (`fe80::1%eth0`) (probe, both versions).
7. **The system resolver reads more names as IPv4 addresses than dotted decimal.** Probe, on Python 3.12.13, 3.13.5 and 3.15.0b1 with glibc: `socket.getaddrinfo("0x7f.1", 80)` and `getaddrinfo("0x7f.0.0.1", 80)` both give `127.0.0.1`, and `"1.0x2"` gives `1.0.0.2`, through `inet_aton`. The WHATWG URL Standard has the matching rule: a host whose last label is a number ("ends in a number") is parsed as IPv4. On this machine (`hosts: files mdns4_minimal [NOTFOUND=return] dns`), `getaddrinfo("foo.localhost")` goes to DNS and fails; RFC 6761 section 6.3's loopback answer for `*.localhost` depends on the resolver, and a search domain can be appended to such a name.
8. **Django refuses a Host with an underscore.** `host_validation_re` is `^([a-z0-9.-]+|\[[a-f0-9]*:[a-f0-9.:]+\])(?::([0-9]+))?$` (`http/request.py`, line 35 in 6.1.3), so `get_host()` raises `DisallowedHost` for `inventory_svc:8000` even with `ALLOWED_HOSTS = ["*"]`. Django answers it with a 400 that carries no neolith error body, which spec 03 maps to `unavailable`.
9. **Django rebuilds some handlers on `setting_changed` and keeps others fixed.** `test/signals.py`: the receiver `clear_cache_handlers` resets the cache handlers when `CACHES` changes, while `COMPLEX_OVERRIDE_SETTINGS = {"DATABASES"}` warns when `DATABASES` is overridden (5.2.19 and 6.1.3).
10. **Python and the URL Standard write an IPv4-mapped IPv6 address differently.** Probe, Python 3.13.5 and Node 24: `str(ipaddress.IPv6Address("::ffff:7f00:1"))` is `::ffff:127.0.0.1`, while `new URL("http://[::ffff:127.0.0.1]").origin` is `http://[::ffff:7f00:1]`. For `::7f00:1` and `64:ff9b::7f00:1` the two agree; they differ only on `::ffff:0:0/96`, where `IPv6Address(h).ipv4_mapped` is not `None`.
11. **The URL Standard decodes a label that begins with `xn--`, and refuses one that is not valid IDNA.** RFC 5890, section 2.3.1, calls such a label an XN-label: an A-label when it is valid IDNA, a Fake A-label otherwise. Probe, Node 24.14.1: `new URL()` refuses `http://xn--zz`, `http://xn--a`, `http://xn--a-ecp.example` and `http://xn--ab--c.example`; the origins of `http://xn--mnchen-3ya.example` and `http://xn--zca` are those strings; and `http://münchen.example` has the origin `http://xn--mnchen-3ya.example`. Python's `idna` codec implements IDNA 2003, not the UTS 46 processing the URL Standard uses: on 3.13.5, decoding `xn--zca` (ß) fails because IDNA 2003 maps ß to `ss`, while `xn--mnchen-3ya` decodes. An ASCII host is passed on without any IDNA check by Go's `net/http` (`idnaASCII`, `src/net/http/request.go`, lines 781 to 795 in Go 1.25.0) and by Kubernetes' name rule (`IsDNS1123Label`, `[a-z0-9]([-a-z0-9]*[a-z0-9])?`, `pkg/util/validation/validation.go` in apimachinery v0.34.0); DNS itself compares labels as octets.

Assumptions about the other records of step 5, none of which this record decides, and none of which is Accepted:

- **A1. Provider endpoints ([0011](0011-the-host-serves-contract-endpoints-itself.md), as amended for step 5).** Contract paths (`POST /<name>/v<N>/<action>`) and the well-known paths of spec 03 sit at the root of the provider host's origin, and owned paths check the Host with `get_host()`, so a Host Django refuses gets its 400 (fact 8). The provider's host owns the whole prefix `/<name>/v<N>/` of each version it provides, so a path there that names no action gets 404 with no neolith error body, while a path under a version it does not provide reaches its deployer's own URLs and middleware. If that record admits a path prefix, the `url` row below widens to admit a path that ends in `/`, which is a compatible change.
- **A2. The HTTP client (0028).** The client sends each call at most once (TRN-17), opens no connection when it is built, and bounds connection establishment so that a provider that cannot be reached fails with `unavailable` also on a call that has no deadline. `url` is the only input that names a call's destination: the client reads no proxy or other environment variable that changes it and follows no redirect (a `3xx` without a neolith error body reaches the caller as `unavailable`). It keeps one connection pool per origin, so identities whose entries have the same `url` share connections. It is synchronous, and a call from async code behaves as that record states. Its TLS trust settings, and whether it retries a connection attempt that sent nothing, are its decisions.
- **A3. Identity on an HTTP call (0027).** The shared-secret keys that a calling app signs its tokens with are configured per calling app, not per provider (`SIGNING_SECRETS` at the caller and `CALLER_BACKENDS` at the provider). A host that has a remote provider for a contract that one of its apps requires, and no key for that app, is refused before Serve by 0027's row. A call with no calling app is refused by the caller before it is sent (0027 Decision 9).

## Options

1. **HTTP only.** One remote path, so it is simple to build and test. Any other protocol needs changes to the core.
2. **A pluggable transport (strategy pattern).** A connector delegates each call to a transport chosen in configuration. In-process and HTTP ship with the core. Others are plugins.
3. **gRPC as the default remote transport.** Efficient and strongly typed, but less familiar to Django teams, and harder to call from a browser or with curl.

## Decision

Option 2, as the general decisions below state, and for step 5 in the admitted set and Decisions 1 to 7 after them.

- A connector delegates to a **transport**, chosen per provider in configuration. The calling code does not change when the transport does.
- The core ships two transports. **In-process** is attached mode, and serializes per [0007](0007-in-process-transport-serializes.md). **HTTP/JSON** is the default for detached apps, described by OpenAPI.
- Other transports, such as gRPC or a message queue, can be added as separate packages behind the same interface, once a later record makes that interface public (Decision 7).
- Contracts stay transport-neutral. They are defined once in Python types, and each transport projects them into its own schema: OpenAPI for HTTP, protobuf for gRPC.
- Every transport must pass the same contract test suite. This is principle 8 (*one test suite over many code paths*) applied to every transport.

GraphQL is not planned as a transport. Contracts are named actions, while GraphQL is a query language over a data graph. A GraphQL API over several apps' contracts fits better as a gateway in front of them than as a way for apps to call each other.

### The admitted set

This record adds one key to 0022's table of `NEOLITH` keys, with one form. Anything else stops the host before Serve with `ImproperlyConfigured`, whose message names the input, as 0022's table does. Every container is exactly the type named, a `dict` or a `list`; a subclass or a tuple is refused with the row's first message, which follows 0022's form `<input>: expected <what the row admits>, got <type found>`. No message of this record prints a `url` value that was refused (Decision 3).

```
REMOTE_PROVIDERS = dict of 1 or more  contract-id: providers     ; a key of NEOLITH; absent means none
contract-id      = str, CON-10's rule, as connect() takes it
providers        = list of exactly 1 entry                        ; single routing; Decision 2
entry            = dict with exactly the keys "BACKEND" and "OPTIONS"
BACKEND          = "neolith.transports.HTTPTransport"             ; the one value in 0.1; Decision 4
OPTIONS          = dict with exactly the key "url"
url              = str: scheme "://" host [":" port]              ; an origin, no path; Decision 3
scheme           = "http" / "https"
host             = reg-name / IPv4address / "[" IPv6address "]"
reg-name         = label *("." label)                             ; at most 253 characters, no trailing dot;
                                                                  ;   its last label is not a number
label            = (lower / DIGIT) [*61(lower / DIGIT / "-") (lower / DIGIT)]   ; RFC 1123
number           = 1*DIGIT / "0x" *HEXDIG                          ; WHATWG URL's "ends in a number"
IPv4address      = dotted decimal, four parts, no leading zero    ; a host whose last label is a number is read as one
IPv6address      = the compressed lower-case form, no zone id,    ; str(ipaddress.IPv6Address(h)) == h and
                   not IPv4-mapped                                 ;   IPv6Address(h).ipv4_mapped is None
port             = %x31-39 *4DIGIT, at most 65535, not the scheme's default (80, 443)
```

Put another way: an admitted `url` is exactly the ASCII serialisation of its own origin ([RFC 6454](https://www.rfc-editor.org/rfc/rfc6454), section 6.2, with the WHATWG URL Standard's host serialisation), restricted to `http` and `https`, to host names of RFC 1123 labels, and to hosts with no zone id, no trailing dot and no IPv4-mapped IPv6 address. That last restriction is what makes the two statements agree: Python and the URL Standard write a mapped address differently (fact 10), and an IPv4-mapped address is written as its IPv4 address instead. One clarification bounds the equivalence: a label that begins with `xn--` is taken as written, never decoded or encoded again, as DNS, Go's `net/http` and Kubernetes take an ASCII name (fact 11). An A-label, such as `xn--mnchen-3ya`, keeps its spelling under the URL Standard, so the equivalence holds for it, and the same name written in Unicode is refused as not ASCII, so each origin still has one spelling. A Fake A-label, such as `xn--zz`, makes a host the URL Standard refuses, so that url has no origin there; it is admitted all the same, and fails at the first call as a misspelt host name does (Decision 5). The grammar above is the exact statement of what is admitted.

For example:

```python
NEOLITH = {
    "REMOTE_PROVIDERS": {
        "<contract>.v1": [
            {
                "BACKEND": "neolith.transports.HTTPTransport",
                "OPTIONS": {"url": "https://<host>:8443"},
            },
        ],
    },
}
```

**The builder's rows** (0022 Decision 1; the shape rows read nothing but the value):

| Input | Admitted | Refused, with its message |
|---|---|---|
| `NEOLITH["REMOTE_PROVIDERS"]` | Absent, or a `dict` with one or more keys. | `NEOLITH["REMOTE_PROVIDERS"]: expected a dict of contract identities, got <type>`; `NEOLITH["REMOTE_PROVIDERS"]: is empty; leave the key out` (0022's row for an empty key). |
| A key | A `str` that matches `contract-id`. | `NEOLITH["REMOTE_PROVIDERS"]: key <repr>: expected a contract identity, such as <contract>.v1`. |
| A value | A `list` of exactly one entry. | `NEOLITH["REMOTE_PROVIDERS"]["<id>"]: expected a list of providers, got <type>`; `...: is empty; leave <id> out`; `...: lists <n> providers; with the single routing policy a contract has one provider, and routing between several arrives with step 6 (ADR 0016)`. |
| An entry | A `dict` with exactly `BACKEND` and `OPTIONS`. | `NEOLITH["REMOTE_PROVIDERS"]["<id>"][0]: expected a dict with the keys BACKEND and OPTIONS, got <type>`; `...: missing key <KEY>`; `...: unknown key <repr>; the keys are BACKEND, OPTIONS`. |
| `BACKEND` | Exactly `"neolith.transports.HTTPTransport"`, compared as written; nothing is imported. | `...["BACKEND"]: expected a str, got <type>`; `...["BACKEND"]: <value> is not a transport of this host; the one remote transport in 0.1 is neolith.transports.HTTPTransport`. |
| `OPTIONS` | A `dict` with exactly the key `url`. | `...["OPTIONS"]: expected a dict, got <type>`; `...["OPTIONS"]: missing key url`; `...["OPTIONS"]: unknown key <repr>; the HTTP transport's options are url`. |
| `url` | A `str` that matches `url`. | `...["OPTIONS"]["url"]: expected a str, got <type>`, and otherwise `...["OPTIONS"]["url"]: is not an origin: <reason>`, which prints no part of the value. The reason is the first of: `the scheme is http or https, in lower case`; `it carries a user or a password; a remote provider's URL holds no credential`; `it has a path, a query or a fragment; contract paths sit at the root of the provider's origin (spec 03)`; `the host is not in lower-case ASCII; write a name in its ASCII form`; `a host name is labels of letters, digits and hyphens joined by dots, each 1 to 63 characters, neither starting nor ending with a hyphen, at most 253 in all, with no trailing dot; a provider refuses any other Host`; `the host ends in a number, so it is read as an IPv4 address; write the address in dotted decimal, without leading zeros`; `the host is not a valid IPv6 address, or is not in its compressed form, or has a zone, or is IPv4-mapped; write an IPv4-mapped address as its IPv4 address`; `the port is 1 to 65535, without a leading zero`; `leave out the scheme's default port (80 for http, 443 for https)`. Every reason is fixed text. A value with a space or a control character anywhere is refused with the first reason that applies to where it sits. |

**Rows that need the declarations** (Resolve, in the builder and again at set-up, 0021 Decision 8; refusals reported together, sorted):

| Input | Admitted | Refused, with its message |
|---|---|---|
| An identity in `REMOTE_PROVIDERS` | Required by at least one hosted app, and provided by none. | `NEOLITH["REMOTE_PROVIDERS"]: <id> is required by no hosted app; remove it`; `NEOLITH["REMOTE_PROVIDERS"]: <id> is provided by app <label> in this host; a contract provided in the host and remotely at once needs a routing policy other than single, which arrives with step 6 (ADR 0016)`. |
| A `requires` entry (0021's row, 0022's builder row) | Provided by a hosted app, or with an entry in `REMOTE_PROVIDERS`; never both, by the row above. | 0021's Resolve refusal, a `DeclarationError`, whose message becomes: `app <label>: requires: <id> has no provider; no app of this host provides it and NEOLITH["REMOTE_PROVIDERS"] has no entry for it`. |

**At set-up, as a system check** (0022 Decision 5; the id is a working number, fixed by the PR that adds it):

| Input | Admitted | Message |
|---|---|---|
| A `url` with the scheme `http` | Admitted. With a warning unless the host is `localhost` or an address for which `ipaddress` gives `is_loopback`. | `neolith.W002`: `NEOLITH["REMOTE_PROVIDERS"]["<id>"]: <url> is plain HTTP to a host that is not loopback; anyone who can watch the network can read and replay the calling app's token (spec 03)`. |

**At Boot** (0022 Decision 7): nothing for a remote provider. Decision 5 says why.

**Under `override_settings`** (0022 Decision 3): `REMOTE_PROVIDERS` cannot change after set-up. A change is refused with 0022's message, `NEOLITH["REMOTE_PROVIDERS"]: cannot change after set-up`. Decision 6 says why, and how a test sends a call over HTTP instead.

### 1. A remote provider is deployment configuration, keyed by contract identity, in one `NEOLITH` key

`NEOLITH["REMOTE_PROVIDERS"]` maps each contract identity a hosted app requires and no hosted app provides to the provider that serves it. The key holds remote providers only. A provider in the host is never listed, because the host derives it from the declarations, and APP-28 refuses deployer input that states what the host derives.

The key is the identity string that `connect()` takes. The entry names no remote app, and no contract class: the host already holds the class, through the `requires` entry that the identity must match (0021 Decision 4).

The host reads only `settings.NEOLITH`, never the environment. Spec 05's Configure builds the configuration "from the declarations plus environment"; in a Django host the settings module is where the environment is read, as Django projects read `DATABASES` from a `DATABASE_URL` there and Django itself reads no such variable. A deployer who keeps a provider's URL in the environment reads it once in the settings module and reuses it for each identity that host serves:

```python
INVENTORY = os.environ["INVENTORY_URL"]
NEOLITH = {
    "REMOTE_PROVIDERS": {
        "inventory.v1": [{"BACKEND": "neolith.transports.HTTPTransport", "OPTIONS": {"url": INVENTORY}}],
        "pricing.v2": [{"BACKEND": "neolith.transports.HTTPTransport", "OPTIONS": {"url": INVENTORY}}],
    },
}
```

Alternatives weighed:

- **A field of the caller's declaration**, such as a `Remote(<Contract>, url=...)` entry in `requires`. It sits beside the requirement it satisfies. The declaration is app code, and spec 00 makes mode a property of the deployment, never of the code; OVR-5 forbids a change to callers when a provider changes mode. A URL also differs per environment, which Django keeps in settings.
- **Keyed by the remote app**, as `{"<app>": {"url": ..., "provides": [...]}}`. One URL per remote host instead of one per contract, and closer to 0002's "mode chosen per app". Encore's `service_discovery`, Dapr's app-id and Spring Cloud's simple discovery client all key a remote address by the remote service. The calling host cannot read which app provides a contract without depending on that app (OVR-2), so the entry would have to restate the remote app's `provides`, a second copy of another app's declaration that nothing here could check.
- **A group the caller names**, as `{"<group>": {"url": ..., "contracts": ["<id>", ...]}}`, the form of Spring Boot's HTTP service client groups (`spring.http.serviceclient.<group>.base-url`, with the interfaces the caller assigns to the group). It needs no copy of the provider's `provides`, since the caller lists only what it requires. It saves one URL per remote host, one environment variable per host, and later one TLS or timeout block per host rather than per identity. It costs a second level of nesting, a check that no identity sits in two groups, and a name that appears nowhere else in neolith: Resolve, `connect()`, 0016's routing policies and the error messages all speak of identities, so every message would have to translate the group back to them. The identity is already the unit of Resolve and of step 6's routing, and a settings variable gives the one-URL saving without a new name. OpenFeign's per-client `spring.cloud.openfeign.client.config.<name>.url`, one entry per interface, is the precedent for the form chosen. If per-host options arrive (open question 1), the group form is the one to weigh again.
- **A list of remote hosts, with the host asking each for its OpenAPI document at Boot to learn what it serves.** Nothing to restate. It makes the calling host's start depend on the network (Decision 5), and the routing table would differ between two starts of the same settings.
- **A separate Django setting**, such as `NEOLITH_REMOTE_PROVIDERS`. The roadmap's public surface puts providers, transports and routing under the one `NEOLITH` key, and 0022 Decision 3 keeps one key table and refuses unknown keys there.
- **The name `PROVIDERS`.** Shorter. It reads as the list of every provider, which APP-28 forbids the deployer to write. `REMOTE_PROVIDERS` uses spec 05's words for what the key holds, "a configured remote provider".

The cost: a remote host that serves several contracts has its URL written once per identity. A settings module holds it in one variable, as above.

### 2. Under `single`, the host chooses each contract's one transport at set-up, by one rule

For each identity the host knows (0021 Decision 4):

- a hosted app provides it: the in-process transport to that app's implementation, as in step 4;
- otherwise `REMOTE_PROVIDERS` has an entry for it: the HTTP transport to that entry's `url`;
- otherwise Resolve refuses the `requires` entry, as the table says.

The choice is made once per set-up, in `build`, and kept in the set-up's table (0021 Decision 8). A connector looks the transport up by identity on each call (0021 Decision 10), so a connector never picks per call and never changes transport between attempts: under `single` there is one attempt (TRN-17). The host builds one HTTP transport per identity; connections belong to the client's pool for the origin, so identities on one origin share them (A2). The connector runs the caller's checks of 0017 Decision 9 before the transport in both cases, so the in-process and HTTP paths differ only after step 4 of that order.

A call from a scope that belongs to no app is not sent over HTTP: the HTTP transport refuses it before sending, with `unauthenticated` and the in-process message (TRN-42, 0021 Decision 9, 0027 Decision 9), so in-process and over HTTP give the same kind and message, and a principal it carries (TRN-56 and TRN-58 name request roots that can have one) never crosses the network. A call from an app with no signing key is refused the same way, and before Serve when a hosted app requires an identity listed here (A3).

An identity both provided in the host and named in `REMOTE_PROVIDERS` is refused in step 5, as the table says. APP-9, one providing app per identity in a host, is unchanged: 0021's open question 2 asked whether it becomes "one in-host provider" under 0016, and the answer for 0.1 is that it stays as it is, since a remote provider is not an app the host holds.

The value is a list of exactly one entry. Spec 02 defines the connector's providers as an ordered set, and 0016 asks that the interface admit more than one from the start. Admitting more entries later widens the row; a single entry now that became a list later would be a second form.

Alternatives weighed:

- **A value that is one entry, not a list.** One pair of brackets fewer. Step 6's `local-first` needs only one remote provider beside the in-host one, so the list would carry one entry through 0.1. Any policy after it that holds two remote providers, such as failover between them or `weighted`, would then need a second form of the value, or a breaking change.
- **A routing-policy key now, admitting only `"single"`.** It makes the policy visible in settings. It is a second spelling of what absence already says, and step 6 has to decide the policy's form with `local-first` in hand. This record fixes only that the absence of a policy means `single`.
- **Choosing per call, with the in-host provider preferred when it exists.** That is `local-first`, which spec 02 defines with a fallback rule that step 6 builds and tests (TRN-8). Under `single` a call has one provider.

### 3. The HTTP transport's one option is the provider's origin

`OPTIONS["url"]` is an origin: a scheme, a host and an optional port, with no path, no query, no fragment and no credential. Contract paths and the well-known paths of spec 03 are resolved against it (A1). An admitted value is exactly the ASCII serialisation of its own origin (RFC 6454, section 6.2; the admitted set, with its clarification for a label that begins with `xn--`), so each origin has one spelling: a lower-case scheme and host, no default port, the compressed form of an IPv6 address that is not IPv4-mapped, and an IPv4 address only in dotted decimal. A host name whose last label is a number is read as IPv4, as WHATWG URL and the system resolver read it (fact 7), so `0x7f.1` is not a second spelling of `127.0.0.1`. A label that begins with `xn--` is taken as written, as the admitted set's clarification says: a valid A-label works with no IDNA code in the host, and a Fake A-label is admitted and found at the first call (fact 11). The host writes the check from that grammar and the standard library's `ipaddress`; it does not use `urlsplit`, which admits several spellings of one URL (fact 6). The check adds no dependency (K5).

A host name holds RFC 1123 labels: letters, digits and hyphens. A name with an underscore can be resolved, but a provider served through Django refuses every request whose Host holds one, whatever `ALLOWED_HOSTS` says (fact 8), so such a url would pass set-up and then fail every call with `unavailable`. For the same reason the provider's `ALLOWED_HOSTS` has to admit the host written in the caller's url.

A refused `url` is never printed, in whole or in part. A url can carry a credential in its userinfo, its query or its path, and set-up's refusals reach server logs and tracebacks: set-up runs from `ready()` under every server (0022 Decision 5), and the host logs a refusal before it raises it (`_refuse` in `src/neolith/_host.py`). The message names the setting's path and the rule, which is enough to find the value and say what to write. This is the class rule for this record: no message, log line or check prints a value that could hold a secret; `neolith.W002` prints an admitted url, which by the grammar carries none.

`http` is admitted, with `neolith.W002` when the host is not loopback. A Django live server is `http://localhost:<port>` (fact 5), and spec 03 says development set-ups often use plain HTTP on a private network. The warning can be silenced, since 0022 refuses to silence only `neolith.E` ids, so a development network that accepts the risk says so in its settings. Loopback means `localhost` or an address that `ipaddress` reports as loopback. A name under `.localhost` is not exempt: RFC 6761 section 6.3 asks resolvers to answer it with loopback, but a plain system resolver may send it to DNS, with a search domain appended (fact 7), so the warning would be lost exactly where the token might leave the machine.

Alternatives weighed:

- **A base URL with a path**, such as `https://<gateway>/<app>/`. It admits a provider behind a gateway that routes by path. Spec 03 places the well-known paths at the origin's root, as RFC 8615 places well-known URIs, so a path prefix is 0011's decision (A1). Starting with the origin and widening later is compatible; the reverse is not.
- **Refusing `http` outside loopback.** It enforces spec 03's TLS rule for bearer tokens. A container network that calls other containers by name over plain HTTP, the common development set-up spec 03 describes, would be refused. The shared-secret backend is for development only (0010 Decision 5), and production authentication is the repository's open question 7.
- **Admitting any URL that `urlsplit` accepts and normalising it**, as Dapr's `baseUrl`, Encore's `base_url`, Spring's `base-url` and Django's `CSRF_TRUSTED_ORIGINS` (whose check 4_0.E001 asks only for a scheme) do. Kinder to a deployer who writes `HTTPS://` or a trailing `/`. The host would then compare and print a value the deployer did not write, and an upper-case scheme, a tab inside a host and a port written `08000` would all be admitted (fact 6). This record is stricter than those precedents on purpose: one spelling per origin makes two entries for one provider visibly the same string.
- **Refusing every label that begins with `xn--`.** The equivalence with the URL Standard would then hold for every admitted url. It shuts out every internationalised host name, which can be written here only as its A-label.
- **Checking an `xn--` label with Python's `idna` codec.** No dependency. The codec is IDNA 2003, which the URL Standard does not follow, so it refuses valid names such as `xn--zca` (fact 11).
- **Checking it with a UTS 46 library**, as the URL Standard's host parser does. Exact, at the cost of a runtime dependency for a check whose failure the first call already reports (K5).
- **Credentials in the URL or the entry.** The key would then carry secrets that `diffsettings` and error messages print. Signing keys are the calling app's, and A3 places them.
- **A per-provider `timeout` option**, a read timeout for each remote provider, as Spring's per-group `read-timeout` offers. It would bound a provider that accepts the connection and never replies on a call with no deadline. It would be a second deadline beside the call's own (TRN-27), with its own rule for which wins, and a call's deadline already comes from its scope, its `timeout=` or `REQUEST_DEADLINE` (0022). The consequence is stated below.

The cost: a deployer who writes a trailing slash, an upper-case host, an underscore in a name or `:443` on `https` is refused, by a message that says what rule the value breaks; one who writes a Fake A-label learns it at the first call.

### 4. Every entry names its transport under `BACKEND`, and in 0.1 the one admitted value is neolith's HTTP transport

`BACKEND` is required, and `OPTIONS` holds the transport's own options, as `PRINCIPAL_BACKENDS` entries hold `BACKEND` and `OPTIONS` ([0023](0023-a-request-principal-comes-from-ordered-backends.md) Decision 4) and as Django's `STORAGES`, `TEMPLATES` and `CACHES` entries do. Every pluggable entry in `NEOLITH` then has the same two keys, so a deployer who copies one shape into another is not refused for the key's name. The documentation of the key says that a remote provider's backend is its transport, spec 02's word. "Chosen per provider in configuration", in this record's first decision, is then literal: the setting names the transport.

The url sits in `OPTIONS`, not beside `BACKEND` as `CACHES` keeps `LOCATION` and as Dapr and Encore keep the base URL at the top of an entry. A later transport, such as a message queue, may have no URL, so the locator belongs to the transport's own options.

`neolith.transports` ships in step 5 with one public name, `HTTPTransport`. The builder compares the `BACKEND` string as written and imports nothing, so the row means the same whether or not the module is imported. The class's constructor and methods are private in 0.1, and the documentation says it is a name for the setting, not a class an adopter builds or subclasses (Decision 7). The dotted path then names something real, as every dotted path in Django settings does, and a later record that opens the interface can import it without changing any adopter's settings.

Alternatives weighed:

- **The key name `TRANSPORT`.** It uses spec 02's word for what the entry names. It would be the one entry form in `NEOLITH` whose class key is not `BACKEND`, beside 0023's `PRINCIPAL_BACKENDS` and 0027's `CALLER_BACKENDS` (A3), and Django's own `STORAGES`, `TEMPLATES` and `CACHES`.
- **No `BACKEND` key in 0.1, since HTTP is the only remote transport.** One line less per entry. A second transport would then need the key: required, a breaking change to every adopter's settings; or optional with HTTP as the default, two spellings of HTTP.
- **A short name, such as `"http"`.** Shorter. Django names every pluggable class in settings by its dotted path, and 0023 does too; a short name needs a table in the core from names to classes.
- **Any dotted path to a transport class.** It opens 0014's plug-in point now. It also makes the private `Transport` protocol public API in step 5, with its typing (K1) and the conformance suite that every transport must pass, which arrives in step 6 (Decision 7).
- **A dotted path that names no module**, compared as a string only. Nothing new ships. A path in a Django setting that does not import would surprise a reader, and opening the interface later would have to create the module the settings already name.

The cost: every entry carries the same `BACKEND` line until a second transport exists, and `neolith.transports` is public surface whose only content in 0.1 is a name.

### 5. A host does not reach a remote provider before it serves, and does not stop when it cannot

Set-up checks every row of the admitted set, and Boot adds nothing for a remote provider: no connection, no DNS lookup, no request to the health or OpenAPI path. The HTTP transport is built at set-up and opens nothing then (A2), so a server that forks after loading apps shares no socket, as 0022 Decision 7 says of Boot's database connections. A call to a provider that cannot be reached fails at the caller with `unavailable`, or with `timeout` when its deadline passes first (TRN-3, TRN-4, [0008](0008-typed-contract-errors-in-both-modes.md) Decision 2), as gRPC's fail-fast default fails an RPC at once with `UNAVAILABLE` on a channel that cannot connect. A call that reaches a host which does not serve the contract version, or the action, gets a response that is not on a contract path there. It fails with `unavailable`, or with `internal` when whatever answers there returns a success status (spec 03, Errors): a provider that has the version but not the action answers 404 (A1), while a host without the version, or another server behind a wrong port, answers as its own URLs do, and a catch-all view's 200 page is `internal`. A body that is a JSON object with `kind` written as a string is read as a neolith error body whatever the status (spec 03, HTTP-19). So whatever answers there can also give `internal`, as a 404 with Kubernetes's `{"kind": "Status", …}` does, or the kind the body names, as `unauthenticated` from another neolith host that holds no key for the caller. A provider that accepts the connection and never replies holds the call until the call's deadline, and indefinitely when it has none (Consequences).

This is an exception to K2 for state outside the host's configuration. The static rows meet K2 for what the settings say: a contract requirement is satisfied when Resolve finds a provider, in the host or configured, and a value outside the admitted set is refused by name before Serve. A misspelt host name, a wrong port or a provider on the wrong contract version is also misconfiguration in K2's sense, and it is found at the first call, usually as `unavailable` or `internal`, and otherwise as the kind whatever answers names (Decision 5). The pull request that proposes this amendment states this exception for agreement in writing, as CONTRIBUTING's "How review works" asks of a rule that a change does not meet.

Alternatives weighed:

- **Boot asks each remote provider's health path and stops when one does not answer.** A wrong host name would be found at the start and not at the first call, which K2 favours. Two detached apps that each require a contract of the other could then never start after both had stopped: each Boot would wait for the other. A provider that is down for a deployment would also keep its callers from restarting, so one app's outage would spread to every app that calls it. 0002 makes the app the unit of deployment, and a start order between apps would undo that. Spring Boot gives the same advice for liveness: it "should not be based on external checks", since a failing external system "would trigger massive restarts and cascading failures across the platform".
- **Boot asks, and only warns.** The host serves either way, so the result is a log line about a moment that has passed by the first call. A readiness signal that tracks the provider is the orchestrator's, through the provider's own health path.
- **Boot resolves each host name.** Cheaper than a request. A successful lookup proves little: a Kubernetes Service name resolves to its cluster IP while no pod behind it is ready, and a short name depends on the namespace's search path, while a headless Service or a Compose name may not resolve until a container is up. The check would therefore both pass hosts that cannot be served and refuse hosts that would have worked.
- **Boot fetches each provider's OpenAPI document and compares it with the contract package.** It would catch a provider on an older version of the contract, which within one major version can lack an action the caller's package has (CON-1). Deciding which differences are compatible is breaking-change detection, which the roadmap defers past 0.1, and it has the start-order cost above. Open question 2 keeps a check run before deployment.

The precedent is Django's own split: the host reads its own app's schema at Boot, because that data is part of the app (0004), and Django opens even that database lazily otherwise (fact 4). Another app is not part of this one; 12-factor treats it as an attached resource that the configuration names and that can be swapped without a code change. gRPC's connectivity semantics likewise describe a channel with no RPCs as "not even trying to create a connection", and leave it to the first RPC to start connecting.

The cost: a misspelt host name, a wrong port or a provider on the wrong contract version is found at the first call, usually as `unavailable` or `internal`, and not at the start. Open question 2 is the way to find it before deployment.

### 6. `REMOTE_PROVIDERS` is fixed after set-up, and a test sends a call over HTTP through a test seam

`REMOTE_PROVIDERS` cannot change under `override_settings`, as the hosted apps and `DATABASES` cannot (0022 Decision 3). A test that needs a call to go over HTTP uses a test seam instead: `over_http(identity, origin)`, a context manager, private in step 5. For as long as it is entered, calls for that identity go through the HTTP transport to `origin`, whether a hosted app provides the identity or a `REMOTE_PROVIDERS` entry names it; on exit the set-up's transport is back. It refuses, with `UsageError`, an identity the host does not know and an origin outside the `url` grammar, and an identity required by an app with no signing key in 0027's `SIGNING_SECRETS` (A3), since set-up cannot see the seam. It runs no system check, so `neolith.W002` does not apply to it; tests reach only the local machine (T3). No setting turns it on, so, like `neolith.testing.boot()` (0022 Decision 7), it is a line of code a review can find.

*Why:* the contract suite over HTTP needs neolith's provider side as well as its caller. A live server in one process serves this process's own host (fact 5), so its providing app is hosted there, and the Resolve row then refuses a `REMOTE_PROVIDERS` entry for that identity. No settings path can carry that case. The seam can: a test host holds both apps, enters `over_http(<id>, live_server_url)`, and each call crosses the network to the live server and back into the same host's provider side. T2 needs this in step 5, and step 6's suite over both transports needs the same seam. With the seam in place, a changeable `url` would be a second way to do part of what it does.

Alternatives weighed:

- **Only an entry's `url` may change under `override_settings`**, the host rebuilding that entry's transport, as Django's `clear_cache_handlers` rebuilds the cache handlers when `CACHES` changes (fact 9). It keeps the test on the settings path. It reaches only a remote identity, so it serves a stub provider that is not neolith, never the live server of a provider in the same host, and system checks such as `neolith.W002` would not see the changed url, since they run at set-up.
- **The provider in a second process, on a port fixed before settings are built.** It keeps every test on the settings path with no seam. It costs a process per test module, a port chosen ahead that another process can take, and a slower suite on a small machine; neolith's own process tests (0022 PR 8) stay for what needs a process.
- **Any change that passes the table.** Adding or removing an identity changes Resolve's result, which 0022 keeps fixed per process, as Django warns on any override of `DATABASES` (fact 9).
- **A public seam in `neolith.testing` now.** Adopters could run their own contracts over HTTP in one process. Its form is best decided with the conformance suite of step 6, which is what adopters would run; starting private and opening later is compatible.

The cost: tests of a remote provider do not exercise a changed setting, and the seam is a second way of naming a destination, private and for tests only. The builder's rows and set-up are tested on their own, through 0022's plain function.

### 7. What step 5 leaves out, and what stays private

Out of step 5, for step 6 and 0016:

- `local-first`, and any policy other than `single`. The absence of a policy means `single`, and step 6's record gives the policy its form;
- an identity provided in the host and configured remotely at once, which the table refuses with a message that names step 6;
- more than one provider per identity, which the list's row refuses;
- retry and fallback. TRN-8 limits them to idempotent actions, `unavailable` and the call's deadline, and TRN-17 forbids both under `single`. Neither the connector nor the HTTP transport retries a call (A2);
- the failover test of the conformance suite (0016's consequence).

Private in 0.1: the `Transport` protocol, the transport classes' constructors and methods, and the `over_http` seam. `neolith.transports.HTTPTransport` is a name the setting admits, not a class an adopter builds or subclasses. A later record opens the plug-in point when a second transport exists to shape it, and the conformance suite of step 6 is there for it to pass.

Follows principles 3 (*configuration over code changes*), 5 (*same semantics over same mechanics*), 6 (*visible failure over transparent calls*), 10 (*Django-native over framework-new*) and 12 (*small core over broad features*).

## Consequences

- A connector may hold more than one provider for a contract, each over its own transport (see [0016](0016-an-app-may-run-attached-and-detached-at-once.md)). In 0.1 it holds exactly one, chosen at set-up (Decision 2), until step 6 adds `local-first`.
- v0.1 ships only the in-process and HTTP transports. The transport interface is the only extra cost now, and it stays private in 0.1 (Decision 7).
- Contract types must stay within a portable subset that both JSON Schema and protobuf can express (no arbitrary Python objects).
- Typed errors ([0008](0008-typed-contract-errors-in-both-modes.md)) must map onto every transport's error model.
- [0006](0006-contracts-are-typed-and-published-as-openapi.md) still holds. OpenAPI becomes the projection for the HTTP transport, not the only one.
- A deployment moves an app from attached to detached by removing it from the calling host's apps and adding one `REMOTE_PROVIDERS` entry per contract the calling host still requires of it, a signing key for each calling app (A3), and at the provider the matching caller keys and an `ALLOWED_HOSTS` that admits the url's host; and the reverse for what the moved app requires of apps still in the calling host, which then also serves those contracts over HTTP. Neither app's code changes (OVR-5).
- A provider's `ALLOWED_HOSTS` has to admit the host each caller writes in its url (fact 8, A1). A Host outside it gets Django's 400, which the caller sees as `unavailable`.
- A remote provider that is down never stops a calling host from starting. Its callers get `unavailable` until it is back, and the calling code decides what that means for its own request (principle 6).
- A provider that is up but never answers holds a call with no deadline indefinitely: only the call's deadline bounds it, and a command scope without a timeout, `REQUEST_DEADLINE: None` or a call that arrived over a transport without a budget gives none. The guide advises giving command scopes a timeout. 0028 states the same gap.
- A caller whose contract package is newer than the provider, within one major version, gets `unavailable` for an action the provider does not have yet, because that path is not a contract path there and the provider answers it with 404 (spec 03, A1). The guide has to say so.
- 0021 and 0022 are not edited. Their rows that name step 5 read with this record, as 0021 Decision 9 added a step to 0017's order without editing it: the `requires` row admits a configured remote provider, its message names the new key, and 0022's key table gains `REMOTE_PROVIDERS` with the rows above, as a key that cannot change after set-up.
- Step 5's public surface gains the `REMOTE_PROVIDERS` key, the module `neolith.transports` with the name `HTTPTransport`, and the check `neolith.W002`.
- The specification gains, in the step 5 pull request that adds the key, with IDs assigned there (P5) and in its own words (P2): in spec 02, that under `single` a host chooses each contract version's one provider before it serves, a provider in the host when one exists and otherwise the configured remote provider; that a host stops before it serves when its configuration names a remote provider for a contract version none of its apps requires, or, under `single`, one of its apps provides; and in spec 05, that a host MUST NOT refuse to serve because a remote provider cannot be reached. Spec 05's Resolve and APP-1 keep their words: a configured remote provider now has its form here, and Resolve still fails before Serve. The url's grammar and messages, `ALLOWED_HOSTS` at the provider, `neolith.W002`, reading a URL from the environment in settings, calls with no deadline and version skew go in django-neolith's documentation, not the specification (P2).
- Tests: one refused value per row of the builder's table, and for each `url` reason, including a value with a password in its userinfo and one with a token in its query, a test that the refused value is absent from the message; a Hypothesis test whose property is that a drawn string is admitted exactly when it equals the ASCII serialisation of its own origin within the grammar's restrictions, its oracle taking a label that begins with `xn--` as written, with `0x7f.1`, `1.0x2`, `127.1`, `a_b.example`, `example.com.`, `münchen.example`, `[::ffff:127.0.0.1]` and `[::ffff:7f00:1]` among the refused examples and `xn--zz` and `xn--mnchen-3ya.example` among the admitted ones, through 0022's plain function; the two Resolve rows, reported together with other refusals; `neolith.W002` on `http` to a non-loopback name and to `foo.localhost`, and its absence on `localhost`, `127.0.0.0/8` and `[::1]`; an `override_settings` that changes `REMOTE_PROVIDERS`, refused; `over_http` entered for an in-host and for a remote identity, and its refusals; a host whose remote provider cannot be reached, which sets up, boots and serves, and whose call fails with `unavailable`; and the contract suite over a live server through `over_http` (Decision 6).

## Open questions

1. **A provider behind a private certificate authority.** For 0.1, 0028 answers this without an option: the authority goes in the system trust store, or in `SSL_CERT_FILE` or `SSL_CERT_DIR` for the process (its Decision 6, under A2). A per-entry or per-host trust option stays a later candidate, with Dapr's HTTPEndpoint `clientTLS.rootCA` and Spring Boot's SSL bundle per client group as the precedent. Under per-contract keying (Decision 1) a per-entry option is repeated for every identity on one host, which is when the group form would be weighed again. TLS verification is never disabled (K9) either way.
2. **A check before deployment that each remote provider serves the contract version.** It would find a wrong URL or an older provider before the first call. Its natural home is `neolith check` (step 7, #14) or breaking-change detection, both outside step 5, and it would run against the provider's well-known OpenAPI path, never as a condition of serving (Decision 5).
3. **The form of a routing policy**, for step 6: per identity inside `REMOTE_PROVIDERS`, or a key of its own. This record fixes only that absence means `single`. Step 6 also says whether a second entry under an identity is another provider, chosen by a routing policy, or another replica of the same provider. Prior art usually keeps replicas behind one name (a Kubernetes Service, a load balancer), though Spring Cloud's discovery client and Django's `CACHES` `LOCATION` list take several addresses; this record expects the list's entries to be distinct providers.
4. **Opening the transport interface** to separate packages (Decision 7), with the conformance suite of step 6 as its test, and whether `over_http` becomes public in `neolith.testing` with that suite (Decision 6).

## References

- Django 5.2.19 and 6.1.3: `db/backends/base/base.py` (`ensure_connection`, `_cursor`), `test/testcases.py` (`LiveServerThread`, `LiveServerTestCase`), `test/utils.py` (`override_settings`, `setting_changed`), `test/signals.py` (`clear_cache_handlers`, `COMPLEX_OVERRIDE_SETTINGS`), `http/request.py` (`host_validation_re`).
- Python 3.12.13 and 3.13.5: `urllib.parse.urlsplit`, `ipaddress`, `socket.getaddrinfo` (probes in facts 6 and 7).
- [RFC 6454](https://www.rfc-editor.org/rfc/rfc6454), section 6.2 (ASCII serialisation of an origin); [WHATWG URL Standard](https://url.spec.whatwg.org/#origin), origin and host serialisation, and the IPv4 parser's "ends in a number"; [RFC 1123](https://www.rfc-editor.org/rfc/rfc1123), section 2.1 (host names); [RFC 5890](https://www.rfc-editor.org/rfc/rfc5890), section 2.3.1 (XN-labels, A-labels and Fake A-labels); [UTS 46](https://www.unicode.org/reports/tr46/) (the processing the URL Standard's domain to ASCII uses).
- Go 1.25.0: `src/net/http/request.go` (`idnaASCII`). Kubernetes apimachinery v0.34.0: `pkg/util/validation/validation.go` (`IsDNS1123Label`).
- [RFC 6761](https://www.rfc-editor.org/rfc/rfc6761), section 6.3 (`localhost`); [RFC 8615](https://www.rfc-editor.org/rfc/rfc8615) (well-known URIs).
- [gRPC connectivity semantics and API](https://github.com/grpc/grpc/blob/master/doc/connectivity-semantics-and-api.md): the IDLE state; [gRPC wait-for-ready](https://grpc.io/docs/guides/wait-for-ready/): fail-fast by default.
- [Spring Boot, HTTP service clients](https://docs.spring.io/spring-boot/reference/io/rest-client.html) (client groups); [Spring Cloud OpenFeign](https://docs.spring.io/spring-cloud-openfeign/reference/) (per-client `url`); [Spring Boot application availability](https://docs.spring.io/spring-boot/reference/features/spring-application.html) (liveness and external checks).
- [Kubernetes DNS for Services and Pods](https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/); [Encore self-hosting](https://encore.dev/docs/ts/self-host/configure-infra) (`service_discovery`); [Dapr HTTPEndpoint](https://docs.dapr.io/reference/resource-specs/httpendpoints-schema/) (`baseUrl`, `clientTLS`); [The Twelve-Factor App, backing services](https://12factor.net/backing-services).
