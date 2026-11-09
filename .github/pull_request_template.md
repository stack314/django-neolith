<!-- Reviewed against CONTRIBUTING.md. The title becomes the squash commit subject, so it follows Conventional Commits (G2, G5). -->

## What changed and why

<!-- G4 -->

## Links

<!-- The issue or decision record this resolves (G4). -->

## Specification and decision records touched

<!-- Name every spec/ document and decision record this changes, or write "None" (G4). A change that contradicts a principle or an Accepted record needs a new record (D1). -->

## Spec trace

<!-- Requirement IDs this adds, changes or withdraws, and the tests that cite them (P6, T5). Write "None" if no requirement changes. For example:

| ID | Change | Tests |
|---|---|---|
| CON-4 | added | tests/test_actions.py::test_connector_never_retries_a_non_idempotent_action |
-->

## How it was verified

<!-- Tests added or run, and the CI matrix result (G4, T1, T4). -->

## Breaking changes

<!-- Only for a type with "!", for example feat!: (G6). Delete this section otherwise. -->

## Checklist

Reviewers look in this order.

- [ ] **Decisions and specification (D, P):** fits the recorded decisions, or includes a new record. New or changed requirements carry IDs and are listed in the spec trace (P5, P6).
- [ ] **Correctness and tests (K, T):** behaviour changes ship with tests, and deprecation warnings are errors.
- [ ] **Writing and privacy (W, X):** one name per concept (W1), and no private references or session trailers (X1).
- [ ] **Hygiene (G):** branch is `<type>/<short-name>` (G1), one logical change (G3).
