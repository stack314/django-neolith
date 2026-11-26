# 0037. The example apps show a change of mode: one project in `examples/`, run as separate hosts by the implementation's own tests, verifies OVR-4 and OVR-5, and `over_http` stays private in 0.1

- **Status:** Proposed
- **Date:** 2026-11-26
- **Accepted by:** the pull request that carries the first code built on it. In #15's plan that is step 8's task 4, which adds `examples/` and its boundary check.
- **Narrows:** [0032](0032-the-conformance-suite-runs-each-case-once-per-transport.md) (the `suite` member of Decision 9's run, Decision 3 here). When this record is accepted, 0032 gains `Narrowed by 0037 (Decision 3)` under its status (D2); nothing else in it changes.
- **Answers:** 0032's open question 3, which is also the seam part of [0014](0014-pluggable-connector-transports.md)'s open question 4, for 0.1 (Decision 2).
- **Leaves unchanged:** [0031](0031-a-contract-version-is-routed-by-single-unless-local-first-is-named.md) Decision 3, whose arrangement the example builds as written; 0032's four parts and their case files; [0017](0017-call-context-explicit-at-the-action-ambient-for-propagation.md)'s Consequences (Decision 4); every requirement ID.

## Context

Roadmap step 8 (#15) is "Two small example apps under `examples/` showing attached, detached and both-at-once; the 0.1 proof", and its spec column reads "all" (`docs/roadmap.md`); 0032 Decision 10's table assigns spec 00 to it. Three Accepted sentences leave something to it.

- **The claim.** Spec 00, OVR-5: "Changing mode MUST NOT require any change to the app or to its callers." 0032 Decision 9: "The one `[not-tested]` entry left that the run reports as `not-tested` is then OVR-5, so django-neolith's run reports `conforms: false` until OVR-5 is shown, at step 8, which is the 0.1 proof in the roadmap." Spec 06, *Claiming conformance*: an implementation conforms "when one run of the suite reports every must-level requirement of every document in the version as passed", apart from those that bind others. OVR-4, a MAY, has no citing test either: "The same app MAY run attached and detached at the same time."
- **The seam.** 0032 Decision 6: "`over_http` stays private in 0.1 ... The first users of a public seam are step 8's example apps, which show an app attached and detached; that step's record decides whether and how it opens (open question 3)." It names three things that go with a public seam: loopback origins only; a context manager that refuses use as a decorator, or wraps a coroutine function correctly; and documentation that it applies to the whole process.
- **The run's hash.** 0032 Decision 9 defines the run's `suite` member with `"tests": <SHA-256 over the sorted paths and contents of every file under tests/, __pycache__ aside>`, "so a published run names the suite it passed, with every app, provider and helper its cases drive. `tests/` ships in the sdist, so the hash can be computed there too."

### Facts this record rests on

1. **A process hosts one set of apps.** [0022](0022-the-app-host-builds-settings-and-checks-before-it-serves.md)'s admitted set refuses a later set-up in the same process with other hosted apps: "a process hosts one set of apps". Attached and detached are therefore different processes, and a test of a change of mode starts each host as its own `neolith run`.
2. **A provider in the host and a remote one are never beside each other in 0.1.** 0031 Decision 3 refuses an identity provided in the host and also configured remotely, under every policy. "Both at once" is an arrangement of the deployment: one host holds the app attached and serves its contracts over HTTP, another holds it detached, and a third lists both under `local-first`. "Roadmap step 8's both-at-once example shows this arrangement."
3. **No conformance part runs one project as several neolith hosts.** Each of 0032's four parts drives one neolith host at a time (Decision 2's table, *Drives*): neolith's connector or provider against the suite's reference or scripted ends, "a host in a process of its own, configured through settings", or "hosts that fail to start, and one app booted alone". 0032 Decision 8 weighed "a second neolith host as the backup provider" and left it out: "It costs a second process, and its outcome depends on that host, not on the policy." A case that ran several hosts of one project would be a fifth kind of part, with its own case file form and its own place in 0032's admitted set.
4. **Spec 06 already admits a requirement with no case.** "02's TRN-73, the share of the connect phase, and TRN-74, the text of a `timeout` after a move, have no case: no case on one machine holds a connect phase open, or lets a deadline pass after a move without waiting for it. The implementation's own tests verify them." The conformance run is written from the whole test run's report (`scripts/spec_trace.py --conformance`), so a test outside `tests/conformance/` that cites an ID counts in the same run.
5. **`neolith run` finds modules from its working directory.** It puts that directory on `sys.path` (`src/neolith/_cli.py`), so an example project runs from its own directory, and a test runs it from a copy in `tmp_path`.
6. **The packages.** The wheel carries `src/neolith` only (`[tool.hatch.build.targets.wheel]`, `packages = ["src/neolith"]`). The sdist carries every file git tracks, through an anchored list of top-level paths in `[tool.hatch.build.targets.sdist]`, and `scripts/sdist_check.py` holds a built sdist to `git ls-files`, so a tracked `examples/` has to join that list.
7. **The declaration admits an app that accepts no principal.** `principal_from=()` is admitted; the refusal's own advice says "or write principal_from=() for none" (`src/neolith/_declaration.py`, `_check_principal_from`). A 0.1 host can still carry a principal without a battery: through a project's own `PrincipalBackend` (0023 Decision 2), or through `command_scope(principal=...)` for a command or background work. Whether a caller sends one is the deployment's choice, not something a closed host forces.

## Options

1. **One example project, run as separate hosts by the implementation's own tests, with spec 06 saying so.** The tests drive real `neolith run` processes, which is what a deployer runs. The conformance suite keeps its parts.
2. **A fifth conformance part that starts several hosts.** Every implementation would inherit a case form that names processes, working directories and settings modules, which are this implementation's, not the specification's (P2).
3. **Show OVR-5 in one process through `over_http`, made public.** A test would call the provider in-process, then through the seam, with the same caller code. That is the contract part's comparison again (0032 Decision 4): it changes the transport, not the mode, since one host still holds both apps and the deployer's settings never change. It also opens a public API to prove a claim the processes prove directly.
4. **Leave OVR-5 `not-tested` and the run `conforms: false` in 0.1.** Honest, and leaves the roadmap's proof undone.

## Decision

Option 1.

### 1. The implementation's own tests verify OVR-4 and OVR-5, from one example project run as separate `neolith run` processes

The tests copy `examples/` to a temporary directory and start each host as `neolith run <apps> --settings <module>` from that directory. Between the attached and the detached runs only the apps named, the settings module and the environment change; the app code is hashed before and after and does not change. A test cites OVR-5 when it shows one outcome attached and detached; a test cites OVR-4 when it shows one app attached in one host and detached in another, with a third host routing between them under `local-first` (0031 Decision 3). No case part is added, so 0032's admitted set of parts is kept.

Spec 06 gains, after the paragraph on TRN-73 and TRN-74:

> 00's OVR-4, an app run attached and detached at the same time, and OVR-5, a change of mode that changes neither the app nor its callers, have no case: each needs one project run as several hosts, each in a process of its own. The implementation's own tests verify them.

In spec 06, "00" links to the document, as "02" does in the paragraph before. This changes what spec 06 asks of a conformance claim, so it comes with this record (spec/README.md: "A change that alters meaning gets a decision record first"). The sentence names no requirement ID of its own and withdraws none.

*Why:* OVR-5 is a claim about deployment, and a deployment is processes with settings and an environment. A test that runs those processes checks the claim itself; a model of it in one process checks something nearer the transport (Option 3). Spec 06 already has the form for a requirement no case can hold (fact 4).

### 2. `over_http` stays private in 0.1

0032's open question 3 is answered for 0.1: no public seam. The proof uses real loopback HTTP between processes, so it needs none. How an app author tests their own contracts over HTTP becomes an item in `docs/open-questions.md`, added by the pull request that accepts this record:

> **Testing an app's contracts over HTTP.** neolith's own suite runs a contract case in-process and over HTTP through a private seam, `over_http` (ADR 0032 Decision 6), and the example project runs its apps as separate hosts (ADR 0037). Open: whether app authors get a public seam, and in what form. A public seam admits only loopback origins; it is a context manager that refuses use as a decorator, or one that wraps a coroutine function correctly; and its documentation says it applies to the whole process, every thread included. Needs its own ADR.

*Why:* 0032 Decision 6 put the public seam to step 8 because the example apps were to be its first users. They do not need it. A public seam is an API every later release keeps, and its admitted set was shaped for this repository's own tests (0032 Decision 6, the alternative "Make `over_http` public now"); no user in 0.1 asks for it yet. In the item, the two records are linked, as the file's other items link theirs.

### 3. The run's `suite.tests` hash covers `examples/` too

0032 Decision 9 says: "`"tests": <SHA-256 over the sorted paths and contents of every file under tests/, __pycache__ aside>`" and "`tests/` ships in the sdist, so the hash can be computed there too."

**The narrowing.** The member reads: `"tests": <SHA-256 over the sorted paths and contents of every file under tests/ and examples/, any __pycache__ aside and examples/.data aside>`. Paths are taken from the repository root, so a file moved between the two directories changes the hash. `tests/` and `examples/` both ship in the sdist (Decision 4), so the hash can still be computed there. Every other member of the run, and what `conforms` means, is unchanged.

*Why:* the run should name what its passing tests drove, and the OVR-4 and OVR-5 tests drive `examples/`. `examples/.data` is where the guide's hosts keep their databases by default; running the example writes it, git ignores it (*Consequences*), and it is not part of the example.

### 4. `examples/` lives in this repository, ships in the sdist and not the wheel, follows the code rules, and declares no principal caller

- **Place.** `examples/` at the root of this repository, beside `tests/`, so a change to neolith and the example that shows it land in one pull request. It joins the sdist's include list, as every tracked path must (fact 6), and stays out of the wheel: the example is something to read and run from a checkout or an sdist, not a package to install.
- **Rules.** It follows the code rules (CONTRIBUTING, *Code*: "Tests and examples follow them too"). Its apps import each other's contract package only (K8), and `neolith check` holds them to that ([0030](0030-the-boundary-check-uses-the-standard-library-only.md)). Its host modules read every secret from the environment (K9: "No secrets in code, tests or fixtures"), and every address too, which is the example's own choice. A host module that needs a secret and finds none stops at import with a message that names the variable, never with a bare `KeyError`. A host that takes no calls over HTTP needs no secret.
- **Names.** The example's host modules and environment variables belong to the example. They are not neolith settings, and neolith reads none of them. The example adds no public name, setting, command or dependency to the `neolith` package.
- **Version words.** The example's guide names no version or status word, so the release runbook's search for them (`docs/releasing.md`) has nothing to change there.
- **The principal.** The providing app declares `principal_from=()`. This is the example's choice: its caller acts on no user's behalf, and its hosts configure no principal backend, so the example shows a change of mode and nothing more (fact 7). The guide shows where the entry goes when a caller sends a principal.

  This narrows nothing of 0017. Its Consequences sentence, "The roadmap's 0.1 skeleton, where two apps call each other, includes those entries", is about 0.1 as `docs/roadmap.md` defines it, and this repository's test host apps that call each other carry those entries. The example's caller acts for no user, which 0017's sentence before it does not cover.

## Consequences

- When this record is accepted, OVR-5 leaves `[not-tested]` once its test passes, and spec 00 joins `enforce`, as 0032 Decision 10's table assigns to step 8. django-neolith's run can then report `conforms: true`.
- 0032 gains `Narrowed by 0037 (Decision 3)`, and `docs/open-questions.md` gains the item of Decision 2, in the pull request that accepts this record. 0017 is unchanged. `over_http` stays private and unchanged.
- Spec 06 gains Decision 1's sentence, and `scripts/spec_trace.py` hashes `examples/` into `suite.tests` with a test, in the pull request that builds the OVR-5 test.
- The sdist gains `/examples`, `.gitignore` gains `examples/.data/`, and ruff and mypy run over `examples/` as over `tests/`, in the pull request that adds `examples/`. The wheel is unchanged.
- A run's `suite.tests` changes when the example changes, as it does for any file under `tests/`.
- The example's tests start several processes each, which costs more than an in-process test; each pull request that adds them reports their time (K10).
- One host never calls an app both in-process and remotely in the example, as 0.1 does not admit it (0031 Decision 3).

Follows principles 8 (*one test suite over many code paths*), 11 (*specification over implementation*), 12 (*small core over broad features*) and 14 (*recorded over remembered*).

## References

- Spec [00](../../spec/00-overview.md), OVR-4 and OVR-5; spec [06](../../spec/06-conformance.md), the paragraph on TRN-73 and TRN-74 and *Claiming conformance*.
- [0031](0031-a-contract-version-is-routed-by-single-unless-local-first-is-named.md) Decision 3; [0032](0032-the-conformance-suite-runs-each-case-once-per-transport.md) Decisions 4, 6, 9 and 10 and open question 3; [0014](0014-pluggable-connector-transports.md) Decisions 6 and 7 and open question 4; [0017](0017-call-context-explicit-at-the-action-ambient-for-propagation.md), Consequences; [0023](0023-a-request-principal-comes-from-ordered-backends.md) Decision 2.
- Roadmap step 8 and its plan, #15.
