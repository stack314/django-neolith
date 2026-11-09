# 0001. Record architecture decisions

- **Status:** Accepted
- **Date:** 2026-11-03

## Context

Neolith makes many decisions that are hard to reverse: public API shape, data rules, wire formats. In an open-source project, contributors arrive later and need to know *why* things are the way they are. Otherwise settled questions get re-argued.

## Options

1. **No formal record.** Decisions live in issues, PRs and chat. Cheap, but scattered and hard to find.
2. **A single design doc.** Good for the big picture, but it gets rewritten in place and loses its history.
3. **Architecture decision records.** One short, numbered, append-only file per decision, kept in the repo.

## Decision

Use ADRs in `docs/adr/`, following the short MADR-style [template](template.md) and the process in the [README](README.md). The vision and principles stay in their own documents. ADRs record the individual decisions made under them.

## Consequences

- A significant change ships with its ADR in the same PR.
- Reversing a decision means writing a new ADR, not editing the old one.
- There is a small writing cost per decision, which is deliberate.
