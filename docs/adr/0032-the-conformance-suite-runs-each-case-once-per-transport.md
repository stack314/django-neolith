# 0032. The conformance suite runs each contract case once per transport and compares the outcomes, lists its cases by name, and reports a run per requirement ID against the specification's own exemptions

- **Status:** Proposed
- **Date:** 2026-11-18; reconciled with the routing record 2026-11-19
- **Narrows, once accepted:** [0028](0028-the-http-transport-uses-the-standard-library-only.md) (Decision 9's live server, Decision 5 here). 0028 gains `Narrowed by 0032 (Decision 5)` under its status (D2).
- **Accepted by:** the pull request that carries the first code built on it. In #13's plan that is step 5's task 8 (#12), which builds Decisions 4 and 5 in their final files. #12's task 8 line lists `src/neolith/_server.py` and `tests/conformance/`, and ends "Accepts the conformance record". It also covers TLS and an IPv6 origin, outside `tests/conformance/` (Decision 4).

Drafted for step 6 and revised after skeptic and prior-art reviews; the routing record, [0031](0031-a-contract-version-is-routed-by-single-unless-local-first-is-named.md) (Proposed), is proposed alongside it, and the two were reconciled before either was proposed. This record leaves 0014 unchanged, `over_http` private among it (Decision 6).

## Context

Roadmap step 6 (#13) delivers "Conformance suite running every contract test over both transports; `local-first` routing and a failover test with two providers", and turns on enforce mode in the spec trace check of [ADR 0018](0018-requirement-ids-and-spec-to-test-traceability.md). [ADR 0015](0015-specification-first-django-reference-implementation.md) makes the conformance tests "the authoritative definition of 'implements neolith'". Spec 06 is an outline with no requirement IDs.

Step 5 (#12) leaves three things to this step. [ADR 0014](0014-pluggable-connector-transports.md) (accepted with PR #128) keeps a private test seam, `over_http(identity, origin)`. Its Decision 6 says a public seam "is best decided with the conformance suite of step 6", and its open question 4 asks whether the seam "becomes public in `neolith.testing` with that suite". Its Decision 7 also leaves "the failover test of the conformance suite (0016's consequence)" to this step. Step 5's task 8, not started yet, runs "the shared contract suite through over_http against a live server (T2); in-process and HTTP parity for the same call", and adds spec 03 to `enforce`. [ADR 0028](0028-the-http-transport-uses-the-standard-library-only.md) Decision 9 (Accepted) says how: "The contract suite runs against a `LiveServerTestCase` on `localhost`, reached through 0014's private `over_http` seam (its Decision 6)." #13's plan has task 8 build this record's Decisions 4 and 5 instead, so the repository has one form from the start.

This record decides what the suite is, how one case runs over every transport, how its cases are named so that another implementation can read them, how a run says which requirements an implementation meets, how the routing part is built, and how the suite stays isolated on a small machine. It does not decide routing semantics: what `local-first` admits and its settings form (0014 open question 3), when a call moves to another provider, and whether an attempt has a timeout of its own (0028 Decision 5: "Whether a try gets a timeout of its own inside the deadline ... is step 6's question") belong to the routing record. The routing part's cases and their expected outcomes are that record's (its Decision 9); this record says how they are driven, named and reported.

### What the code and libraries do

Read at Django 5.2.19 and 6.1.3, pytest 9.2.0, pytest-django 4.14.0 and Hypothesis 6.170.15, and at the branches named.

1. **Coverage is measured per test, by outcome.** `scripts/spec_trace_plugin.py` records each collected test that carries the `spec` marker, keyed by node id, with the outcome `passed`, `skipped`, `xfailed`, `xpassed`, `failed`, `error` or `not run`. `scripts/spec_trace.py` counts a requirement as covered when any test that cites it passed (`Trace.covering`). A test parametrized over two transports is two node ids, so one that passed over one transport and was skipped over the other covers its requirements.
2. **Enforcement is a list that each step extends.** `tests/spec_trace.toml` at `origin/release/0.1.0a3` enforces `["01", "02", "05"]`, and its comment says "The pull request of the roadmap step that implements a document adds its number here", as ADR 0018 does. `scripts/spec_trace.py` also takes `--enforce NN...` for a single run. The `[not-tested]` entries are OVR-2, OVR-5, OVR-7, CON-1, CON-5, CON-12 and APP-4. CON-5's entry reads "The conformance suite checks it for the reference contract, roadmap step 6". `spec/README.md` marks 00, 01, 02, 03, 05 and 06 "yes" in its *In 0.1* column, and 04 "outline only". `docs/roadmap.md` gives step 8 the spec column "all".
3. **A provider's OpenAPI document covers every contract version its host provides.** Spec 03 HTTP-2 (#129): "A provider MUST serve an OpenAPI 3.1 document describing every contract version it provides". `src/neolith/_openapi.py` builds one document per host, in the order each description lists its actions, kinds and records. The test host already provides the contracts of `tests/host_apps/stock` and `couriers`.
4. **The seam replaces one identity's connector, process-wide, and is a generator context manager.** In `src/neolith/_http_transport.py`, `over_http` is decorated with `@contextlib.contextmanager`, stores its connector in the module dict `_SEAMS` under a lock, and `_host` consults it on each call. A generator context manager is also a `ContextDecorator`: on an `async def` test it enters and exits before the coroutine runs. 0014 Decision 6 exempts the seam from `neolith.W002` because "tests reach only the local machine (T3)", which binds this repository's tests, and its grammar admits any `http` origin.
5. **pytest loads every installed `pytest11` entry point.** `_pytest/config/__init__.py` (pytest 9.2.0, lines 1764 to 1769) calls `load_setuptools_entrypoints("pytest11")` unless `PYTEST_DISABLE_PLUGIN_AUTOLOAD` is set or `-p no:...` is given. Libraries ship such modules without making pytest a runtime dependency: anyio declares `pytest11 = {anyio = "anyio.pytest_plugin"}` with pytest only in its test group, and Hypothesis registers `_hypothesis_pytestplugin` the same way.
6. **pytest-django's `live_server` asks for a transactional database on every test that uses it.** `_live_server_helper` (`pytest_django/fixtures.py`, lines 641 to 665) calls `request.getfixturevalue("transactional_db")`, and the server binds `localhost` unless `--liveserver` says otherwise. pytest-django blocks database access for the whole process (`plugin.py`, lines 890 to 923), so a server thread that reaches a database without that fixture fails loudly.
7. **Django's `LiveServerThread` is not documented API, and changed between the supported versions.** In 6.1.3 `terminate()` stops the server only when `is_ready` is set; in 5.2.19 it does not check. It needs the private `_StaticFilesHandler`.
8. **neolith's own server counts connections and requests.** `src/neolith/_server.py` (ADR 0022 Decisions 9 and 10, ADR 0025) keeps a `Connections` table. `opened` enters a connection with the value `None`; `begin` marks a request only once its request line is read; `closed` removes the connection. `in_flight()` lists only connections with a request marked, so a connection the server has accepted but whose request line it has not read yet is not listed. `wake(callback)` is called whenever a request ends or a connection closes.
9. **`get_wsgi_application()` sets Django up again.** `django/core/wsgi.py` calls `django.setup(set_prefix=False)` before it returns `WSGIHandler()`, which configures logging again. `neolith run` serves `WSGIHandler()` directly (`src/neolith/_cli.py`, line 333).
10. **Django's test setup adds only `testserver` to `ALLOWED_HOSTS`** (`django/test/utils.py`, line 141 in 5.2.19, 140 in 6.1.3).
11. **The HTTP client reads the clock through its own name.** `src/neolith/_http.py` binds `_now = _context.now` at import. The clock seam of `tests/test_call_context.py` patches `_context.now` only.
12. **An action's own `unavailable` or `timeout` never reaches an in-process caller.** ADR 0008 Decision 1 lets an action raise any standard kind "except `unavailable` and `timeout`", and its Decision 3 turns such an error into `internal`, whose fixed message "carries the trace identifier".
13. **An explicit root has no idempotency key.** `command_scope(app, *, principal, timeout)` in `src/neolith/_host.py` takes no key. TRN-49: an explicit root "has no idempotency key, so the keys of its calls are random (TRN-33)". A parent that holds a key gives each of its calls a distinct key, counting repeats (TRN-32). So a case opened from an explicit root cannot make two calls with one key, over either transport. A key repeats only when a parent that holds one runs again: an action whose call is retried derives the same keys for its own calls (TRN-32, "a retried parent reproduces its children's keys"; spec 06 item 7), and the routing record sends one key to every provider of a call it moves (its Decision 6). Neither can be started from an explicit root without a caller that writes `Idempotency-Key` itself.
14. **pytest 9 runs subtests itself.** `_pytest/subtests.py` (pytest 9.2.0, lines 258 to 262) reports each subtest under the test's node id, which the spec trace plugin would read as the test's own outcome. pytest 8.4, the floor in `pyproject.toml`, has no such fixture.
15. **One process hosts one set of apps.** `tests/hosting.py` runs code in a fresh process for a test that needs a host of its own (ADR 0022 Decision 5), passing the settings as JSON in `argv`. `subprocess.TimeoutExpired` prints the whole command.
16. **Some tests fork.** `tests/test_http_client.py` has two tests that call `os.fork()` and silence the multi-threaded `DeprecationWarning`; any thread alive at that point is inherited.
17. **The provider side also runs under ASGI in 0.1.** `_endpoint.py` has `aanswer` for an `ASGIRequest`, which runs the action through `sync_to_async(thread_sensitive=True)`; #12 task 6 verifies each row under WSGI and ASGI.
18. **The docs site mirrors only numbered spec documents.** `scripts/docs.py` (line 227) mirrors `spec/[0-9][0-9]-*.md`.
19. **Release assets already exist; CI artifacts expire.** `docs/releasing.md` creates a GitHub release with `gh release create`. `.github/workflows/release.yml` already uses `actions/upload-artifact`, pinned by commit; `ci.yml` does not. GitHub keeps workflow artifacts for 90 days by default.

## Options

1. **The suite as this repository's tests only.** No new layout. Nothing tells a case that crosses a contract from a test of one transport's internals, and "same semantics" (spec 06 item 6) is only as strong as each test's assertions.
2. **A suite package in the wheel, as a pytest plugin.** Adopters and other implementations run it with one command. It loads into every adopter's pytest session before their settings are configured (fact 5), ties a shipped module to pytest's hook API, and runs a reference contract that is not theirs.
3. **A suite in this repository, organised by what it drives, with one case run over every transport and the outcomes compared, every case named in a neutral list beside the reference document, and a run report keyed by requirement ID.** No new public API; another implementation reads the reference document and the case list from the specification. A driver that runs the code against another implementation waits for one to exist.

## Decision

Option 3, in the admitted set and Decisions 1 to 11 below.

### The admitted set

Every input this record adds has one form. Anything else is refused with the message named. None of these inputs is public API of the package.

| Input | Admitted | Refused with |
|---|---|---|
| `over_both(call, *, identity="reference.v1")`, a fixture of `tests/conformance/contract/` | `call` is a callable with no arguments that opens its own scope and makes its own calls; `identity` is exactly a `str` the test host provides. | `TypeError` for anything else; `over_http`'s `UsageError` for an identity; a case that is not repeatable fails with both outcomes shown (Decision 4). |
| The reference contract `reference.v1` | Exactly the actions, records and kinds of `spec/conformance/reference.v1.openapi.json` (Decision 3). | The reference contract's part of the served document differs: CNF-new-r2 fails. |
| A case file `spec/conformance/cases/<part>.json` or `spec/conformance/cases/<part>-<topic>.json` | `<part>` is one of `contract`, `wire`, `routing`, `lifecycle`, and `<topic>` matches `[a-z][a-z0-9]*`. A JSON array of objects with exactly `name` (unique across all case files, `[a-z][a-z0-9_]*`), `part` (the file's `<part>`), `state`, `procedure`, `expected` (strings) and `requirements` (live IDs). | `scripts/spec_trace.py`: `<file>: a case file is named <part>.json or <part>-<topic>.json, where <part> is contract, wire, routing or lifecycle`; `<file>: case <n>: <member>: <reason>`; `<name>: listed in <file> and <other file>`. |
| The marker `conformance_case(name)` | One name from a case file, on a test whose `spec` marker cites every ID the case lists. Each listed case has at least one test with the marker. | `<node id>: case <name> is not listed`; `<node id>: case <name> lists <ID>, which the test does not cite`; `case <name>: no test runs it`. |
| `scripts/spec_trace.py --conformance PATH` | A file path, given together with `--report`. | `--conformance needs --report`; `--conformance needs a file path, not '<value>'`; `--conformance cannot write <path>: <reason>`. |
| The plugin's report | `{"format": 2, "environment": {...}, "tests": {...}}`; `environment` has exactly `python`, `architecture` and `packages`, the last a map of `django-neolith` and `Django` to their versions; a test's entry may carry `transport_outcomes` (Decision 4). | `<path>: report format <n>; this checker reads 2`. |
| `[not-implemented]` in `tests/spec_trace.toml` | IDs of SHOULD- or MAY-level requirements only, each with a reason. | `[not-implemented] <ID>: is a MUST; only SHOULD and MAY requirements may be left unimplemented`. |

`enforce`, `[not-tested]` and `--enforce` are unchanged and keep ADR 0018's rules: the step that implements a document adds it to `enforce`, and a `[not-tested]` entry is a judgement made in review.

The test host's settings (`tests/settings.py`) gain the shared-secret caller backend, a signing secret generated per run (K9) for each hosted app, derived from the list of hosted apps so that adding an app needs no other change, and `127.0.0.1` in `ALLOWED_HOSTS` (fact 10). Step 5's task 8 adds them (#13's plan).

### 1. The suite is spec 06's items, made executable in this repository; three readers use it in three ways

The conformance suite is the executable form of spec 06's items. It is part of this repository, as spec 06 says, and lives under `tests/conformance/`.

- **This repository runs all of it**, in every leg of the CI matrix (ADR 0012), as part of `pytest`. Its run is django-neolith's conformance run (Decision 9).
- **An app author does not run the suite.** The suite checks neolith against a reference contract, not an app's contract against its provider. `@implements` already checks a provider's shape at import (K2). Python's DB-API compliance suite, `dbapi20.py`, draws the same line: driver authors run it, app authors do not. How an app author runs their own contract tests over HTTP in one process is left to step 8 (Decision 6).
- **Another implementation reads the suite from the specification.** Spec 06 defines the reference contract by its reference document (Decision 3), names every case in the case files beside it, each with the state it needs, its procedure, its expected outcome and the requirement IDs it covers (Decision 7), and maps its items to requirement IDs (Decision 9). Another implementation writes its own runner from those files, as each implementation of gRPC's interop cases or of the JSON Schema Test Suite does. In 0.1 the Python cases run against this repository only; a driver that runs them against another implementation is open question 1.

*Why:* spec 06 says the suite "is part of this repository and is the authoritative meaning of 'implements neolith'", and ADR 0015 says another implementation "is conformant when it passes the conformance tests". A case list in data costs no runner code and makes the second reader's path real now. A driver needs a second implementation to shape and test it against, and none exists.

Alternatives weighed:

- **The suite as a separate distribution, run against any implementation.** It lets another implementation run the same code. It needs a driver protocol for that implementation's connector. Open question 1 keeps this.
- **A suite an app author runs against their own provider**, as Pact's provider verification does. neolith's contracts are typed and checked at import, and a call's behaviour is the app's own; a generic suite cannot know what an app's action should return.

### 2. Four parts, by what they drive

The suite has four parts. The contract, wire and routing parts live under `tests/conformance/`; the lifecycle part's tests stay where step 4 put them, in `tests/test_run.py` and `tests/test_boot.py`. Each test module under `tests/conformance/` says which part it belongs to in its docstring.

| Part | Drives | Transports | Spec 06 items |
|---|---|---|---|
| `contract/` | neolith's connector against the reference provider, through `over_both` | in-process and HTTP (WSGI), compared | 2, 6, 7, and 1's contract cases |
| `wire/` | neolith's provider with the suite's own HTTP caller; neolith's connector against the suite's own scripted provider | HTTP only (T2: the wire is the subject) | 1's projection, 3 |
| `routing/` | a host in a process of its own, configured through settings with one or more remote providers per identity | HTTP, scripted providers | 4 |
| lifecycle, outside `tests/conformance/` | hosts that fail to start, and one app booted alone | none | 5 |

The lifecycle part gathers the process tests that step 4 already wrote (`tests/test_run.py`, `tests/test_boot.py`), which already cite APP IDs. Nothing moves: they gain `conformance_case` markers and a case file, and spec 06's item table maps item 5 to their APP IDs.

Outside `tests/conformance/`, a test moves into `contract/` only when its case can be written against `reference.v1`. A test that calls through `neolith.connect()` to a contract of `tests/host_apps` (as in `test_host.py`, `test_calling_app.py` and `test_command_scope.py`) stays where it is, and its module's docstring gives its T2 reason (task 5 of #13's plan writes the three docstrings named here): it checks the host's own apps, not the contract's semantics over every transport. Tests of one transport's internals stay too: fake transports, the clock seam on the in-process transport, and the socket-pair tests of the HTTP client.

*Why:* each part has one kind of oracle. The contract part compares two transports with each other. The wire part compares one transport with bytes the specification fixes. The routing part needs settings fixed at set-up (0014 Decision 6), so it needs its own host.

### 3. The reference contract is `reference.v1`, defined by a committed OpenAPI document

`spec/conformance/reference.v1.openapi.json` is the reference document: the projection (HTTP-2, HTTP-20) of the reference contract alone, as a provider that conforms to spec 0.1 serves it. It is the reference contract's neutral definition. The spec trace reads only Markdown in `spec/`, so a JSON file there is not a document it checks. `scripts/docs.py` mirrors `spec/conformance/` to the docs site, and spec 06 links the file, so a reader finds it where they find the specification (fact 18).

The contract, by action:

| Action | Idempotent | Request → reply | Behaviour of the reference provider |
|---|---|---|---|
| `echo` | yes | `Values` → `Values` | Returns the request. `Values` holds every portable form of spec 01, each once required and once optional: boolean, integer, number, string, bytes, decimal, timestamp, date, duration, `list`, `map`, a nested record and an enum. |
| `accept` | no | `Values` → empty | Returns nothing. |
| `refuse` | yes | `Refusal` → `Values` | Raises the kind the request names (`invalid_argument`, `unauthenticated`, `permission_denied`, `not_found`, `conflict`, or the declared kind `gone` with detail `Gone`) with the request's message. |
| `seen` | yes | `Empty` → `Seen` | Returns what its call context holds: the principal or none, the calling app, whether a deadline and an idempotency key are present. |
| `deposit` | yes | `Deposit` → `Balance` | Adds `amount` to `account` once per idempotency key, and returns the total (CON-5). |
| `leak` | yes | `Empty` → `Values` | Returns a value that is not of the reply type. |
| `boom` | yes | `Empty` → `Values` | Raises an ordinary exception. |
| `stall` | yes | `Empty` → `Values` | Waits until its own deadline has passed, then returns. Fails with `invalid_argument` at once when the call has no deadline, so it never waits unbounded. |

The provider is a pure function of its request, apart from `deposit`'s totals, which are keyed by `account` and kept in memory under a lock. The reference apps are three, in `tests/host_apps`: `reference` provides the contract with `principal_from=("reference_caller",)`; `reference_caller` requires it; `reference_stranger` requires it and is not on the allow-list. None has a database, so no live-server thread reaches one (fact 6).

**CNF-new-r2** (spec 06): the OpenAPI document a provider of the reference contract serves at the projection's location holds the reference contract's part of the reference document unchanged: the paths of the reference contract's actions, every component those paths reach, and the top-level members that spec 03's projection list fixes. Members are compared as JSON values, so another implementation may order an object's members or space them differently. Arrays compare in order, apart from `required`, `enum`, `oneOf`, `anyOf` and `allOf`, which spec 03 does not order and which compare as sets. The document may hold other contract versions the host provides (HTTP-2).

*Why:* spec 06 item 1 asks for "a reference contract with every portable type form, an idempotent and a non-idempotent action, and declared errors", and that "its projection matches the expected OpenAPI document". A host serves one document for every version it provides (fact 3), so whole-document equality would hold only on a host with the reference apps alone; comparing the reference contract's part keeps the check on the shared test host and on any adopter's host.

Alternatives weighed:

- **Whole-document equality on a host that provides only the reference apps, in a process of its own.** Simpler to state. It costs a process, and it would fail another implementation whose test host provides more contracts beside the reference one, which HTTP-2 requires it to describe.
- **The reference contract written in the specification's prose.** Readable, but a second definition beside the document, which could disagree with it.
- **Reusing the contracts of `tests/host_apps`.** They exist, and none has every portable form or a stateful idempotent action.

### 4. One case runs over every transport through `over_both`, which compares the outcomes

A contract case is written once, as a callable, and handed to the fixture `over_both`:

```python
@pytest.mark.conformance_case("reply_of_another_type")
@pytest.mark.spec("CNF-new-r1", "TRN-18", "CON-28")
def test_a_reply_of_another_type_is_internal(over_both: OverBoth) -> None:
    def call() -> Values:
        with neolith.command_scope("reference_caller"):
            return neolith.connect("reference.v1").leak(Empty())

    outcome = over_both(call)
    assert outcome.kind == "internal"
```

`over_both` runs `call` once in-process, with the host's own connector, then once inside the private seam `over_http(identity, <live server origin>)`. After each run it waits until the live server has no open connection (Decision 5). It records each run's *outcome*:

- a reply: the value returned, with its exact type;
- an error: its exception type, and for a `ContractError` its kind, its detail and its message.

Two outcomes are equal when both are replies of the same type that compare equal, or both are errors of the same type, kind and detail whose messages are equal, apart from `internal`, `unavailable` and `timeout`, whose messages name a trace or what the transport saw (fact 12). When they differ, the test fails with both outcomes, each labelled with its transport. When they agree, `over_both` returns the outcome for the case's own assertions. The transports are a tuple, `("in-process", "http")`, in the fixture's module; a later transport adds one entry and its seam.

`over_both` also records each leg's outcome on the test, through `request.node.user_properties`, as `transport_outcomes`: for each transport, `"same"` when every example agreed, or the outcome of the first example where the legs differed. The plugin's format 2 carries it into the report, so a run says which leg diverged while the case keeps one node id, as interop matrices report a case per pairing.

A case must be repeatable: a second run must not see the first. A case names the state it needs in its case file (Decision 7), such as "account A has no deposits"; the Python case meets it by drawing a fresh `account` per run. A case that cannot be repeatable, such as one that counts calls, belongs in another part.

**CNF-new-r1** (spec 06): an implementation gives the caller the same outcome over the in-process and the HTTP transport for a call that reaches its provider and returns before its deadline, for a call whose deadline passes first, and for a call the caller refuses before sending it: the same reply, or an error of the same kind and detail and, apart from `internal`, `unavailable` and `timeout`, the same message. A call whose deadline passes first ends in `timeout` over both. A refusal that the specification places on one transport only is outside this rule: in 0.1, a call from an app that holds no key to sign a shared-secret token with, which the HTTP caller refuses (HTTP-18) and the in-process transport serves, and a request a provider's host refuses for its body limit (HTTP-40, a MAY). A refusal that the specification later adds for the network alone joins this list in the specification, never in a test (#133 weighs one, a provider's host that requires HTTPS). Every `over_both` case cites CNF-new-r1. The HTTP leg runs the provider side under WSGI; the endpoint tests' ASGI rows (#12 task 6, fact 17) carry the ASGI provider side.

Hypothesis cases run inside `over_both` under the settings of `tests/test_contract_round_trip.py` (`derandomize=True`, `database=None`, `deadline=None`), with `max_examples` measured so the contract part fits the CI budget (K10). `over_both` is function-scoped, so it can reach the test's node; it holds no state from one example to the next, so the contract part's Hypothesis settings suppress `HealthCheck.function_scoped_fixture`, as Hypothesis documents for such fixtures.

*Why:* one test covers its requirements only when it passed over both transports, since a run that differs fails the one test (fact 1). The comparison checks what a case does not assert, which is what spec 06 item 6 asks: "the same contract test cases run over the in-process and the HTTP transport and produce identical results".

**One form with step 5's task 8.** Task 8 builds "in-process and HTTP parity for the same call" over a live server. The repository keeps one form of it: this record's live server and `over_both`. In #13's plan, task 8 builds them in their final files, with its parity cases over `stock.v1`, and its pull request accepts this record, since it carries the first code built on it, as #12's task 8 line says (header). Step 6's tasks then add cases and names to those files. The one change to what task 8 wrote is that task 2 rewrites task 8's parity cases against `reference.v1` and adds CNF-new-r1 to each: they run over `stock.v1`, a contract the specification does not define, and a case file names only cases over `reference.v1`. This server is plain HTTP on an IPv4 loopback address under neolith's own server, so `over_both` does not exercise TLS, gunicorn or an IPv6 origin, which #12's comment of 2026-11-18 expected task 8's suite over a live server to cover. Task 8 covers TLS and an IPv6 origin outside `tests/conformance/`: a second live-server variant on `[::1]`, and one wrapped in TLS with a CA made by trustme (already a test dependency). gunicorn is not added as a test dependency and is not exercised.

Alternatives weighed:

- **A fixture parametrized over the transports.** pytest's usual form, with a node id per transport. A test skipped over one transport still covers its requirements through the other (fact 1), and each run is checked only against what the test asserts. Closing that would need the plugin to group node ids by function and parameter, which is fragile when a test has other parameters. `transport_outcomes` gives the per-leg report without it.
- **pytest's subtests, one per transport.** They need pytest 9, above the floor, and each subtest's report reaches the spec trace plugin under the test's node id (fact 14).
- **Comparing kinds only.** Simpler, and blind to a message or detail that one transport changes, which spec 02 forbids for an error the provider's host delivers.

### 5. The live server is neolith's own development server, once for the suite's package, on `127.0.0.1` and a port the system picks

A package-scoped fixture in `tests/conformance/conftest.py` binds `neolith._server.listen("127.0.0.1", 0, ipv6=False)`, serves `wsgiref.validate.validator(WSGIHandler())` with `serve_forever` in a daemon thread, and gives its origin as `http://127.0.0.1:<port>`. It serves `WSGIHandler()` directly, as `neolith run` and Django's `LiveServerThread` do, so Django is not set up again mid-session (fact 9). The standard library's validator checks every request and response against PEP 3333 at no dependency cost; the task that builds the fixture checks that Django's handler and neolith's responses pass it, and where one does not, it records which check and why, fixes neolith's response when the specification says so, and otherwise drops the wrapper, never patching Django.

At the end of `tests/conformance/` it calls `shutdown()` and `server_close()` and joins the thread, with a bound of 10 seconds, after which it fails naming the requests still in flight. Package scope ends the thread before later modules run, including the client's fork tests (fact 16).

After each run, `over_both` leaves the seam, which closes its pool, then waits on the server's table until it holds no open connection at all, with the same bound. Waiting for no request in flight would miss a connection whose request line the server has not read yet (fact 8), and its provider scope could open during the next test and fail that test's `_no_scope_left_open` guard. `Connections` gains one private read, the number of open connections, beside `in_flight()`.

This narrows 0028 Decision 9, which reads: "The contract suite runs against a `LiveServerTestCase` on `localhost`, reached through 0014's private `over_http` seam (its Decision 6). That server keeps connections alive (Context, point 5), so pooling is exercised there too." In its place: the contract suite runs against neolith's own development server on `127.0.0.1`, reached through the same seam. It keeps connections alive too (fact 8), so pooling is still exercised. The narrowing form of D2 fits because only the server and its address change; the seam, the live server and what is exercised stay.

*Why:* it is the server `neolith run` serves with, so the suite exercises it too. Its table (fact 8) lets a case end only when the provider's side has ended: `stall` over HTTP leaves the provider waiting after the caller has given up. Binding an address, not a name, keeps name resolution out of every new connection and the test on the local machine (T3).

Alternatives weighed:

- **pytest-django's `live_server`.** Maintained and familiar. It forces a transactional database on every case that uses it (fact 6), which flushes the test databases after each case, and binds `localhost`.
- **Django's `LiveServerThread`.** What `LiveServerTestCase` uses. It is not documented API, its `terminate()` differs between 5.2 and 6.1, and it needs a private static files handler (fact 7). It does not say when a request has ended.
- **A session-scoped server.** One fewer start per run. Its threads would be alive in every later fork (fact 16).
- **An ASGI leg in `over_both`.** The provider side already runs under ASGI in 0.1 (fact 17), and the endpoint tests check each row under both. A leg for every contract case needs an ASGI server on a socket; Django ships none, so it adds a test dependency such as uvicorn and its own dependencies, and a third of the suite's time. Open question 2 keeps it.

### 6. No public test entry point in 0.1; `over_http` stays private, and nothing in the wheel imports pytest

0014 Decision 7 is unchanged: `over_http` stays private in 0.1, and this record narrows nothing of 0014. #13 asks for the suite, not a public testing API, and the suite needs only the private seam. The first users of a public seam are step 8's example apps, which show an app attached and detached; that step's record decides whether and how it opens (open question 3).

When it opens, three things found here go with it. A public seam admits only loopback origins (`localhost`, `127.0.0.0/8`, `[::1]`), since 0014's W002 exemption rests on T3, which binds this repository's tests and not an adopter's; Django's `LiveServerTestCase` and pytest-django's `live_server` both serve on loopback. It is a context manager that refuses use as a decorator, or one that wraps a coroutine function correctly, since a generator context manager exits before an `async def` test runs (fact 4). And its documentation says it applies to the whole process, every thread included.

The wheel gains no pytest plugin, no `pytest11` entry point and no module that imports pytest. `over_both`, the live server fixture and the reference apps stay in `tests/`, which ships in the sdist only (`pyproject.toml`, `[tool.hatch.build.targets.sdist]`).

*Why no plugin:* a plugin module does not make pytest a runtime dependency (K5 does not apply), as anyio and Hypothesis show (fact 5). The reasons are others. A context manager works under any test runner, as Django's own test tools and asgiref's `ApplicationCommunicator` do (principle 10). An entry point loads into every adopter's pytest session before their settings are configured. And a shipped plugin follows pytest's hook API across its versions, which is upkeep for this project. A later record can weigh the plugin form again on these grounds, with anyio and Hypothesis as its precedent.

Alternatives weighed:

- **Make `over_http` public now**, narrowing 0014 Decision 7. Adopters could test their contracts over HTTP in one process. Its admitted set was shaped for a seam that reaches only this repository's local tests, and its first real users arrive with step 8.
- **A public `over_both`.** A second public helper whose outcome rule (Decision 4) is this suite's.

### 7. The wire part uses its own HTTP caller and its own scripted provider, and every case is named in a neutral list

Spec 06 item 3 asks that "the implementation's HTTP provider is driven by the suite's own HTTP caller, and the implementation's HTTP connector is driven against the suite's own reference provider".

- **The suite's own caller** is the standard library's `http.client`, writing each request exactly as spec 03 fixes it, with a token minted by the test from spec 03's rules. It never uses `neolith._http`. Each test asserts on the status line, header fields and body bytes, and imports nothing from `neolith` to make its assertions. It also fetches `/.well-known/neolith/openapi.json` and checks CNF-new-r2.
- **CON-5 is checked here.** The caller sends `deposit` twice with one `Idempotency-Key` and gets one effect and equal totals, and a third time with another key and gets a second effect. A case opened from an explicit root cannot repeat a key over either transport, and the other ways a key repeats need a caller that writes the key or a provider that moves calls (fact 13), so CON-5's case is HTTP only (its T2 reason); CON-5's `[not-tested]` entry is removed with it.
- **The suite's own scripted provider** is a server on a socket, as `tests/http_peer.py` already plays one for the client's tests. It answers neolith's connector with bytes the test writes, so each row of spec 03's response mapping is checked against bytes, not against neolith's own provider.

The step 5 tests that already do this, in `tests/test_http_transport.py` and `tests/test_provider_endpoints.py` (PR #129), move into `wire/` when they cite spec 03 IDs and drive only the wire; the rest stay.

**The case list.** Every case of every part has a stable name and an entry in a case file under `spec/conformance/cases/`, named by its part and, where a part has more than one file, by a topic (admitted set). A file groups cases by what a reader looks for, not by where the Python lives: one file may list the cases of several test modules, as `lifecycle.json` lists those of `tests/test_run.py` and `tests/test_boot.py`, and one module's cases may sit in more than one file. Each entry gives its `state` (what must hold before it runs, as Pact's provider states name it), its `procedure` in numbered prose, its `expected` outcome, and the requirement IDs it covers. The form follows gRPC's interop test descriptions, which name each case with the features it needs, a procedure and the client's assertions, and the JSON Schema Test Suite, which other implementations read with only a JSON parser. A Python test names its case with the `conformance_case` marker, and the checker holds the two in step (admitted set). `scripts/docs.py` mirrors the case files with the reference document.

*Why:* two neolith ends that agree with each other can both be wrong. An independent end on each side is what makes two conformant implementations interoperate. A neutral case list lets another implementation write that independent end without reading Python.

### 8. The routing part runs a host of its own, configured through settings

The test is `tests/conformance/routing/test_failover.py`. A module fixture runs one process (fact 15) that binds every socket it needs before it configures settings, so no port is chosen ahead and none can be taken by another process. The child generates its own signing secret, so no secret crosses `argv`; the scripted providers do not verify tokens.

The scripted providers live in `tests/scripted_providers.py`. Each records every request it receives. A field that can carry a secret, a principal or a key (`Authorization`, `Neolith-Principal` and `Idempotency-Key`) is recorded as its position among the distinct values of that field the child process has seen, across every provider (`token 1`, `token 2`), never as the value, so a case can show that two providers saw one key and two tokens without printing either:

- **R** (refuses): a socket bound and never listening, held for the process's life; a connection to it is refused at once;
- **U** (unavailable): answers `503` with no neolith error body;
- **N**: answers `503` with a neolith error body of kind `unavailable`;
- **X** (reset after a read): reads the whole request, then resets the connection;
- **E** (errors): answers every request with the neolith error body of one kind, with that kind's status, the kind fixed when the case binds it: `timeout` (504), `unauthenticated` (401), `permission_denied` (403), `conflict` (409), `internal` (500), or a declared kind (422). A case that uses E has one E, makes one call and has one outcome; the routing record has one case per kind (its `routing_stays_on_<kind>` rows);
- **V** (invalid): answers `200` with a body that is not a valid reply;
- **S** (silent): reads the request and never answers;
- **B** (backup): answers with a valid reply;
- **D** (down): a second **U**.

The settings host a calling app and configure one small test contract per case, each with two actions, `get` (idempotent) and `put` (not idempotent), and, for the case whose E answers a declared kind, that kind declared on `get`. Each case binds its own providers, so no provider serves two cases. The settings are in the routing record's form (its Decision 2): a `single` case leaves its identity out of `NEOLITH["ROUTING_POLICIES"]` and lists one `REMOTE_PROVIDERS` entry, and every other case names `local-first` for it there and lists two. The process runs every case, prints one JSON record per case, and the module's tests assert on it, one test per case.

The cases are the routing record's: its Decision 9 has the one table of them, with each case's providers, call, outcome, requests and the requirement IDs it cites. This record does not restate them. Each case has an entry in `spec/conformance/cases/routing.json` under the naming rule of Decision 7, whose `expected` member is that table's outcome and requests, and its test carries the `conformance_case` marker with that name. A change to the routing record's table changes the case file and the test in the same pull request.

`routing_no_time_left_ends_unavailable` sets the deadline with the suite's clock, patched in the child both at `_context.now` and at `_http._now` (fact 11), and U moves it before it answers; `routing_moves_on_503` moves it by 5 ms, so that the budget at B is exactly 5 less than at U. The clock starts at a whole number of seconds, and the case's timeout is a whole number of seconds: `budget` works in binary floating point, and from any other start 5 ms less can come out as 6. `routing_silent_provider_times_out` is the one case that waits in real time, and only for its own deadline.

*Why:* only the settings path shows that a policy is configured and chosen as a deployment would configure it (principle 3). `REMOTE_PROVIDERS` is fixed after set-up (0014 Decision 6), so the test host cannot change it. Scripted providers make each failure exact, and recording requests shows what a policy did, not only what the caller got. Recording a position and not a value keeps 0014's rule that no output prints a value that could hold a secret, and numbering across providers is what lets a case compare two of them. The failover cases live once, here; the routing record keeps its own unit tests of the connector's walk and the client's bound, with fake transports, and adds no routing seam.

Alternatives weighed:

- **A routing seam, such as `over_http(identity, origin, *more)`.** It keeps the test in the session's process. It tests the seam rather than the settings path, and both records keep `over_http` at one identity and one origin.
- **A second neolith host as the backup provider.** It shows two neolith ends interoperating, which the contract part already shows. It costs a second process, and its outcome depends on that host, not on the policy.
- **Positions counted per provider.** Simpler to read. Each provider of a moved call sees one token, so both record `token 1`, and a case could not show that the token differs while the key does not.

### 9. A run says, per requirement ID, whether the implementation meets it, against exemptions the specification lists

- **Citations.** Every conformance test cites the requirement IDs it verifies, as ADR 0018 says, the CNF ID of its item where it has one, and its case name.
- **The items, mapped.** Spec 06 lists its items in a table: item, what it checks, and the requirement IDs that verify it, in 00, 01, 02, 03 and 05. Most items restate requirements of other documents, so they carry no ID of their own; CNF-new-r1 and CNF-new-r2 are the two that the other documents do not state. The citation grammar of `scripts/citations.py`, which `scripts/docs.py check` runs over the mirrored specification, refuses an ID in the table that is not a live requirement.
- **What binds others.** Spec 06 lists the must-level requirements that bind app code, contract authors or deployment rather than an implementation, each with whom it binds: in 0.1, OVR-2 and OVR-7 (app code), CON-1 (contract authors) and APP-4 (deployment). The list is part of the specification, so every implementation inherits it, as the OpenTelemetry compliance matrix marks a row N/A for everyone. An implementation cannot add to it. Spec 06 writes it under a heading *Requirements that bind others*, as a Markdown table with the columns `ID` and `Binds`; the checker reads the `ID` column of that table only, and a spec 06 without the heading has an empty list, so every must-level requirement then counts.
- **The run.** With `--conformance PATH`, `scripts/spec_trace.py` writes the conformance run, a JSON object with exactly these members:
  - `format`: `1`;
  - `spec`: the version in `spec/README.md`'s *Status* line, such as `"0.1"`; a test checks that it equals the version the OpenAPI document states (HTTP-2);
  - `suite`: `{"cases": <SHA-256 of the case files>, "reference": <SHA-256 of the reference document>, "tests": <SHA-256 over the sorted paths and contents of every file under tests/, __pycache__ aside>}`, so a published run names the suite it passed, with every app, provider and helper its cases drive. `tests/` ships in the sdist, so the hash can be computed there too. A change to an unrelated test changes the hash: it may change when the suite did not, and never stays the same when it did;
  - `implementation`: `{"name": "django-neolith", "version": ...}`, and `environment`, both from the plugin's report;
  - `requirements`: for each live requirement ID of every level, `{"document", "level", "status", "tests"}`, where `status` is the first of these that applies, in this order: `failed` (a citing test failed or errored), `passed` (a passing test cites it), `not-applicable` (listed by spec 06 as binding others, whatever `[not-tested]` holds for it), `not-tested` (with a `reason` from `[not-tested]`), `not-implemented` (a SHOULD or MAY, with a `reason` from `[not-implemented]`), `uncovered`; and for a test entry, its `transport_outcomes` when it has them;
  - `conforms`: `true` exactly when no requirement is `failed`, and every must-level requirement of every document that `spec/README.md` marks as part of the targeted specification version is `passed` or `not-applicable`.
- **The plugin.** Its report becomes format 2, with `environment` and each test's `transport_outcomes` (admitted set). It reads the versions with `importlib.metadata`, so the checker never imports Django or neolith.
- **Publication.** Each leg of the CI matrix uploads its run with `actions/upload-artifact`, pinned to the commit `release.yml` already uses (fact 19). At a release, the runbook downloads the release commit's four runs with `gh run download` and attaches them to the GitHub release with `gh release upload`, so they stay reachable after the artifacts expire. The release notes name them. That is how django-neolith "publishes its conformance run" (spec 06).

A must-level requirement whose status is `not-tested`, whatever its reason, keeps `conforms` false. `[not-tested]` keeps its role under ADR 0018, which is coverage enforcement in this repository; it does not make a claim of conformance. An ID that spec 06 lists as binding others keeps its `[not-tested]` entry, which enforcement in this repository still reads, and the run reports it `not-applicable`, since the order above puts that status first. In 0.1 that is OVR-2, OVR-7, CON-1 and APP-4. The one `[not-tested]` entry left that the run reports as `not-tested` is then OVR-5, so django-neolith's run reports `conforms: false` until OVR-5 is shown, at step 8, which is the 0.1 proof in the roadmap. CON-12, whose entry says it "holds by construction", gains a test that cites it: a contract class that redefines an action is described with that action once.

Spec 06's *Claiming conformance* becomes: an implementation conforms to a specification version when one run of the suite reports every must-level requirement of every document in that version as passed, apart from those spec 06 lists as binding others. Partial conformance stays no claim. The run's JSON form is django-neolith's, documented in the checker's docstring; the specification fixes what a run says, not its schema, until a second implementation consumes one.

*Why:* a requirement ID is how ADR 0018 lets "tests, reviews, bug reports and other implementations name a requirement exactly". Spec 06 says partial conformance is not a claim, and ADR 0015 says an implementation conforms when it passes the tests; exemptions an implementation writes for itself would let it claim what it never checked. Certification processes keep exclusions on the specification's side: in the Jakarta EE TCK process a test leaves certification only through an accepted challenge, and the exclusion ships with the TCK. Giving SHOULD and MAY a status of their own, as web-platform-tests reports an optional feature as `PRECONDITION_FAILED` rather than `FAIL`, shows an optional behaviour left out as a choice, not as a gap.

### 10. Enforce mode follows ADR 0018 as written

The step's last pull request adds `06` to `enforce`; step 5's task 8 adds `03`. The checker does not derive `enforce` from `spec/README.md`, and it does not restrict a `[not-tested]` reason's wording: ADR 0018 makes the list a step's job and each entry "a judgement made in review, not by the checker", and this record keeps both. `--enforce` stays as it is.

| Document | Enforced from | What this step changes |
|---|---|---|
| 00 | step 8, which implements "all" (`docs/roadmap.md`) | OVR-6 is cited by the contract part's context cases, with TRN-2; reported until step 8. OVR-2's and OVR-7's entries stay, reported `not-applicable`; OVR-5's stays, reported `not-tested`. |
| 01 | step 2 | CON-5's entry is removed (Decision 7); CON-12's is removed (Decision 9). CON-1's stays, and the run reports CON-1 `not-applicable` (Decision 9). |
| 02 | step 3 | The routing part cites the routing record's IDs. |
| 03 | step 5 | The wire part cites the IDs that step 5's tests cited; HTTP-40 is a MAY. |
| 04 | never in 0.1 | "Outline only" and not part of 0.1 (`spec/README.md`); reported only. |
| 05 | step 4 | The lifecycle part gains case names; APP-4's entry stays, and the run reports APP-4 `not-applicable` (Decision 9). |
| 06 | step 6 | CNF-new-r1 and CNF-new-r2, cited by the contract and wire parts. |

*Why:* #13 asks that the check switch to enforce mode in this step, and for the documents this step implements it does. Enforcing 00 here would come before the step that implements it. A rule derived from the README column, or a fixed set of reason prefixes, would change an Accepted record's rule for a gain that review already gives; the conformance claim, which is where self-granted exemptions would matter, no longer reads `[not-tested]` (Decision 9).

### 11. Isolation on a small machine

- **Ports.** Every socket binds `127.0.0.1` and port 0, and the test holds it until it is done. No test names a port. The refusing provider is a bound socket that never listens.
- **Processes.** The routing part runs one process per module, not per case. The lifecycle part keeps step 4's process tests. Every subprocess has a bound of 120 seconds, and no secret is in its `argv` (Decision 8).
- **Threads.** One live server for `tests/conformance/`, ended before later modules run (Decision 5). Every scripted provider's `accept` and reads have a 10-second bound. `tests/conformance/conftest.py` arms `faulthandler.dump_traceback_later` around each test, as `tests/conftest.py` does for two modules, so a hang ends the run with every thread's stack instead of hanging CI.
- **Order.** No case depends on another (T3): the reference provider is a function of its request, apart from `deposit`'s totals under a fresh account per run, and each case ends only when the server holds no open connection.
- **Output.** No recorded request, assertion message or subprocess error prints a token, a secret or a principal's sent form.
- **Cost.** The suite runs serially. pytest-xdist is not added: on a 4 GB machine each worker would hold a host and a live server. The pull request reports the suite's time on the reference machine and in CI, and the extra per-call cost of the HTTP leg (K10).
- **Waiting.** Only the routing record's silent-provider case and `stall` wait in real time, each for its own deadline, set at 500 ms or less. That waits for the condition under test, the deadline passing (T3).

## Dependencies and libraries

No runtime dependency is added (K5). No test dependency is added either: the suite uses pytest 9.2.0 (MIT), pytest-django 4.14.0 (BSD 3-Clause) and Hypothesis 6.170.15 (MPL 2.0), which the `test` group already lists, and the standard library's `socket`, `http.client`, `wsgiref.validate`, `hashlib` and `importlib.metadata`. The connector is synchronous (0028), so no async test support is needed. CI gains one action in `ci.yml`, `actions/upload-artifact`, at the commit `release.yml` already pins; the runbook uses `gh`, which it already uses. Libraries weighed and not added:

- **pytest-xdist**: parallel runs; memory on small machines (Decision 11).
- **uvicorn**: an ASGI live server; a test dependency with dependencies of its own, for a leg the endpoint tests already cover (Decision 5).
- **Schemathesis**: generates cases from an OpenAPI document and runs them against a WSGI callable or a URL. A test dependency with a large tree of its own; the hand-written `http.client` caller keeps each assertion on exact bytes that spec 03 fixes, and Hypothesis already draws the contract part's values.
- **pytest's built-in subtests**: not a dependency, but a pytest 9 feature above the floor, and at odds with the spec trace plugin (fact 14).

## Consequences

- Each contract case costs two calls, one over a local socket. The suite's time grows with the contract part, and the pull request measures it.
- A case that one transport handles differently fails in one place, with both outcomes, and the run says which leg diverged. A difference that the specification allows has to be written into CNF-new-r1's exceptions, in the specification, not hidden in a test.
- No public API is added. `over_http` stays private, and step 8 decides its public form with the constraints of Decision 6.
- The test host gains three reference apps, a caller backend and per-run signing secrets. Tests that list the hosted apps change with them.
- Spec 06 changes from an outline to a specified document: the reference contract by its document, the case files, the item table with IDs, the list of requirements that bind others, CNF-new-r1, CNF-new-r2 and the claim. Its `IDs` column in `spec/README.md` stays `CNF`. `spec/conformance/` joins the docs site.
- `scripts/spec_trace.py` gains `--conformance`, the case-file and `conformance_case` checks, `[not-implemented]`, and format 2 of the report. Its docstring lists each new check. ADR 0018's rules for `enforce` and `[not-tested]` are unchanged.
- django-neolith's run reports `conforms: false` until step 8 shows OVR-5. That is the honest state of an alpha.
- CI uploads four conformance runs per commit, and the release runbook attaches the release commit's four to the GitHub release.
- The routing part's cases are the routing record's table (its Decision 9); a change there changes the case file and its test in the same pull request.
- 0028 gains `Narrowed by 0032 (Decision 5)`: the contract suite's live server is neolith's own on `127.0.0.1`, not a `LiveServerTestCase` on `localhost`.

Follows principles 5 (*same semantics over same mechanics*), 8 (*one test suite over many code paths*), 10 (*Django-native over framework-new*), 11 (*specification over implementation*), 12 (*small core over broad features*) and 14 (*recorded over remembered*).

## Open questions

1. **Running the suite against another implementation.** The case files and the reference document let another implementation write its own runner today. A driver that runs this repository's code against it needs a way to ask another connector to make one named call; gRPC's interop client, which runs one case by `--test_case=<name> --server_host --server_port`, is the precedent to start from. Whether the suite then ships as a distribution of its own waits for a second implementation.
2. **An ASGI leg in the contract part.** 0.1 already serves the provider side under ASGI, checked row by row in the endpoint tests. A third entry in `over_both`'s transports would run every contract case under it, at the cost of an ASGI server as a test dependency.
3. **A public `over_http`**, decided with step 8's example apps, under the constraints of Decision 6 (0014 open question 4, the seam part).
4. **Opening the transport interface** (0014 open question 4, the rest of it). `over_both`'s tuple of transports is the place a third transport would join; the record that opens the interface decides whether CNF-new-r1 then names every transport or only those this specification defines.

## References

- Django 5.2.19 and 6.1.3: `django/test/testcases.py` (`LiveServerThread`), `django/core/servers/basehttp.py` (`ThreadedWSGIServer`), `django/test/utils.py` (`setup_test_environment`), `django/core/wsgi.py` (`get_wsgi_application`).
- pytest 9.2.0: `_pytest/config/__init__.py` (`pytest11` entry points), `_pytest/subtests.py`. pytest-django 4.14.0: `pytest_django/fixtures.py`, `pytest_django/plugin.py`. Hypothesis 6.170.15 (`settings`, `HealthCheck`).
- Python: `wsgiref.validate` (https://docs.python.org/3/library/wsgiref.html#module-wsgiref.validate).
- Prior art: gRPC interop test descriptions (https://github.com/grpc/grpc/blob/master/doc/interop-test-descriptions.md); JSON Schema Test Suite (https://github.com/json-schema-org/JSON-Schema-Test-Suite); Jakarta EE TCK process (https://jakarta.ee/committees/specification/tckprocess/); OpenTelemetry compliance matrix (https://github.com/open-telemetry/opentelemetry-specification/blob/main/spec-compliance-matrix.md); web-platform-tests testharness (https://web-platform-tests.org/writing-tests/testharness-api.html); Pact provider states (https://docs.pact.io/getting_started/provider_states); dbapi20 (https://github.com/baztian/dbapi-compliance); anyio's and Hypothesis's `pytest11` entry points; asgiref's `testing.py`; Schemathesis (https://schemathesis.readthedocs.io/).
- django-neolith at `origin/release/0.1.0a3`: `scripts/spec_trace.py`, `scripts/spec_trace_plugin.py`, `scripts/docs.py`, `tests/spec_trace.toml`, `tests/settings.py`, `src/neolith/_server.py`, `src/neolith/_host.py`, `src/neolith/_cli.py`, `src/neolith/_openapi.py`, `tests/conftest.py`, `tests/hosting.py`, `tests/http_peer.py`, `tests/test_call_context.py`, `tests/test_http_client.py`, `docs/releasing.md`, `.github/workflows/`, `spec/`. Since PRs #128 and #129 merged: `src/neolith/_http_transport.py`, `src/neolith/_http.py`, `src/neolith/_endpoint.py`, `spec/03-http-transport.md`, `tests/test_http_transport.py`, `tests/test_provider_endpoints.py`, ADR 0014. The routing record of step 6, as reconciled on 2026-11-19 (its Decisions 2, 4, 5 and 9).
- ADRs 0007, 0008, 0011, 0014, 0015, 0016, 0017, 0018, 0022, 0025, 0027, 0028.
