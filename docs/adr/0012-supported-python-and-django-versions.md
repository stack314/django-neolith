# 0012. Supported Python and Django versions

- **Status:** Accepted
- **Accepted:** 2026-11-09
- **Date:** 2026-11-03

## Context

As of this ADR, Django 6.1 is current, 5.2 is the active LTS (supported to April 2028), and 6.2 LTS is expected in April 2027. Django 6.x requires Python 3.12+. Neolith targets long-lived, professional codebases, which typically run LTS releases. Many corporate codebases are anchored on Python 3.12 and Django 5.2 today, so that floor is crucial for adoption.

A wide support range has a known failure mode: code written for the floor keeps using APIs that later releases deprecate and then remove, and the project discovers this only when it tries to move up.

## Options

1. **Latest only** (Django 6.1+, Python 3.12+). Least to test, but excludes LTS users, who are the core audience.
2. **Current LTS onward** (Django 5.2 LTS and 6.x, Python 3.12+).
3. **Broad** (Django 4.2+, Python 3.10+). A wide matrix for a v0.x project, and 4.2 LTS is already past end of life.

## Decision

Option 2, with a planned upper milestone and a rule against deprecated APIs.

- **Floor:** Django 5.2 LTS on Python 3.12. Every release of neolith supports it until Django 5.2 reaches end of support; neolith then drops it in its next minor release.
- **Planned milestone: Python 3.15 and Django 6.1.** Neolith is verified on this pair before 0.1 is released, as soon as Django declares support for Python 3.15. From then on, the top of the CI matrix tracks the newest Django on the newest Python it supports.
- **CI matrix:** at least the two ends, Django 5.2 on Python 3.12 and the milestone pair, on x86-64 and arm64.
- **No deprecated APIs.** Neolith does not use a Python or Django feature that is deprecated or scheduled for removal when a newer approach exists. Tests run with deprecation warnings as errors (`DeprecationWarning`, `PendingDeprecationWarning` and Django's `RemovedInDjango*Warning`), so a deprecated call fails CI on whichever version first warns about it.
- **When the newer approach is above the floor**, the version-dependent code lives in one compatibility module, `neolith._compat`, with a comment naming the version that lets it go. No version checks elsewhere in the code base.

## Consequences

- Teams on the current LTS can adopt neolith without upgrading Django first.
- Python 3.12 lets us use modern typing (PEP 695 generics) in contract definitions.
- CI covers arm64 as well as x86-64.
- Deprecation warnings raised by third-party packages are filtered by package name, so they cannot hide neolith's own. Each filter names the upstream issue it waits on.
- Moving the floor up is a small change, because nothing outside `neolith._compat` depends on the old versions.
