"""The runbook's steps for pull requests left open when a release branch goes.

``docs/releasing.md`` section 8 sorts each pull request that was open into the
release branch after GitHub deleted it: moved, recovered, or left alone. The
sorting is a ``jq`` program in the runbook, run here on the shapes of
pull request the REST API returns, so that a merged pull request or one closed
on purpose is never reopened. The text around it is checked for the claims a
reader acts on, and CONTRIBUTING G7's rule for merging into a release branch
is checked against the runbook steps that follow it.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "releasing.md"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
BRANCH = "release/0.1.0a4"
DELETED = ["base_ref_deleted", "closed"]
# The events of #147 (0.1.0a3), oldest first, up to its first reopen.
PR_147 = ["review_requested", "milestoned", "base_ref_deleted", "closed"]


def _section(number: int) -> str:
    text = RUNBOOK.read_text(encoding="utf-8")
    match = re.search(rf"^## {number}\. .*?(?=^## \d+\. |\Z)", text, re.M | re.S)
    assert match, f"no section {number}"
    return match.group(0)


def _sorter() -> str:
    found = re.search(r"jq -r --arg last [^\n]*?'\n(.*?)\n\s*'", _section(8), re.S)
    assert found, "section 8 has no jq program that sorts the pull requests"
    return found.group(1)


def _last_events() -> tuple[str, str]:
    found = re.search(
        r"LAST=\$\(gh api --paginate \"[^\"]+/events\" -q '([^']+)' \| ([^\n]+)\)$",
        _section(8),
        re.M,
    )
    assert found, "section 8 does not read each pull request's last events"
    return found.group(1), found.group(2)


def _pull(**fields: object) -> dict[str, object]:
    pull: dict[str, object] = {
        "number": 147,
        "state": "closed",
        "merged": False,
        "base": {"ref": BRANCH},
        "head": {"repo": {"fork": True}},
    }
    return pull | fields


def _verdict(pull: dict[str, object], events: list[str]) -> str:
    jq = shutil.which("jq")
    assert jq
    program, rest = _last_events()
    last = subprocess.run(  # noqa: S603
        ["sh", "-c", f'"$0" -r "$1" | {rest}', jq, program],  # noqa: S607
        input=json.dumps([{"event": event} for event in events]),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    done = subprocess.run(  # noqa: S603
        [jq, "-r", "--arg", "last", last, "--arg", "base", BRANCH, _sorter()],
        input=json.dumps(pull),
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.split("fork=")[1].split(" ", 1)[1].strip()


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not installed")
@pytest.mark.parametrize(
    ("pull", "events", "verdict"),
    [
        pytest.param(_pull(), PR_147, "recover", id="147-closed-by-the-deletion"),
        pytest.param(_pull(), DELETED, "recover", id="closed-by-the-deletion"),
        pytest.param(
            _pull(), ["closed"], "leave: closed on purpose", id="closed-on-purpose"
        ),
        pytest.param(
            _pull(),
            ["closed", "reopened", "closed"],
            "leave: closed on purpose",
            id="closed-again-on-purpose",
        ),
        pytest.param(
            _pull(),
            [*DELETED, "reopened", "closed"],
            "leave: closed on purpose",
            id="recovered-before-then-closed-on-purpose",
        ),
        pytest.param(
            _pull(base={"ref": "main"}),
            DELETED,
            "leave: closed on purpose",
            id="closed-by-the-deletion-of-another-base",
        ),
        pytest.param(
            _pull(base={"ref": "main"}),
            ["closed"],
            "leave: closed on purpose",
            id="closed-on-purpose-on-another-base",
        ),
        pytest.param(_pull(), [], "leave: closed on purpose", id="no-events"),
        pytest.param(
            _pull(merged=True),
            [*DELETED, "reopened", "closed"],
            "leave: merged",
            id="merged",
        ),
        pytest.param(
            _pull(state="open", base={"ref": "main"}), [], "move", id="open-on-main"
        ),
        pytest.param(
            _pull(state="open"),
            [],
            f"leave: open on {BRANCH}",
            id="still-open-on-the-old-branch",
        ),
    ],
)
def test_the_sorter_recovers_only_what_the_deletion_closed(
    pull: dict[str, object], events: list[str], verdict: str
) -> None:
    assert _verdict(pull, events) == verdict


def test_the_sorter_does_not_go_by_the_clock() -> None:
    assert "MERGED_AT" not in _section(8)
    assert "closed_at" not in _sorter()


def test_the_runbook_cites_what_github_documents() -> None:
    for number in (4, 8):
        assert "documentation does not say" not in _section(number)
    # The documentation's own words, quoted once in section 4: the move is for
    # pull requests "in the same repository", after a merged head is deleted.
    assert '"in the same repository"' in _section(4)
    assert "closes the open pull requests associated with it" in _section(4)
    assert "(section 4)" in _section(8)


def test_the_list_is_made_again_right_before_the_merge() -> None:
    merge = re.search(r"- \[ \] \*\*\(maintainer\)\*\* Run the list[^\n]*", _section(4))
    assert merge
    assert "again right before merging" in merge.group(0)


def test_the_reason_for_running_ci_again_is_not_the_old_base() -> None:
    assert "while the old base was still set" not in _section(8)
    assert "waiting for approval" in _section(8)


def test_the_contributor_comment_covers_moved_and_recovered() -> None:
    text = _section(8)
    comment = re.search(r"- \[ \] [^\n]*comment[^\n]*", text)
    assert comment
    assert "recovered" in comment.group(0)
    assert "moved" in text[comment.end() :].split("- [ ]")[0]


def test_a_hotfix_aims_the_moves_at_the_open_release_branch() -> None:
    step = re.search(r"^4\. .*?(?=^5\. )", _section(9), re.M | re.S)
    assert step
    assert 'NEXT_BRANCH="$OPEN_BRANCH"' in step.group(0)


def test_deleting_the_open_branch_closes_its_pull_requests() -> None:
    text = _section(10)
    assert "closes or moves" not in text
    assert "closes every open pull request into it instead of moving them" in text


def _rule(name: str) -> str:
    text = CONTRIBUTING.read_text(encoding="utf-8")
    found = re.search(rf"^- \*\*{name}\.\*\* [^\n]*", text, re.M)
    assert found, f"CONTRIBUTING has no rule {name}"
    return found.group(0)


@pytest.mark.parametrize(
    "clause",
    [
        "once CI's run on the branch's current tip has passed",
        "While that tip fails, only a pull request whose checks passed on that tip"
        " merges: the fix",
        "needs passing checks from a run made after the current tip landed",
        "Closing and reopening it starts that run and keeps the approval",
        "a re-run does not count, since it tests the old base",
        # The known gap is written down, not hidden.
        "can still turn the tip red once; this rule then holds the next merge",
        # Any pull request checked on an older tip, not only one that predates
        # a new check: a change on the base can break it without touching it.
        "whose checks ran on an older tip meets every change since then",
    ],
)
def test_g7_holds_merges_until_ci_passed_on_the_tip(clause: str) -> None:
    # Whole clauses, so that reversing one ("a re-run also counts") fails.
    assert clause in _rule("G7")


def test_every_path_g7_names_as_a_checker_exists() -> None:
    listed = re.search(r"a checker \(([^)]*)\)", _rule("G7"))
    assert listed, "G7 does not list what counts as CI or a checker"
    paths = re.findall(r"`([^`]+)`", listed.group(1))
    assert paths
    for path in paths:
        assert (ROOT / path).exists(), f"G7 names {path}, which does not exist"


# Boots Django on the test settings and resolves the URLconf, as the first
# request test does, imports the conftests named in argv, and prints every
# non-empty module under tests/ that this loaded. "Loads" is transitive, so the
# runtime is asked rather than predicted from file names.
_LOADS = """
import importlib, os, sys
import django
django.setup()
from django.urls import get_resolver
get_resolver().url_patterns
for name in sys.argv[1:]:
    importlib.import_module(name)
