#!/bin/sh
# The build command of the neolith-docs Worker on Cloudflare Workers Builds
# (ADR 0024), run from the repository root. Workers Builds picks its Python
# with the build variable PYTHON_VERSION; the site needs 3.13 or newer. PYTHON
# names another interpreter, as on a machine without a plain "python". The
# venv keeps the install out of the interpreter's own packages. The install
# line is the one CI's docs step runs, and publish exits 1 on any problem, so
# a site that fails a check is never deployed.
set -eu
python="${PYTHON:-python}"
"$python" --version
"$python" -m venv build/docs-venv
build/docs-venv/bin/python -m pip install . --group docs "Django~=5.2.0"
build/docs-venv/bin/python scripts/docs.py publish
