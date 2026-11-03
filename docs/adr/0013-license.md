# 0013. License

- **Status:** Accepted
- **Accepted:** 2026-11-09
- **Date:** 2026-11-03

## Context

Neolith is an open-source library that adopters embed in commercial Django codebases. Changing the license after outside contributions arrive needs every contributor's agreement, so this is close to a one-way door.

## Options

1. **BSD-3-Clause.** Django's own license, and common across the Django ecosystem. Permissive, with no explicit patent grant.
2. **MIT.** The most common permissive license, and short. No explicit patent grant.
3. **Apache-2.0.** Permissive, with an explicit patent grant and patent-retaliation clause. Longer, and requires a NOTICE file.
4. **Copyleft (GPL or AGPL).** Protects the commons, but deters exactly the commercial teams neolith is for.

## Decision

**BSD-3-Clause.** It matches Django and gives adopters' legal reviews the least friction. Apache-2.0 was the alternative if an explicit patent grant mattered more; it did not outweigh ecosystem consistency. Accepted by the maintainer on 2026-11-09.

## Consequences

- Permissive licensing maximises adoption, but allows proprietary forks.
- `LICENSE` holds the BSD-3-Clause text. `pyproject.toml` declares `license = "BSD-3-Clause"` once it exists.
- No per-file license headers; the repository-level `LICENSE` is sufficient, as in Django itself.