tests = os.path.join(os.getcwd(), "tests") + os.sep
for module in list(sys.modules.values()):
    path = os.path.abspath(getattr(module, "__file__", None) or "")
    if path.startswith(tests) and os.path.getsize(path):
        print(os.path.relpath(path, os.getcwd()))
"""


def _checked_by_ci() -> list[str]:
    """Every file outside the package that decides whether CI passes.

    Found from what CI and its tools load without being asked, and what those
    load in turn, not listed by hand, so that a module added later is found too.
    """
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    run = sorted(set(re.findall(r"\b(scripts/[\w./-]+\.py)", workflow)))
    assert run, "ci.yml runs no script under scripts/"
    # pytest loads every conftest under the test paths, and pytest-django the
    # settings module pyproject.toml names, for every test.
    conftests = sorted(
        p.relative_to(ROOT).as_posix() for p in ROOT.glob("tests/**/conftest.py")
    )
    settings = re.search(
        r'^DJANGO_SETTINGS_MODULE = "([\w.]+)"',
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
        re.M,
    )
    assert settings, "pyproject.toml names no settings module for pytest"
    module = settings.group(1).replace(".", "/") + ".py"
    loaded = subprocess.run(  # noqa: S603 (our own code, fixed arguments)
        [sys.executable, "-c", _LOADS, *(c[:-3].replace("/", ".") for c in conftests)],
        cwd=ROOT,
        env={**os.environ, "DJANGO_SETTINGS_MODULE": settings.group(1)},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    files = [*run, *conftests, module, *loaded, "tests/spec_trace.toml"]
    if "scripts/docs.py" in run:
        # Sphinx runs the site's configuration and extensions as code, renders
        # every page through its templates, and fails on any warning.
        site = ROOT / "docs" / "site"
        code = [
            site / "conf.py",
            *sorted((site / "_ext").glob("*.py")),
            *sorted((site / "_templates").glob("*")),
        ]
        files += [path.relative_to(ROOT).as_posix() for path in code]
    return files


def test_every_file_ci_runs_is_a_checker_g7_names() -> None:
    paths = re.findall(r"`([^`]+)`", _rule("G7"))
    for checked in _checked_by_ci():
        assert any(checked.startswith(p) for p in paths), f"G7 misses {checked}"


_READS_THE_REPOSITORY = re.compile(
    r"""["'](?:docs|spec|README\.md|CONTRIBUTING\.md|CHANGELOG\.md|\.github)\b"""
)


