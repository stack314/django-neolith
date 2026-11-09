# Contributing to django-neolith

Thank you for considering a contribution. This document is the standard every pull request is reviewed against, including the maintainer's own. Each rule has an ID such as **K2**, so a review can say exactly which rule a change meets or misses.

Neolith is young. Nothing is installable yet, and the specification and the reference implementation are being written together. That makes design discussion as valuable as code right now.

## Before you start

- **S1.** Small, obvious fixes (a typo, a broken link, a wrong example) can go straight to a pull request.
- **S2.** For anything else, open an issue first. Describe the problem before the solution, and agree on the approach before writing code. This saves you from building something that conflicts with a recorded decision.
- **S3.** If the change is hard to reverse, shapes public API, adds a runtime dependency or changes what the specification means, it needs a decision record (**D1**). Propose it in the issue or in the same pull request.

## Decisions

The [vision](VISION.md), the [guiding principles](docs/principles.md) and the [architecture decision records](docs/adr/README.md) are binding. They are how the project settles arguments once instead of in every review.

- **D1.** A change that contradicts a principle or an Accepted decision record must come with a new decision record that explains why. Use the [template](docs/adr/template.md) and the next free number, with status Proposed. The maintainer accepts or rejects it.
- **D2.** An Accepted record's decision is never edited. To reverse it, write a new record and mark the old one `Superseded by NNNN`. Typos and broken links in an Accepted record may be fixed.
- **D3.** Record numbers are never reused, including for rejected records.
- **D4.** When an item in [open questions](docs/open-questions.md) is settled, the same pull request records the decision and removes the item.

## Specification

Neolith is defined by a specification under `spec/`, written independently of Django and Python. django-neolith is its reference implementation ([ADR 0015](docs/adr/0015-specification-first-django-reference-implementation.md)).

- **P1.** A change to behaviour the specification defines updates the specification in the same pull request. On `main`, the code and the specification never disagree.
- **P2.** Specification text names no Django or Python construct. Framework-specific detail belongs in the implementation's documentation.
- **P3.** The normative words MUST, MUST NOT, SHOULD and MAY are used in capitals, with their RFC 2119 meaning, only in `spec/`.
- **P4.** A change to the specification changes the conformance tests to match, in the same pull request.

## Code

These rules apply to the `neolith` package. Tests and examples follow them too, except where a rule says otherwise.

- **K1.** The public API is fully type-annotated, and the type checker passes with no new ignores. An ignore that is unavoidable carries a comment saying why.
- **K2.** Fail at startup, not at first use. Misconfiguration, an unsatisfied contract requirement or a contract that does not match its implementation is detected at import or when the host starts, with a message that names the cause.
- **K3.** No placeholders in shipped code: no `TODO`, `FIXME`, stub bodies or `NotImplementedError` on a reachable path. Unfinished work stays on a branch or in an issue. The `...` body of a contract action is a declaration and is allowed.
- **K4.** No deprecated APIs. Neolith does not use a Python or Django feature that is deprecated or scheduled for removal when a newer approach exists. Code that differs by version lives only in `neolith._compat` ([ADR 0012](docs/adr/0012-supported-python-and-django-versions.md)).
- **K5.** No new runtime dependency without an Accepted decision record. Every dependency becomes a dependency of every adopter. Development-only dependencies need a one-line reason in the pull request.
- **K6.** No hidden state. The call context is passed explicitly, never read from thread-local or global state. Process-wide state is owned by the host and documented.
- **K7.** Errors that cross a contract are contract errors with a defined kind. A transport's own exceptions never reach the caller.
- **K8.** The boundary applies to our own code. An example or test application imports another application's contract package only, never its models, services or signals ([ADR 0003](docs/adr/0003-apps-communicate-through-contracts.md)).
- **K9.** Secure by default. No secrets in code, tests or fixtures. No disabled TLS verification or authentication outside test code. Every value arriving over a transport is validated before it reaches an implementation.
- **K10.** Performance claims come with a measurement. A change made for speed shows the benchmark and its numbers in the pull request.

