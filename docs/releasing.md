# Releasing django-neolith

This is the checklist for publishing a release to PyPI. Follow it from top to bottom, every release. Each step gives the command to run and what it should print. Steps marked **(maintainer)** are done by the maintainer; anyone with write access can do the rest.

A release walks through these branches and pull requests ([CONTRIBUTING G5, G7 and G8](../CONTRIBUTING.md#commits-and-pull-requests)). It is built on its own branch, `release/<version>`, made from `main` when the previous release was published. Each work pull request into that branch is squash-merged, so it adds one commit there.

1. A **version pull request** into `release/<version>` sets the version and dates the changelog section. It is squash-merged: one commit on the release branch.
2. A **release pull request** from `release/<version>` into `main` lists the changes and every decision record whose status changes. The maintainer merges it with a merge commit: one commit on `main`, whose second parent is the tip of the release branch. GitHub then deletes the release branch.
3. An annotated **tag** `v<version>` on that merge commit starts [the release workflow](../.github/workflows/release.yml). It builds and checks the files, smoke-tests the wheel, and waits for the maintainer to approve the `release` deployment. Then it publishes to PyPI. From then on, the tag marks the release.
4. A **GitHub Release** for the tag carries the changelog section as its notes. It adds no commit.
5. The **next release's branch**, `release/<next version>`, is made from `main`, at the release's merge commit. It adds no commit.
6. A **`.dev` pull request** into that branch starts the next development version. It is squash-merged: the first commit on the new branch.

A hotfix walks the same path on a second release branch, made from `main` while the open release branch stays as it is ([section 9](#9-when-something-goes-wrong)).

Lines marked *Recommendation* are this runbook's advice, not a rule the project has recorded.

## Variables

Every command below reads these. Set them once, in a bash or zsh shell, from a clone of the repository whose `origin` is `stack314/django-neolith` on GitHub.

```
VERSION=0.1.0a2                        # the version this release publishes
BRANCH="release/${VERSION}"            # the branch it is built on
PREVIOUS=0.1.0a1                       # the last published version
NEXT_VERSION=0.1.0a3                   # the version the next release is expected to have
NEXT_BRANCH="release/${NEXT_VERSION}"  # the branch made for it after this release
NEXT_DEV="${NEXT_VERSION}.dev0"        # the development version that branch starts with
REPO=stack314/django-neolith
PY_FLOOR=python3.12                    # pairs with Django 5.2 (ADR 0012)
PY_NEWEST=python3.15                   # pairs with Django 6.1 (ADR 0012)
WORK=$(mktemp -d)                      # builds, virtual environments and notes
git fetch origin --tags
```

If `PY_FLOOR` or `PY_NEWEST` is not installed, use the closest Python you have, and say which one you used in the release pull request. CI and the release workflow still run the exact pairs.

## 1. Before you start

- [ ] `gh` can act on the repository.
  ```
  gh auth status
  ```
  Expected: logged in to github.com, with the `repo` scope.
- [ ] The version is free on PyPI. A version number is never reused, even one that was yanked.
  ```
  curl -s -o /dev/null -w '%{http_code}\n' "https://pypi.org/pypi/django-neolith/${VERSION}/json"
  ```
  Expected: `404`.
- [ ] Everything meant for this release is merged. Each release has a milestone named after its version.
  ```
  gh issue list --repo "$REPO" --milestone "${VERSION}" --state open
  gh pr list --repo "$REPO" --base "$BRANCH" --state open --search "milestone:${VERSION}"
  ```
  Expected: no pull requests, and no issues except the ones that track the release itself. Move anything that will not ship to the next milestone, with a one-line comment saying why.
- [ ] CI passed on the tip of the release branch.
  ```
  gh run list --repo "$REPO" --workflow ci.yml --commit "$(git rev-parse "origin/${BRANCH}")" \
    --json status,conclusion -q '.[] | "\(.status) \(.conclusion)"'
  ```
  Expected: `completed success`. No output means no run exists yet for that commit.
- [ ] Everything on `main` is on the release branch. After a hotfix, this holds only once `main` was merged into the open release branch ([section 9](#9-when-something-goes-wrong)).
  ```
  git merge-base --is-ancestor origin/main "origin/${BRANCH}" && echo "main is in ${BRANCH}" || echo "MAIN NOT IN ${BRANCH}"
  ```
  Expected: `main is in release/<VERSION>`. If not, merge `main` into the release branch as section 9 describes, before going on.
- [ ] You know what is on the release branch since the last release.
  ```
  git log --first-parent --format='- %s' "v${PREVIOUS}..origin/${BRANCH}"
  ```
  Compare it with `## [Unreleased]` in `CHANGELOG.md` by reading both: every change a user can notice needs an entry. Keep this list for the release pull request.
- [ ] You know which decision records change status.
  ```
  git switch --detach "origin/${BRANCH}"
  git rev-parse -q --verify "v${PREVIOUS}^{commit}" >/dev/null || echo "MISSING TAG v${PREVIOUS}"
  for f in docs/adr/[0-9]*.md; do
    old=$(git show "v${PREVIOUS}:${f}" 2>/dev/null | grep -m1 '^- \*\*Status' || echo "(new)")
    new=$(grep -m1 '^- \*\*Status' "$f")
    [ "$old" != "$new" ] && echo "$f: $old -> $new"
  done
  ```
  Expected: one line per record that is new or changed status, and no `MISSING TAG` line. Keep the output for the release pull request.
- [ ] Every specification document whose roadmap step is complete in this release is enforced by the spec trace ([T5](../CONTRIBUTING.md#tests)).
  ```
  grep '^enforce' tests/spec_trace.toml
  ```
- [ ] For 0.1 itself: the gate in the [roadmap](roadmap.md) holds, verified on Python 3.15 with Django 6.1 ([ADR 0012](adr/0012-supported-python-and-django-versions.md)).

## 2. The version pull request into the release branch

- [ ] The version follows [PEP 440](https://peps.python.org/pep-0440/) in its normalized form: `0.1.0a2` for an alpha, `0.1.0b1` for a beta, `0.1.0rc1` for a release candidate, `0.1.0` for a final release. It is never a `.dev` version, and it sorts after the last release.
  ```
  python - "${PREVIOUS}" "${VERSION}" <<'PY'
  import sys
  from packaging.version import Version

  previous, version = sys.argv[1], sys.argv[2]
  assert str(Version(version)) == version, f"not normalized: write {Version(version)}"
  assert not Version(version).is_devrelease, "a release is never a .dev version"
  assert Version(previous) < Version(version), f"{version} does not sort after {previous}"
  print(version, "is a pre-release" if Version(version).is_prerelease else "is a final release")
  PY
  ```
  Expected: `0.1.0a2 is a pre-release` (or `is a final release`). `packaging` is in the `test` dependency group.
- [ ] Make a branch from the release branch.
  ```
  git switch -c "chore/version-${VERSION}" "origin/${BRANCH}"
  ```
- [ ] Find every line that carries the version or a status.
  ```
  git grep -nwiE "alpha|beta|${PREVIOUS}|--pre|no release yet|Development Status|draft 0\.1" \
    -- ':!CHANGELOG.md' ':!tests' ':!docs/adr' ':!spec/0*' ':!docs/releasing.md'
  ```
  Expected: lines only in the files listed below (`README.md`, `pyproject.toml`, `docs/site/index.md`, `CONTRIBUTING.md`, `SECURITY.md` and `spec/README.md`). If a line appears in another file, add that file to this list in the same pull request.
- [ ] `src/neolith/__init__.py`: `__version__ = "<VERSION>"`. This is the only place the version is set; the build reads it, and the release workflow checks the tag against it.
- [ ] `CHANGELOG.md` ([Keep a Changelog](https://keepachangelog.com/en/1.1.0/)):
  - [ ] `## [Unreleased]` becomes `## [<VERSION>] - <YYYY-MM-DD>`, dated the day you expect to tag.
  - [ ] A new, empty `## [Unreleased]` sits above it. *Recommendation*, following Keep a Changelog.
  - [ ] The links at the bottom: `[Unreleased]` compares the new tag with `main`, since `main` and the tag keep this file and the release branch will be gone; the `.dev` pull request points it at the next release branch (section 8). The new version links to its release page.
    ```
    [Unreleased]: https://github.com/stack314/django-neolith/compare/v<VERSION>...main
    [<VERSION>]: https://github.com/stack314/django-neolith/releases/tag/v<VERSION>
    ```
  - [ ] Every breaking change is listed under *Changed* or *Removed* ([G6](../CONTRIBUTING.md#commits-and-pull-requests)). These commands list the pull requests that declared one:
    ```
    git log --format='%s' "v${PREVIOUS}..origin/${BRANCH}" | grep -E '^[a-z]+(\([^)]*\))?!:'
    ```
- [ ] `README.md`. This file is also the description on PyPI.
  - [ ] The status line: `**Status: alpha (<VERSION>). The API may change before 0.1.**`, with the right status word.
  - [ ] The install line: `pip install --pre django-neolith` while the release is a pre-release. *Recommendation:* for a final release, decide whether `--pre` stays, since it also admits pre-release versions of the dependencies.
  - [ ] The *Features* list, its "Not yet in this version" line and the example describe only what this version contains, as the changelog lists it up to this version. Between releases, `main` keeps this README, so it must not describe unreleased API.
  - [ ] The specification's status in *Links* matches `spec/README.md`.
- [ ] Files that ship in the package or are built at a tag (the README, and the documentation site's pages) say "this version" and name no branch, since a reader sees them long after the branch has moved.
  ```
  git grep -nE '`(main|release/[^`]*)`|\b(main|release) branch' -- README.md docs/site
  ```
  Expected: no output. Reword any line it finds, in this pull request.
- [ ] `pyproject.toml`:
  - [ ] The `Development Status` classifier matches the status word: `3 - Alpha` for an alpha. *Recommendation:* `4 - Beta` for a beta or release candidate, and `5 - Production/Stable` only from 1.0.
  - [ ] `requires-python`, the `Programming Language :: Python :: 3.N` classifiers and the `Framework :: Django :: X.Y` classifiers match [ADR 0012](adr/0012-supported-python-and-django-versions.md) and the CI matrix in `.github/workflows/ci.yml`.
- [ ] `docs/site/index.md`: the status line.
- [ ] `CONTRIBUTING.md`: "It is in alpha" in the opening paragraph.
- [ ] `SECURITY.md`: *Supported versions* describes the released versions.
- [ ] `spec/README.md`: the specification's status line. It changes only when the specification's own status changes.
- [ ] Commit, push and open the pull request into the release branch, in the milestone. Its title follows [G2](../CONTRIBUTING.md#commits-and-pull-requests). *Recommendation:* `chore: set version <VERSION>`.
  ```
  gh pr create --repo "$REPO" --base "$BRANCH" --title "chore: set version ${VERSION}" \
    --body-file "$WORK/version-pr.md" --milestone "${VERSION}"
  ```
- [ ] It is reviewed, CI passes, and it is squash-merged ([G5, G7](../CONTRIBUTING.md#commits-and-pull-requests)).

*Recommendation:* from here until the release pull request merges, merge nothing else into the release branch. The release pull request's head is the release branch itself, so a later merge would join the release unverified. Step 4 guards this with `--match-head-commit`.

## 3. Verify what will be released

Run every check on the tip of the release branch after the version pull request merged. If one fails, fix it by a pull request into the release branch, and run this section again from the start.

- [ ] Check out that commit in a clean worktree, and record it.
  ```
  git fetch origin
  git worktree add --detach "$WORK/src" "origin/${BRANCH}"
  cd "$WORK/src"
  VERIFIED=$(git rev-parse HEAD); echo "$VERIFIED"
  grep -qx "__version__ = \"${VERSION}\"" src/neolith/__init__.py && echo "version ${VERSION}" || echo "WRONG VERSION"
  ```
  Expected: a SHA, then `version <VERSION>`. Keep `VERIFIED` for steps 4 and 5.
- [ ] The full suite and the spec trace, on both pairs, from the built package as CI installs it.
  ```
  for DJANGO in 5.2 6.1; do
    PY=$PY_FLOOR; [ "$DJANGO" = 6.1 ] && PY=$PY_NEWEST
    "$PY" -m venv "$WORK/test-$DJANGO"
    "$WORK/test-$DJANGO/bin/python" -m pip install --upgrade pip
    "$WORK/test-$DJANGO/bin/python" -m pip install . --group test "Django~=$DJANGO.0"
    "$WORK/test-$DJANGO/bin/python" -m pytest -p no:cacheprovider --spec-report="$WORK/spec-$DJANGO.json"
    "$WORK/test-$DJANGO/bin/python" scripts/spec_trace.py --report "$WORK/spec-$DJANGO.json" | tail -1
  done
  ```
  Expected: pytest reports no failures and no errors; two documentation tests are skipped, since the `docs` group is not installed. The trace ends with `0 problems`.
- [ ] Lint, formatting and types.
  ```
  "$PY_NEWEST" -m venv "$WORK/dev"
  "$WORK/dev/bin/python" -m pip install --upgrade pip
  "$WORK/dev/bin/python" -m pip install -e . --group dev "Django~=5.2.0"
  "$WORK/dev/bin/ruff" check . && "$WORK/dev/bin/ruff" format --check .
  "$WORK/dev/bin/mypy"
  ```
  Expected: `All checks passed!`, `... files already formatted`, and `Success: no issues found`.
- [ ] The documentation site builds and passes its checks as a release build, which it is now that the version has no `.dev`. This needs Python 3.13 or newer ([ADR 0024](adr/0024-user-documentation-is-built-with-sphinx-from-this-repository.md)).
  ```
  "$WORK/dev/bin/python" -m pip install -e . --group docs
  "$WORK/dev/bin/python" scripts/docs.py check
  ```
  Expected: `docs: every check passed (check)`.
- [ ] Build the sdist and the wheel with the tools the release workflow pins, check that the sdist carries every tracked file, and check their metadata.
  ```
  "$PY_FLOOR" -m venv "$WORK/build"
  "$WORK/build/bin/python" -m pip install -c .github/release-constraints.txt pip build twine
  PIP_CONSTRAINT="$PWD/.github/release-constraints.txt" \
    "$WORK/build/bin/python" -m build --outdir "$WORK/dist"
  "$PY_FLOOR" scripts/sdist_check.py "$WORK"/dist/*.tar.gz
  "$WORK/build/bin/python" -m twine check --strict "$WORK"/dist/*
  ```
  Expected: `Successfully built django_neolith-<VERSION>.tar.gz and django_neolith-<VERSION>-py3-none-any.whl`, then `sdist: django_neolith-<VERSION>.tar.gz carries the <N> tracked files`, then `PASSED` for both files. For a Markdown README, `twine check` only confirms a description is present; it does not render it.
- [ ] Render the README as PyPI will, and read it. It cannot be changed on PyPI after upload.
  ```
  "$WORK/build/bin/python" -m pip install -c .github/release-constraints.txt "readme-renderer[md]"
  "$WORK/build/bin/python" -m readme_renderer README.md -o "$WORK/readme.html"
  grep -o '<strong>Status:[^<]*' "$WORK/readme.html"
  ```
  Expected: `<strong>Status: alpha (<VERSION>). ...`. Then open `$WORK/readme.html` in a browser and read it through. Without the `[md]` extra, the renderer warns and writes no file.
- [ ] Read the wheel's metadata.
  ```
  "$PY_FLOOR" - "$WORK"/dist/*.whl "${VERSION}" <<'PY'
  import pathlib
  import sys
  import tomllib
  import zipfile
  from email.parser import Parser

  wheel = zipfile.ZipFile(sys.argv[1])
  names = wheel.namelist()
  meta = Parser().parsestr(
      wheel.read(next(n for n in names if n.endswith(".dist-info/METADATA"))).decode()
  )
  for key in ("Name", "Version", "Requires-Python", "License-Expression"):
      print(f"{key}: {meta[key]}")
  for key in ("Requires-Dist", "Classifier", "Project-URL"):
      for value in meta.get_all(key, []):
          print(f"{key}: {value}")
  print("py.typed:", "neolith/py.typed" in names)

  project = tomllib.loads(pathlib.Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
  problems = [
      f"{key}: {got!r}, expected {want!r}"
      for key, got, want in (
          ("Name", meta["Name"], project["name"]),
          ("Version", meta["Version"], sys.argv[2]),
          ("Requires-Python", meta["Requires-Python"], project["requires-python"]),
          ("License-Expression", meta["License-Expression"], project["license"]),
          ("Classifier", meta.get_all("Classifier"), project["classifiers"]),
          ("py.typed", "neolith/py.typed" in names, True),
      )
      if got != want
  ]
  print("\n".join(problems) or "metadata matches pyproject.toml")
  PY
  ```
  Expected: the metadata, then `metadata matches pyproject.toml`. Read the `Requires-Dist` lines against `dependencies` in `pyproject.toml`.
- [ ] Run the release workflow's smoke script against the wheel on both pairs. The script runs only when a tag is pushed, so CI never runs it; a change to start-up has broken it before. These commands take the script from `release.yml` itself, so it is the one the tag will run.
  ```
  awk 'index($0, "venv/bin/python - <<") {inside = 1; next} inside && $1 == "EOF" {exit} inside {print}' \
    .github/workflows/release.yml | sed 's/^ \{10\}//' > "$WORK/smoke.py"
  grep -q '^import neolith$' "$WORK/smoke.py" || echo "NO SMOKE SCRIPT FOUND in release.yml"
  for DJANGO in 5.2 6.1; do
    PY=$PY_FLOOR; [ "$DJANGO" = 6.1 ] && PY=$PY_NEWEST
    "$PY" -m venv "$WORK/smoke-$DJANGO"
    "$WORK/smoke-$DJANGO/bin/python" -m pip install "$WORK"/dist/*.whl "Django~=$DJANGO.0"
    (cd "$WORK" && TAG="v${VERSION}" "$WORK/smoke-$DJANGO/bin/python" - < "$WORK/smoke.py")
  done
  ```
  Expected: no `NO SMOKE SCRIPT` line, then `neolith <VERSION> on Django 5.2.<n>` and `neolith <VERSION> on Django 6.1.<n>`. A wrong version fails with an `AssertionError`. The script is run from `$WORK`, where there is no source tree, so it can only import the installed wheel.

## 4. The release pull request, `release/<version>` into `main`

- [ ] Write the description in `$WORK/release-pr.md` ([G4, G8](../CONTRIBUTING.md#commits-and-pull-requests)):
  - a summary of three or four bullets: the version, what it delivers, and the milestone;
  - *Changes*: the `git log` list from step 1;
  - *Decision records*: every record whose status changes, from step 1, or "None";
  - *How it was verified*: the `VERIFIED` SHA, and the results of section 3 with the Python and Django versions used.

  The merge commit takes this title and description, so they are what `main` records.
- [ ] Open it. *Recommendation:* the title `chore: release <VERSION>` ([G2](../CONTRIBUTING.md#commits-and-pull-requests)).
  ```
  gh pr create --repo "$REPO" --base main --head "$BRANCH" --title "chore: release ${VERSION}" \
    --body-file "$WORK/release-pr.md" --milestone "${VERSION}"
  ```
- [ ] The required checks pass, and it has an approving review.
  ```
  gh pr checks <number> --repo "$REPO" --required --watch
  ```
  Expected: the lint job and the four test jobs pass.
- [ ] List the other pull requests still open into the release branch, forks included, and keep the list for section 8. Deleting the branch changes them: GitHub's documentation says that a pull request from the same repository is moved to the release pull request's base, `main`, and that other pull requests open on a deleted branch are closed. In 0.1.0a3 the one from a fork (#147) was closed. Section 8 checks each one, whichever happened.
  ```
  gh pr list --repo "$REPO" --base "$BRANCH" --state open --json number,isCrossRepository,title \
    -q '.[] | "\(.number) \(if .isCrossRepository then "fork" else "same-repo" end) \(.title)"' | tee "$WORK/open-prs.txt"
  ```
  Expected: one line per pull request, or no output. The release pull request itself is not in the list, since its base is `main`.
- [ ] **(maintainer)** Run the list above again right before merging, so that a pull request merged or closed since then is not in the file. Merge it with a merge commit, only if the release branch is still the commit section 3 verified. Never squash or rebase.
  ```
  gh pr merge <number> --repo "$REPO" --merge --match-head-commit "$VERIFIED"
  ```
  Expected: the pull request is merged. If the release branch moved, the merge is refused: verify the new tip (section 3) and update the description first.
- [ ] GitHub deleted the release branch when the pull request merged. From section 5 on, the tag marks the release.
  ```
  git ls-remote origin "refs/heads/${BRANCH}"
  ```
  Expected: no output. If the branch is still there, a ruleset or the merge settings keep GitHub from deleting it (section 10): fix them, then delete it with *Delete branch* on the merged pull request. The open pull requests that targeted it are then moved to `main` or closed, after the automatic deletion or that button; section 8 checks each one and moves them to the next release branch.

## 5. Tag and publish

- [ ] Find the merge commit and check it.
  ```
  git fetch origin
  MERGE=$(gh pr view <number> --repo "$REPO" --json mergeCommit -q .mergeCommit.oid)
  git log -1 --format='%H%nparents: %P%nsubject: %s' "$MERGE"
  git merge-base --is-ancestor "$MERGE" origin/main && echo "on main" || echo "NOT ON MAIN"
  git diff --quiet "$VERIFIED" "$MERGE" && echo "same files as verified" || echo "FILES DIFFER FROM VERIFIED"
  git show "${MERGE}:src/neolith/__init__.py" | grep -qx "__version__ = \"${VERSION}\"" \
    && echo "version ${VERSION}" || echo "WRONG VERSION"
  ```
  Expected: two SHAs after `parents:`, the release pull request's title as the subject, `on main`, `same files as verified`, and `version <VERSION>`. If the files differ, do not tag: run section 3 on `$MERGE`.
- [ ] Make an annotated tag on the merge commit, and check it.
  ```
  git tag -a "v${VERSION}" -m "django-neolith ${VERSION}" "$MERGE"
  git cat-file -t "v${VERSION}"
  git rev-parse "v${VERSION}^{commit}"
  ```
  Expected: `tag`, then the merge commit's SHA.
- [ ] Push the tag by name, and nothing else.
  ```
  git push origin "refs/tags/v${VERSION}"
  ```
- [ ] Watch the release run.
  ```
  until RUN=$(gh run list --repo "$REPO" --workflow release.yml --branch "v${VERSION}" -L 1 \
      --json databaseId -q '.[0].databaseId') && [ -n "$RUN" ]; do sleep 5; done
  gh run view "$RUN" --repo "$REPO"
  ```
  The run is found by its tag, so an earlier release's run is never picked; the loop waits until GitHub has created it. Expected: the run is for `v<VERSION>`. *Build and check* and both *Smoke test* jobs pass, and *Publish to PyPI* waits for review.
- [ ] **(maintainer)** Approve the `release` deployment, only once the build and both smoke tests have passed. On the run's page, choose *Review deployments*, tick `release`, then *Approve and deploy*. Or:
  ```
  ENV_ID=$(gh api "repos/${REPO}/environments/release" -q .id)
  gh api -X POST "repos/${REPO}/actions/runs/${RUN}/pending_deployments" \
    -F "environment_ids[]=${ENV_ID}" -f state=approved -f comment="Release ${VERSION}"
  ```
- [ ] The run finishes.
  ```
  gh run watch "$RUN" --repo "$REPO" --exit-status && echo "run succeeded" || echo "RUN FAILED"
  ```
  Expected: `run succeeded`: every job succeeded, and the publish job notes that it generated and uploaded attestations.

If a job fails before *Publish to PyPI* runs, nothing was published. See [section 9](#9-when-something-goes-wrong).

## 6. After publishing, verify

- [ ] PyPI has the release, with both files.
  ```
  curl -s "https://pypi.org/pypi/django-neolith/${VERSION}/json" | python3 -c '
  import json, sys
  d = json.load(sys.stdin)
  if "info" not in d:
      sys.exit("NOT ON PYPI")
  print(d["info"]["version"], d["info"]["requires_python"], "YANKED" if d["info"]["yanked"] else "not yanked")
  kinds = sorted(f["packagetype"] for f in d["urls"])
  for f in d["urls"]:
      print(f["packagetype"], f["filename"])
  print("both files" if kinds == ["bdist_wheel", "sdist"] else f"FILES: {kinds}")'
  ```
  Expected: `<VERSION> >=3.12 not yanked`, a `bdist_wheel` and an `sdist` line, then `both files`. The page https://pypi.org/project/django-neolith/ shows the README.
- [ ] Each file has its provenance.
  ```
  for f in "django_neolith-${VERSION}-py3-none-any.whl" "django_neolith-${VERSION}.tar.gz"; do
    curl -s -o /dev/null -w "%{http_code} $f\n" -H 'Accept: application/vnd.pypi.integrity.v1+json' \
      "https://pypi.org/integrity/django-neolith/${VERSION}/${f}/provenance"
  done
  ```
  Expected: `200` for both.
- [ ] It installs from PyPI into clean virtual environments on both pairs, and the tag's smoke script and the README's tests pass against it.
  ```
  git worktree add --detach "$WORK/tag" "v${VERSION}"
  git show "v${VERSION}:.github/workflows/release.yml" \
    | awk 'index($0, "venv/bin/python - <<") {inside = 1; next} inside && $1 == "EOF" {exit} inside {print}' \
    | sed 's/^ \{10\}//' > "$WORK/smoke-pypi.py"
  for DJANGO in 5.2 6.1; do
    PY=$PY_FLOOR; [ "$DJANGO" = 6.1 ] && PY=$PY_NEWEST
    "$PY" -m venv "$WORK/pypi-$DJANGO"
    "$WORK/pypi-$DJANGO/bin/python" -m pip install --upgrade pip
    "$WORK/pypi-$DJANGO/bin/python" -m pip install --no-cache-dir "django-neolith==${VERSION}" "Django~=$DJANGO.0"
    (cd "$WORK" && TAG="v${VERSION}" "$WORK/pypi-$DJANGO/bin/python" - < "$WORK/smoke-pypi.py")
    (cd "$WORK/tag" && "$WORK/pypi-$DJANGO/bin/python" -m pip install --group test \
      && "$WORK/pypi-$DJANGO/bin/python" -m pytest -q -p no:cacheprovider tests/test_readme.py tests/test_package.py)
  done
  ```
  Expected: `neolith <VERSION> on Django ...` for each pair, then the tests pass. `pip install --group test` installs only the test tools, so the tests import the release from PyPI. A new release can take a few minutes to reach every PyPI mirror; if pip cannot find it, wait and try again.
- [ ] Download the conformance runs of the release commit: CI's run of the push to `main` writes one per leg of its matrix, four in all ([ADR 0032](adr/0032-the-conformance-suite-runs-each-case-once-per-transport.md), Decision 9). CI keeps them for 90 days, so the release keeps a copy.
  ```
  CI_RUN=$(gh run list --repo "$REPO" --workflow ci.yml --commit "$MERGE" --event push \
    --status success -L 1 --json databaseId -q '.[0].databaseId')
  gh run download "$CI_RUN" --repo "$REPO" --pattern 'conformance-run-*' --dir "$WORK/conformance"
  for f in "$WORK"/conformance/*/conformance-run-*.json; do
    python3 -c 'import json, sys; r = json.load(open(sys.argv[1])); print(sys.argv[1].rsplit("/", 1)[1], "spec", r["spec"], r["implementation"]["version"], "conforms:", r["conforms"])' "$f"
  done
  ```
  Expected: four lines, one per leg (`3.12-django5.2` and `3.15-django6.1`, each on `x86-64` and `arm64`), each with `spec 0.1`, the version `<VERSION>` and whether the run conforms. If `CI_RUN` is empty, CI has not passed on the merge commit: wait for it, or rerun it, before going on. Until roadmap step 8 the runs say `conforms: False`, since OVR-5 is not tested yet; that is expected for an alpha, and the notes say it.
- [ ] Create the GitHub Release, with the changelog section as its notes. This step is required: the release workflow does not create one, the changelog links to it, and the website's status reads GitHub Releases. Relative links in the section are made absolute at the tag.
  ```
  cd "$WORK/tag"
  python3 - "${VERSION}" > "$WORK/notes.md" <<'PY'
  import pathlib
  import re
  import sys

  version = sys.argv[1]
  text = pathlib.Path("CHANGELOG.md").read_text(encoding="utf-8")
  heading = rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}$"
  section = re.search(heading + r"\n(.*?)(?=^## |^\[)", text, re.MULTILINE | re.DOTALL)
  if section is None:
      sys.exit(f"NO SECTION [{version}] in CHANGELOG.md")
  base = f"https://github.com/stack314/django-neolith/blob/v{version}/"
  links = re.compile(r"\]\((?!https?://|#)([^)]+)\)")
  print(links.sub(lambda m: f"]({base}{m[1]})", section[1]).strip())
  PY
  python3 - "$WORK"/conformance/*/conformance-run-*.json >> "$WORK/notes.md" <<'PY'
  import json
  import pathlib
  import sys

  print("\n### Conformance runs\n")
  print("The four runs of the conformance suite on the release commit, one per CI leg, are attached (spec 06):\n")
  for name in sys.argv[1:]:
      path = pathlib.Path(name)
      run = json.loads(path.read_text(encoding="utf-8"))
      print(f"- `{path.name}`: spec {run['spec']}, conforms: {str(run['conforms']).lower()}")
  PY
  gh release create "v${VERSION}" --repo "$REPO" --verify-tag --title "${VERSION}" \
    --notes-file "$WORK/notes.md" --prerelease
  gh release upload "v${VERSION}" --repo "$REPO" "$WORK"/conformance/*/conformance-run-*.json
  gh release view "v${VERSION}" --repo "$REPO" | head -8
  gh release view "v${VERSION}" --repo "$REPO" --json assets -q '.assets[].name'
  ```
  Leave out `--prerelease` for a final release. Expected: the URL of the release, then `title: <VERSION>`, `tag: v<VERSION>`, `draft: false`, and `prerelease: true` for a pre-release, then the four `conformance-run-*.json` files. A release that does not exist prints `release not found`. If it was already created, `gh release create` refuses; check it with `gh release view`, and attach the runs with `gh release upload` if they are missing.
- [ ] The changelog's link to the release works.
  ```
  curl -s -o /dev/null -w '%{http_code}\n' "https://github.com/stack314/django-neolith/releases/tag/v${VERSION}"
  ```
  Expected: `200`.
- [ ] The README's badges show the release. They read PyPI through shields.io, and both cache for a while, so a badge can lag behind PyPI.
  ```
  for b in pypi/v/django-neolith pypi/pyversions/django-neolith pypi/frameworkversions/django/django-neolith; do
    curl -s "https://img.shields.io/$b" | grep -o '<title>[^<]*'
  done
  ```
  Expected: `pypi: v<VERSION>`, the supported Python versions and the supported Django versions. Until a final release exists, the version badge shows the newest pre-release.
- [ ] The documentation site at https://docs.stack314.com/neolith/ shows the release. It is built outside this repository, from a copy of its documents, so it changes only when that copy is refreshed.
  ```
  curl -sL https://docs.stack314.com/neolith/project/changelog/ | grep -cF "[${VERSION}]"
  ```
  Expected: a count of at least 1. If it is 0, ask for the site to be refreshed where it is built.

## 7. The landing page

The page https://stack314.com/neolith/ is updated at every release. Its source is in the website's own repository, which is not public. Changes are requested there as issues, and the website's maintainer makes them; no pull request is opened there from this project. The release is not done until the issues are filed and the live page is checked against the released version.

Go through the items below on the live page. Where a value comes "from the tag", read it with `git show "v${VERSION}:<path>"`.

- [ ] The page's description (meta and structured data) and its status word everywhere it appears (the kicker, the status sheet, the introductions) match the `Development Status` classifier at the tag, with "the API may change" for as long as the version is before 1.0.
- [ ] The status sheet's *latest release* links to `https://pypi.org/project/django-neolith/<VERSION>/`, and that URL returns 200.
- [ ] The status sheet's *python* and *django* match `requires_python` on PyPI and the `Framework :: Django :: X.Y` classifiers at the tag.
- [ ] The status sheet's *specification* matches the status line of `spec/README.md` at the tag.
- [ ] Every sentence that names a version, a mode or what is built (for example "runs apps attached", "detached is not built yet") matches the changelog section and the README's "Not yet in this version" line at the tag.
- [ ] The evidence rows:
  - [ ] *Specification*: the number of documents still outlines matches the status lines of `spec/0*.md` at the tag.
  - [ ] *Design decisions*: the counts of Accepted and Proposed records match `docs/adr/README.md` at the tag.
  - [ ] *Requirement trace*: matches the `enforce` list in `tests/spec_trace.toml` at the tag.
  - [ ] *CI matrix*: matches `.github/workflows/ci.yml` at the tag.
  - [ ] Each capability that shipped in this release moves from *Plan* to *Fact*, naming the release; the rest stay *Plan*.
- [ ] *Not solved yet*: each item is still open, per `docs/open-questions.md` and the roadmap's "Deferred past 0.1" at the tag.
- [ ] *What's next*: the current roadmap step and what is open in it match the open `roadmap` issues.
  ```
  gh issue list --repo "$REPO" --label roadmap --state open
  ```
- [ ] *Get started*: the install command is the README's install line at the tag, and resolves to the release.
  ```
  python3 -m venv "$WORK/landing" && "$WORK/landing/bin/python" -m pip install --dry-run --pre django-neolith
  ```
  Expected: `Would install ... django-neolith-<VERSION> ...`.
- [ ] *Get started*: the link to the release's README is `https://pypi.org/project/django-neolith/<VERSION>/`.
- [ ] The code example is identical to the README's example at the tag, and its label names the release. The README's example runs against the release in section 6, so an identical copy runs too.
- [ ] The notes under the example and the figures ("planned for 0.1", "only in-process is built", "the settings format is not final") are still true.
- [ ] Every external link on the page answers.
  ```
  curl -sL https://stack314.com/neolith/ | grep -oE 'href="https://[^"]+"' | sed 's/^href="//; s/"$//' | sort -u \
    | while read -r url; do echo "$(curl -s -o /dev/null -w '%{http_code}' -L "$url") $url"; done
  ```
  Expected: `200` on every line.
- [ ] The same facts elsewhere on the website (its home page, about page, FAQ and `llms.txt`) are checked at the same time.
- [ ] File the issues on the website's repository: one tracking issue for the release, and one issue for each independent change, linked from the tracking issue. Each change issue gives the exact current text, the replacement ready to paste, and how to check it.
- [ ] Once the website's maintainer closes them, go through this section again on the live page. Expected: every item matches the released version.

## 8. After the release

Run these in your own clone, not in the worktrees under `$WORK`.

- [ ] Make the next release's branch from `main`, now that the release is published and verified ([G8](../CONTRIBUTING.md#commits-and-pull-requests)). The ruleset for `release/*` covers it from the start (section 10).
  ```
  git fetch origin
  git merge-base --is-ancestor "v${VERSION}" origin/main && echo "v${VERSION} is on main" || echo "TAG NOT ON MAIN"
  gh api "repos/${REPO}/git/refs" -f ref="refs/heads/${NEXT_BRANCH}" -f sha="$(git rev-parse origin/main)" -q .ref
  git fetch origin
  [ "$(git rev-parse "origin/${NEXT_BRANCH}")" = "$(git rev-parse origin/main)" ] && echo "${NEXT_BRANCH} is at main" || echo "NOT AT MAIN"
  ```
  Expected: `v<VERSION> is on main`, `refs/heads/release/<NEXT_VERSION>`, then `release/<NEXT_VERSION> is at main`. If the branch already exists, GitHub refuses with `Reference already exists`.
- [ ] The next release has its milestone, titled with its version, so a contributor finds the branch from it.
  ```
  gh api "repos/${REPO}/milestones" -q ".[] | select(.title == \"${NEXT_VERSION}\") | .title + \" \" + .state"
  ```
  Expected: `<NEXT_VERSION> open`. If nothing is printed, create it with `gh api "repos/${REPO}/milestones" -f title="${NEXT_VERSION}"`.
- [ ] Sort each pull request in `$WORK/open-prs.txt` (section 4). GitHub's documentation says that deleting the release branch moves a pull request from the same repository to `main` and closes the others; 0.1.0a3 matched it, since the one from a fork (#147) was closed, with the events `base_ref_deleted` and `closed` in the same second. A pull request can also have been merged, or closed on purpose, after the list was made; only one that the deletion closed is recovered, which is one closed when or after the release pull request merged.
  ```
  MERGED_AT=$(gh pr view <release pull request> --repo "$REPO" --json mergedAt -q .mergedAt)
  while read -r NUMBER _; do
    gh api "repos/${REPO}/pulls/${NUMBER}" | jq -r --arg merged "$MERGED_AT" --arg base "$BRANCH" '
      "#\(.number) fork=\(.head.repo.fork) " + (
        if .merged then "leave: merged"
        elif .state == "open" then (if .base.ref == "main" then "move" else "leave: open on \(.base.ref)" end)
        elif .base.ref == $base and .closed_at >= $merged then "recover"
        else "leave: closed before the release" end)
    '
  done < "$WORK/open-prs.txt"
  ```
  Expected: one line for each, ending in `move`, `recover` or `leave: ...`. Do nothing to a `leave`. The steps below handle the other two. (The timestamps are in one format, so the comparison of text is a comparison of time.)
- [ ] Move each pull request marked `move` to the new branch, which is `$NEXT_BRANCH` (after a hotfix, `$OPEN_BRANCH`, section 9). Use the API: `gh pr edit` fails on this repository.
  ```
  gh api -X PATCH "repos/${REPO}/pulls/<number>" -f base="$NEXT_BRANCH" -q .base.ref
  ```
  Expected: `release/<NEXT_VERSION>`. Then run the CI step below for it.
- [ ] Recover each pull request marked `recover` (#147 in 0.1.0a3). GitHub reopens a pull request only while its base branch exists, so make the old branch again at the release pull request's head, which is the tip it had:
  ```
  gh api "repos/${REPO}/git/refs" -f ref="refs/heads/${BRANCH}" \
    -f sha="$(gh pr view <release pull request> --repo "$REPO" --json headRefOid -q .headRefOid)" -q .ref
  gh api -X PATCH "repos/${REPO}/pulls/<number>" -f state=open -q .state
  gh api -X PATCH "repos/${REPO}/pulls/<number>" -f base="$NEXT_BRANCH" -q .base.ref
  ```
  Expected: `refs/heads/release/<VERSION>`, `open`, then `release/<NEXT_VERSION>`. Make the branch once for all of them. Its creation starts a CI run on an old commit, which can be ignored.
- [ ] Run CI again for each pull request moved or recovered. After a retarget, the checks it earlier passed do not count for the new base: GitHub lists the required checks as "expected" (seen on #147 on 2026-11-21). Closing and reopening starts a new run, as a push would. A run of a fork's pull request that is still waiting for approval ends when the pull request is closed (the run from #147's first reopen did, with no jobs), which is why the next step approves the new run.
  ```
  gh api -X PATCH "repos/${REPO}/pulls/<number>" -f state=closed -q .state
  gh api -X PATCH "repos/${REPO}/pulls/<number>" -f state=open -q .state
  gh run list --repo "$REPO" --workflow ci.yml --event pull_request --limit 3 --json databaseId,status,conclusion,createdAt
  ```
  Expected: `closed`, `open`, then a run created just now, `queued` or `in_progress`.
- [ ] Approve the run if it waits. A run for a pull request from a fork can wait for a maintainer's approval before it starts: GitHub asks for this from first-time contributors, and the run reports `action_required` with no jobs. Read the fork's changes first, since the run executes its code.
  ```
  gh run list --repo "$REPO" --workflow ci.yml --status action_required --json databaseId,headSha,createdAt
  gh api -X POST "repos/${REPO}/actions/runs/<run id>/approve"
  gh pr checks <number> --repo "$REPO" --required --watch
  ```
  Expected: the run's `headSha` is the pull request's head; the approval prints nothing (HTTP 201); then the lint job and the four test jobs pass. No output from the first command means the run did not need approval.
- [ ] Leave a comment for the contributor of each pull request moved or recovered, so a closed or retargeted pull request is not read as a decision about the change. For a recovered one:
  ```
  gh api "repos/${REPO}/issues/<number>/comments" -f body="GitHub closed this pull request when release/${VERSION} was deleted after the release; that was not a decision about the change. It is reopened against ${NEXT_BRANCH}, and CI has run again. Nothing is needed from you." -q .html_url
  ```
  For a moved one, which was never closed by GitHub, use this body instead: `The release branch was deleted after the release, so this pull request now targets ${NEXT_BRANCH}, and CI has run again. Nothing is needed from you.` Expected: the comment's URL.
- [ ] Once every recovered pull request has its new run, delete the old release branch again.
  ```
  gh api -X DELETE "repos/${REPO}/git/refs/heads/${BRANCH}"
  git ls-remote origin "refs/heads/${BRANCH}"
  ```
  Expected: no output from either. Skip this if no pull request was recovered.
- [ ] Start the next development version with a pull request into the new branch ([G8](../CONTRIBUTING.md#commits-and-pull-requests)). It also points the changelog's `[Unreleased]` link at the new branch, where unreleased work now is.
  ```
  git fetch origin
  git switch -c "chore/start-${NEXT_DEV}" "origin/${NEXT_BRANCH}"
  sed -i.bak "s/^__version__ = .*/__version__ = \"${NEXT_DEV}\"/" src/neolith/__init__.py && rm src/neolith/__init__.py.bak
  grep -qx "__version__ = \"${NEXT_DEV}\"" src/neolith/__init__.py && echo "version ${NEXT_DEV}" || echo "WRONG VERSION"
  LINK="[Unreleased]: https://github.com/stack314/django-neolith/compare/v${VERSION}...${NEXT_BRANCH}"
  sed -i.bak "s|^\[Unreleased\]: .*|${LINK}|" CHANGELOG.md && rm CHANGELOG.md.bak
  grep -qxF "$LINK" CHANGELOG.md && echo "link to ${NEXT_BRANCH}" || echo "WRONG UNRELEASED LINK"
  awk '/^## \[Unreleased\]/ {found = 1; next} found && /^## \[/ {exit} found && NF {print "UNRELEASED NOT EMPTY: " $0; bad = 1; exit}
    END {if (!found) print "NO UNRELEASED SECTION"; else if (!bad) print "Unreleased is empty"}' CHANGELOG.md
  ```
  Expected: `version <NEXT_DEV>`, `link to release/<NEXT_VERSION>`, then `Unreleased is empty`. If there is no `## [Unreleased]` section, add an empty one at the top of the changelog. Open the pull request into `$NEXT_BRANCH`, in the next release's milestone. *Recommendation:* the title `chore: start <NEXT_DEV> after releasing <VERSION>`. Until this merges, the documentation build treats the new branch as a release (`docs/site/conf.py`).
- [ ] Close the issues the release carries. A `Closes #N` line does not close an issue when its pull request merges into a release branch ([G1](../CONTRIBUTING.md#commits-and-pull-requests)), so list them from the release's commits and close any still open.
  ```
  for n in $(git log --format=%B "v${PREVIOUS}..v${VERSION}" \
      | grep -oiE '\b(close[sd]?|fix(e[sd])?|resolve[sd]?) #[0-9]+' | grep -oE '[0-9]+' | sort -un); do
    gh issue view "$n" --repo "$REPO" --json number,state,title -q '"\(.number) \(.state) \(.title)"'
  done
  gh issue close <number> --repo "$REPO" --comment "Released in ${VERSION}."
  ```
  Expected: after closing, every listed issue is `CLOSED`.
- [ ] Close the release's milestone, after the checks in sections 6 and 7 pass. Open issues left in it move to the next milestone first, each with a one-line comment saying why.
  ```
  gh issue list --repo "$REPO" --milestone "${VERSION}" --state open
  MILESTONE=$(gh api "repos/${REPO}/milestones" -q ".[] | select(.title == \"${VERSION}\") | .number")
  echo "milestone ${MILESTONE:-NOT FOUND}"
  gh api -X PATCH "repos/${REPO}/milestones/${MILESTONE}" -f state=closed -q '.title + " " + .state'
  ```
  Expected: no open issues, `milestone <n>`, then `<VERSION> closed`.
- [ ] The next release's milestone is now the current one: the `.dev` pull request and every later pull request into `$NEXT_BRANCH` go in it.
- [ ] The roadmap issue this release finishes is closed, or its open parts are moved to the next milestone, and its task list is ticked. The project board shows the release's issues and pull requests as done.
- [ ] `docs/roadmap.md` still describes what is left. It changes only when a step's scope changes.
- [ ] Announcements: the repository names no place where releases are announced. If one is added, list it here.
- [ ] Remove the scratch worktrees: `git worktree remove --force "$WORK/src"; git worktree remove --force "$WORK/tag"`.

## 9. When something goes wrong

**Never delete a release from PyPI, and never reuse a version number.** PyPI refuses a file name that was ever uploaded, even after deletion.

**The build or a smoke test fails, before publishing.** Nothing reached PyPI. If the run is still waiting for review, reject the deployment (*Review deployments*, then *Reject*), or cancel the run with `gh run cancel "$RUN" --repo "$REPO"`. GitHub deleted the release branch when the release pull request merged, so make it again from `main`, which now holds the version: `gh api "repos/${REPO}/git/refs" -f ref="refs/heads/${BRANCH}" -f sha="$(git rev-parse origin/main)"`. Fix the cause by a pull request into it, then run sections 3 and 4 again. *Recommendation:* since the version never reached PyPI, keep it, and move the tag to the new merge commit once that merge is verified:
```
git push origin --delete "refs/tags/v${VERSION}"
git tag -d "v${VERSION}"
```
Then repeat section 5. Do not open the `.dev` pull request until the release is published.

**The publish job fails.** Read its log with `gh run view "$RUN" --repo "$REPO" --log-failed`, and check PyPI (section 6). If no file was uploaded, fix the cause (for example the trusted publisher's settings, section 10), then run the job again with `gh run rerun "$RUN" --repo "$REPO" --failed`. If one file was uploaded and the other was not, a rerun fails on the file that exists, because the workflow does not skip existing files. *Recommendation:* yank that version and release the next one.

**A bad release reached PyPI.** Yank it; do not delete it. On https://pypi.org/manage/project/django-neolith/releases/, open the version's *Options*, choose *Yank*, and give the reason. pip then skips it unless it is pinned exactly. Release a fixed version through the whole runbook. *Recommendation:* mark the changelog heading `## [<VERSION>] - <date> [YANKED]` in the fixed release's version pull request, as Keep a Changelog suggests, and add the reason to the top of the GitHub Release's notes.

**The tag is on the wrong commit.** If the release workflow refused it ("is not on main" or "does not match `__version__`"), nothing was published: delete the tag as above and tag the right commit. If the wrong commit was published, the tag stays where it is, since it records what was published; treat it as a bad release.

**A fix must ship while the open release branch holds work that is not ready.** Make a hotfix: a release of its own, on a second release branch made from `main` ([G8](../CONTRIBUTING.md#commits-and-pull-requests)). *Recommendation:* if the open release branch can be released as it stands, release it instead, since each pull request leaves it working.

1. Set the variables, and check that the hotfix's version sorts after the last release and before the open release's version. In the alpha series, only a post-release sorts in between, such as `0.1.0a2.post1` while `release/0.1.0a3` is open; after a final release, it is the next patch version, such as `0.1.1` while `release/0.2.0` is open. Make a milestone titled with the hotfix's version.
   ```
   VERSION=0.1.0a2.post1                  # the hotfix's version
   BRANCH="release/${VERSION}"            # the hotfix's branch
   PREVIOUS=0.1.0a2                       # the last published version
   OPEN_VERSION=0.1.0a3                   # the version of the open release branch
   OPEN_BRANCH="release/${OPEN_VERSION}"
   python - "${PREVIOUS}" "${VERSION}" "${OPEN_VERSION}" <<'PY'
   import sys
   from packaging.version import Version

   previous, version, open_version = (Version(v) for v in sys.argv[1:])
   assert previous < version < open_version, f"{version} does not sort between {previous} and {open_version}"
   print(version, "sorts between", previous, "and", open_version)
   PY
   ```
   Expected: `0.1.0a2.post1 sorts between 0.1.0a2 and 0.1.0a3`. Section 2 checks the rest of the version's form.
2. Make the hotfix branch from `main`, as section 8 makes a release branch:
   ```
   gh api "repos/${REPO}/git/refs" -f ref="refs/heads/${BRANCH}" -f sha="$(git rev-parse origin/main)" -q .ref
   ```
3. The fix is a pull request into `$BRANCH`, squash-merged: one commit on the hotfix branch. If the fix is already on the open release branch, `git cherry-pick -x` its commit onto a branch made from `$BRANCH`.
4. Sections 1 to 7 as for any release: the version pull request adds one commit to `$BRANCH`; the release pull request adds a merge commit to `main`, and GitHub deletes `$BRANCH`; the tag goes on that merge commit, and the GitHub Release follows. In section 8, skip every step about the next release: the open release branch already exists and keeps its `.dev` version. The pull requests that were open into the hotfix branch are still sorted, moved or recovered, with `NEXT_BRANCH="$OPEN_BRANCH"` set first, so that they land on the open release branch.
5. Bring `main` into the open release branch, so the next release carries the fix and its release pull request merges cleanly. This pull request is merged with a merge commit ([G5](../CONTRIBUTING.md#commits-and-pull-requests)). A squash would copy the changes without `main`'s history, and the next release pull request would then conflict on the version and the changelog.
   ```
   git fetch origin
   git switch -c "chore/merge-${VERSION}" "origin/${OPEN_BRANCH}"
   git merge --no-ff origin/main -m "chore: merge main after releasing ${VERSION}"
   ```
   Expected: conflicts in `src/neolith/__init__.py` and `CHANGELOG.md`. Keep the open branch's `.dev` version. In the changelog, keep `## [Unreleased]` with its entries, put the hotfix's section under it, and keep the hotfix's link. Point the `[Unreleased]` link at the open branch: `https://github.com/stack314/django-neolith/compare/v<VERSION>...<OPEN_BRANCH>`. Then:
   ```
   grep -nE '^(<<<<<<<|=======|>>>>>>>)' src/neolith/__init__.py CHANGELOG.md || echo "no conflict markers"
   git add src/neolith/__init__.py CHANGELOG.md && git commit --no-edit
   git merge-base --is-ancestor origin/main HEAD && echo "main is in this branch" || echo "MAIN NOT IN THIS BRANCH"
   ```
   Expected: `no conflict markers`, the commit, then `main is in this branch`. Push it and open the pull request into `$OPEN_BRANCH`. Once it is reviewed and CI passes, the maintainer merges it with `gh pr merge <number> --repo "$REPO" --merge`.
6. Check that the open release branch now holds `main`:
   ```
   git fetch origin
   git merge-base --is-ancestor origin/main "origin/${OPEN_BRANCH}" && echo "main is in ${OPEN_BRANCH}" || echo "MAIN NOT IN ${OPEN_BRANCH}"
   ```
   Expected: `main is in <OPEN_BRANCH>`. Section 1 checks this again before the next release.

## 10. One-time setup (already in place)

For reference, and for checking when something in section 5 fails.

- **PyPI trusted publisher** for the project `django-neolith`: owner `stack314`, repository `django-neolith`, workflow `release.yml`, environment `release`. No API token is stored anywhere.
- **The `release` environment** (Settings, Environments): the maintainer is a required reviewer, and only tags matching `v*` may deploy to it.
- **Branch rulesets** (Settings, Rules):
  - `release/*`, which covers each release branch from the moment it is made: no force push; changes by pull request only, one approving review, stale approvals dismissed on push; the lint job and the four test jobs required. Squash merges and merge commits are allowed: every pull request is squash-merged except the one that brings `main` in after a hotfix (section 9). Deletion is not restricted, so that GitHub can delete a release branch when its release pull request merges; a rule or ruleset that restricts it keeps GitHub from doing so. The cost: anyone with write access can delete the open release branch, and GitHub then closes or moves every open pull request into it (section 8).
  - `main` (the default branch): no deletion or force push; changes by pull request only, merge commits only, one approving review, stale approvals dismissed on push; the same five jobs required. A second ruleset for `main`, which would let only admins update it, exists and is disabled.
- **Merge settings** (Settings, General): squash merging and merge commits allowed, rebase merging off. Both take the pull request's title and description as the commit's message. Head branches are deleted after merge: a release branch goes when its release pull request merges, and the open pull requests that targeted it are moved to `main` or closed (section 8).
- **Release branches**: nothing is set per branch. Section 8 makes each release's branch from `main` once the previous release is published, and section 9 makes a hotfix's.
- **CI** (`.github/workflows/ci.yml`) runs on every pull request and on every push to `main` and to a release branch (`release/**`).
