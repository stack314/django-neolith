# 0012. Supported Python and Django versions

- **Status:** Proposed
- **Date:** 2026-11-03

## Context

As of this ADR, Django 6.1 is current, 5.2 is the active LTS (supported to April 2028), and 6.2 LTS is expected in April 2027. Django 6.x requires Python 3.12+. Neolith targets long-lived, professional codebases, which typically run LTS releases.

## Options

1. **Latest only** (Django 6.1+, Python 3.12+). Least to test, but excludes LTS users, who are the core audience.
2. **Current LTS onward** (Django 5.2 LTS and 6.x, Python 3.12+).
3. **Broad** (Django 4.2+, Python 3.10+). A wide matrix for a v0.x project, and 4.2 LTS is already past end of life.

## Decision

Option 2. Support Django 5.2 LTS and every later release, on Python 3.12+. CI tests the matrix ends: 5.2 on 3.12, and the latest Django on the latest Python. When a Django LTS reaches end of support, neolith drops it in the next minor release.

## Consequences

- Teams on the current LTS can adopt neolith without upgrading Django first.
- Python 3.12 lets us use modern typing (PEP 695 generics) in contract definitions.
- CI covers arm64 as well as x86-64.