## Tests

- **T1.** Every behaviour change ships with tests. A bug fix ships with a regression test that fails without the fix.
- **T2.** Contract behaviour is tested through the shared contract suite, which runs over every transport. A test that exercises only one transport needs a reason.
- **T3.** Tests are deterministic. No sleeping to wait for something, no network beyond the local machine, and no dependence on test order.
- **T4.** The full CI matrix passes before review is requested. Deprecation warnings are errors ([ADR 0012](docs/adr/0012-supported-python-and-django-versions.md)).

## Writing

These rules apply to documentation, docstrings, error messages, commit messages and pull request text.

- **W1.** Use the project's terms with one meaning each: *application*, *contract*, *action*, *contract package*, *provider*, *connector*, *transport*, *event*, *declaration*, *host*, *mode* (*attached* or *detached*). Do not use *interface*, *client*, *adapter*, *port*, *binding* or *service* as names for these.
- **W2.** Describe trade-offs. Do not call Django, other projects or other approaches wrong. Credit prior work where it is relevant.
- **W3.** No unverifiable claims, such as *first*, *only*, *fastest* or *best*, unless a source is linked.
- **W4.** The principles, decision records and specification never address the reader as *you*. Guides and this document may.
- **W5.** The vision, principles and decision records stay abstract, with names like `<contract>.v1`. Concrete examples belong in the specification and guides, and should vary rather than repeat one domain.
- **W6.** Plain language. Short sentences, one idea each. Prefer a list to a long paragraph when items are parallel.

## Privacy

- **X1.** Nothing committed or posted to this repository refers to private or environment-specific things: internal or chat links, session links, hostnames, machine or hardware names, personal file paths, or credentials. This includes commit trailers, which must be removed before pushing.

## Commits and pull requests

- **G1.** Work on a branch named `<type>/<short-name>`, for example `fix/connector-timeout`. Never push to `main`.
- **G2.** Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/). Types: `feat`, `fix`, `docs`, `spec`, `test`, `refactor`, `perf`, `build`, `ci`, `chore`. The subject is imperative, starts in lower case after the type, and is at most 72 characters. The body says why.
- **G3.** One logical change per pull request. Unrelated clean-up goes in its own pull request.
- **G4.** The pull request description says what changed and why, links the issue or decision record, names every specification document and decision record it touches, and says how the change was verified.
- **G5.** Pull requests are squash-merged. The pull request title becomes the commit subject, so it follows **G2**.
- **G6.** A breaking change uses `!` after the type, for example `feat!:`, and has a *Breaking changes* section in the description. Before 1.0, breaking changes are allowed but never silent.
- **G7.** Merging needs the maintainer's approval, and every review comment answered or resolved.

## How review works

Every pull request is reviewed against this document. Review comments cite the rule they apply, for example "**K2**: this is only detected on the first call". A pull request that breaks a rule is changed, or the exception is agreed in writing in the pull request by the maintainer.

Reviewers look in this order, because an early problem can make later ones moot:

1. **Decisions and specification** (D, P): does the change fit what is recorded, and is a new record needed?
2. **Correctness and tests** (K, T): does it work, fail safely, and prove it?
3. **Writing and privacy** (W, X).
4. **Hygiene** (G).

## Responsibility

- **R1.** You are responsible for every line you submit, however it was produced, including with AI tools. You should be able to explain any part of the change and why it is needed.

Be respectful and assume good faith, in issues, reviews and everywhere else in the project.

## Reporting a security issue

Do not open a public issue. Report it privately through the repository's Security tab ("Report a vulnerability"). You will get a reply before any details are made public.

## License

Contributions are accepted under the project's [BSD 3-Clause License](LICENSE). By submitting a pull request, you agree that your contribution is licensed under it.