def test_g7_counts_tests_of_the_repository_files_as_checkers() -> None:
    # A test that reads the repository's documents is a check on them: one
    # added in a pull request can fail on a base that moved, as #113 did.
    rule = _rule("G7")
    assert "a test that checks the repository's own files" in rule
    named = re.findall(r"`(tests/test_\w+\.py)`", rule)
    assert named, "G7 gives no example of such a test"
    for example in named:
        text = (ROOT / example).read_text(encoding="utf-8")
        assert _READS_THE_REPOSITORY.search(text), f"{example} reads no document"


def test_section_8_retargets_before_it_closes_and_reopens() -> None:
    text = _section(8)
    retargets = [m.start() for m in re.finditer(r'-f base="\$NEXT_BRANCH"', text)]
    closes = [m.start() for m in re.finditer(r"-f state=closed", text)]
    assert retargets
    assert closes
    assert max(retargets) < min(closes), (
        "a run started before the retarget tests the old base"
    )
    step = re.search(r"- \[ \] Run CI again[^\n]*", text)
    assert step
    assert "retarget first, then close and reopen" in step.group(0)
    assert "a re-run of an old run would test the old base" in step.group(0)


def test_a_run_waiting_for_approval_is_described_by_its_conclusion() -> None:
    # GitHub reports such a run as status `completed`, conclusion
    # `action_required`: a reader who sees `completed` must not take it as done.
    text = _section(8)
    assert "status `completed` with conclusion `action_required`" in text
    for sentence in re.findall(r"[^.\n]*`action_required`[^.\n]*", text):
        assert "conclusion `action_required`" in sentence or "--status" in sentence, (
            sentence
        )


def test_the_contributor_comments_explain_the_reopen() -> None:
    text = _section(8)
    step = text[text.index("Leave a comment for the contributor") :].split("\n- [ ]")[0]
    bodies = re.findall(r'body="([^"]+)"|instead: `([^`]+)`', step)
    assert len(bodies) == 2
    for body in ("".join(pair) for pair in bodies):
        assert "closed and reopened" in body
        assert "only to run CI" in body


def test_setup_says_why_there_is_no_queue_and_no_up_to_date_rule() -> None:
    text = _section(10)
    line = re.search(r"- \*\*No merge queue[^\n]*", text)
    assert line, "section 10 does not say why there is no merge queue"
    for words in ("up to date", "G7", "G5", "merge method", "dismissed on push"):
        assert words in line.group(0)


def test_setup_says_how_to_check_g7() -> None:
    # G7 rests on two facts: the tip's run passed, and a pull request's run
    # came after the tip landed. Each is read with one command.
    line = re.search(r"- \*\*Checking G7\*\*[^\n]*", _section(10))
    assert line, "section 10 does not say how to check G7"
    text = line.group(0)
    for words in (
        "--event push --branch release/<VERSION> -L 1",
        "--json headSha,conclusion,createdAt",
        "`headSha` equal to the branch tip and conclusion `success`",
        "--event pull_request --branch <head branch>",
        "a later `createdAt` than that tip run",
    ):
        assert words in text, words


def test_the_landing_page_step_names_the_readme_example_markers() -> None:
    step = _section(7)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    start, end = "<!-- neolith-example:start -->", "<!-- neolith-example:end -->"
    assert readme.count(start) == readme.count(end) == 1
    assert start in step
    assert end in step
    found = re.search(r"git show \"v\$\{VERSION\}:README.md\" \| ([^\n]+)", step)
    assert found, "section 7 has no command that prints the marked example"
    pipeline = found.group(1)
    assert f"/^{start}$/,/^{end}$/p" in pipeline
    # Run the runbook's own pipeline (everything after `git show ... |`), so a
    # command that leaves the marker lines in the output fails here.
    shown = subprocess.run(  # noqa: S603
        ["sh", "-c", pipeline],  # noqa: S607
        input=readme,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    inner = readme.split(start + "\n", 1)[1].split(end, 1)[0]
    assert shown == inner
    assert start not in shown
    assert end not in shown
