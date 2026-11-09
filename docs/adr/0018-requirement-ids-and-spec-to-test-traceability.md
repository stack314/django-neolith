# 0018. Requirement IDs and spec-to-test traceability

- **Status:** Proposed
- **Date:** 2026-11-10

## Context

[ADR 0015](0015-specification-first-django-reference-implementation.md) (Proposed) makes the specification the definition of neolith and the conformance tests the arbiter. P1 and P4 in the contributing guide ask that the specification, the conformance tests and the code change together.

The specification has no way to name a single requirement. A reference can point only at a document and a heading. Headings move as outlines become prose, and one heading often holds several requirements. A test cannot say which requirement it verifies. Nothing shows a normative statement that no test covers. Another implementation cannot cite a requirement precisely when it reports a difference.

Most of the code is written by AI coding agents and reviewed one pull request at a time. Reviewers need a cheap, mechanical way to see which requirements a change adds or changes and which tests verify them.

Spec-driven development tools were evaluated for this, including GitHub Spec Kit, OpenSpec and Kiro. They bring useful ideas: numbered requirements, a living specification changed by deltas, and an explicit marker for what is still unclear. Each tool also keeps its own specification files in its own layout, per feature or per capability. neolith already has a specification, decision records and a conformance suite. Adopting a tool would mean either moving `spec/` into the tool's layout or keeping two specifications side by side.

## Options

1. **No IDs. Cite documents and headings.** Nothing new to maintain. References break as documents are rewritten, and coverage cannot be checked.
2. **Adopt a spec-driven development tool.** Ready-made prompts, templates and checks, maintained by others. A second specification tree next to `spec/`, in formats built for product features rather than a protocol standard.
3. **Section numbers, such as `01.4.2`.** Familiar from standards documents. They change whenever a section is inserted, and every citation changes with them.
4. **Stable IDs per document, cited by tests and checked in CI.** Small and independent of any agent or tool. The convention and its check are maintained by this project.

## Decision

Option 4.

**IDs.**

- Every statement in `spec/` that uses a normative word carries an ID of the form `<AREA>-<n>`. The ID is written in bold before the statement, as `**<AREA>-<n>.**`.
- Each document has one area, listed in the document table in `spec/README.md`: `OVR` (00), `CON` (01), `TRN` (02), `HTTP` (03), `EVT` (04), `APP` (05) and `CNF` (06). A new document gets its area in the same table.
- Numbers are assigned in order within an area. An ID is never renumbered or reused. A withdrawn requirement keeps its ID, struck through, with the specification version that withdrew it and the reason.
- A statement written as plain fact, without a normative word, gets no ID. Making such a statement normative changes what the specification means, and follows the rules for that in `spec/README.md`.
- An implementation cites a requirement by specification version and ID, for example `spec 0.1 <AREA>-<n>`.

**Trace.**

- A test names the requirements it verifies with the `spec` pytest marker. One test may cite several IDs, and one ID may have several tests.
- Code does not carry requirement IDs. The trace from a requirement to the code runs through the tests that cite it and the pull request that added them.
- `scripts/spec_trace.py` reads `spec/` and the test sources and runs as its own CI job. It fails on a normative statement without an ID, an ID that is malformed or defined twice, and a test that cites an unknown or withdrawn ID. Until roadmap step 6, when the conformance suite runs over both transports, it only reports coverage. From step 6, every must-level requirement in the documents of the current release needs at least one citing test.

**Workflow for a roadmap step.**

1. The issue holds the plan: the specification documents the step implements, the decisions and open questions it depends on, and a task list that names the requirement IDs each task covers.
2. The pull request changes the specification first, with IDs, then adds tests that cite them, then the code.
3. The pull request description carries a *Spec trace* section listing the IDs added, changed or withdrawn, and the tests that cite them.
4. A plan never settles an open question or a Proposed decision record. Those are settled by a record the maintainer accepts.

Follows principles 8 (*one test suite over many code paths*), 11 (*specification over implementation*) and 14 (*recorded over remembered*).

## Consequences

- Tests, reviews, bug reports and other implementations can name a requirement exactly. Requirements without a test are visible in every CI run.
- Every edit to the specification has to place IDs, and withdrawn requirements stay in the documents as struck-through lines.
- The checker is project code. It reads markers statically, so a marker has to name its IDs as literal strings.
- Contributor rules P5, P6, T5 and S4 describe the practice. They apply once this record is Accepted.
- If many contributors start changing the specification in parallel, a tool with per-change folders, such as OpenSpec, may be worth another look. The IDs stay valid either way.
