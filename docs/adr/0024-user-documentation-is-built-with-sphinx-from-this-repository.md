# 0024. User documentation is built with Sphinx from this repository

- **Status:** Proposed
- **Date:** 2026-11-12

## Context

Neolith's user documentation is published under `docs.stack314.com/neolith/`. Until now it was a separate static site holding a hand-synced copy of this repository's Markdown. A copy drifts from the code and the specification it describes, and it cannot show the Python API without a second tool that reads the package.

The documentation has four kinds of content: an API reference for the names `neolith` exports; the specification, the decision records and the project documents, which already live here as Markdown; concept and how-to pages; and the status of the project. The site has rules of its own: it is honest about status, its own text names no people and says nothing about how the code is written, mirrored documents stay word for word, a file holding an email address is not published, every page carries the trademark footer, and every page reads without JavaScript.

The library supports Python 3.12 ([ADR 0012](0012-supported-python-and-django-versions.md)). Its docstrings follow Google sections, checked by ruff and `tests/test_api_docs.py`.

## Options

1. **Keep the separate site and sync it by hand.** Nothing new to build. The copy drifts, and the API reference has to be written by hand.
2. **Zensical or Material for MkDocs with mkdocstrings.** Fast, Markdown throughout, and reads the package statically. Zensical is in beta, Material for MkDocs is in maintenance, and a prototype passed a private type in a public signature without a warning.
3. **Sphinx with MyST and autodoc, in this repository.** Mature and stable, and its nitpicky mode refuses every reference it cannot resolve, which found the private types in public signatures. It imports the package, and Sphinx 9.2 needs Python 3.13.

## Decision

Option 3.

**Tools.** Sphinx 9.2, MyST-Parser 5.2 and pydata-sphinx-theme 0.24, with the `dirhtml` builder, in a `docs` dependency group pinned exactly and marked `python_version >= '3.13'`. The library's floor stays 3.12; on an older interpreter the group installs none of them.

**Source.** The site's own pages, its configuration and its Sphinx extension live in `docs/site/`. `scripts/docs.py` stages a source tree under `build/`, builds it and checks it, so a change to the code, its docstrings and its pages lands in one pull request.

**API reference.** The reference covers exactly `neolith.__all__` without `__version__`, generated from the code, one page per group. The group map lives in `scripts/docs.py`, and a name in no group, in two, or not exported fails the build and the test suite. Signatures keep their types, linked to the Python, Django and Pydantic documentation. A reference to a class by its private module path is shown under its public name, a private name the package does not export fails the build, and any `neolith._` left in a generated page fails the check. Each entry lists the requirements and records its docstring cites, with each record's status.

**Mirrored documents.** The specification, the decision records, the vision, the principles, the roadmap, the open questions, the contributing guide and the changelog are published from the same commit, word for word. Only link targets change: a link to a mirrored file leads to its page, and a link to another file of the repository leads to it on GitHub at the built commit, shown as plain text while the repository is private. markdown-it-py parses the source and the staged copy, and every token but a link's target must be equal. A file holding an email address fails the build, so the code of conduct is linked, not mirrored.

**Requirements and citations.** Each requirement gets the anchor of its lowercase ID, such as `#con-12`. In every page and docstring, a requirement ID, `ADR NNNN` or `spec NN` becomes a checked link, read with the one grammar that `tests/test_api_docs.py` uses. A citation of an unknown requirement, record or document fails the build, and so does a withdrawn requirement cited outside a mirrored document; a mirrored document may name one as history. A record whose status is not Accepted says that only Accepted records bind.

**Site rules, checked.** The build runs with `-W -n`. `scripts/docs.py check` then reads every page. On every page it requires the footer, and on a development build `noindex` and the banner "Development version. Not released."; it refuses an email address, anything shaped like a secret, a script or stylesheet from another origin, a link or anchor that leads nowhere, and a page whose `<main>` has no heading and text without JavaScript. On pages the site writes or generates, it also refuses `neolith._`, the word "official", the names of the project's authors and copyright holder, words about how the code is written, an install command before a release, and words about planned behaviour outside a `{planned}` block, which names the roadmap step that delivers it. The word checks are a backstop for review, not a proof.

**Publishing.** The site is served by an assets-only Cloudflare Worker under `/neolith/`, built from this repository: `/neolith/dev/` from `main`, and from the first release `/neolith/X.Y/`, built once at the tag and kept, with `/neolith/latest/` redirecting to the newest. The deployment, the release workflow and the files at the site's root (`versions.json`, `llms.txt`, redirects) are added by the changes that publish the site.

**Gate.** Until the repository is public, `python scripts/docs.py check` is run before review is requested, as an honour gate. Once it is public, a required CI job runs it.

## Consequences

- The reference cannot describe code that does not exist, and adding a public name forces a decision about its group in the same pull request.
- A link from a page to a requirement, record or document is checked on every build, so renaming a spec document or withdrawing a requirement shows every page that cites it.
- Contributors need Python 3.13 or newer to build the site locally. The test suite, which checks the staging, the group map and the HTML rules without Sphinx, runs on every supported Python.
- The extension and the check script are code this project maintains. They are small, and their rules are tested.
- Because the mirrored documents are published word for word, an email address added to one of them stops the build until it is removed or the file is no longer mirrored.
