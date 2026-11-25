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
import re
import shutil
import subprocess
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


def test_g7_holds_merges_until_ci_passed_on_the_tip() -> None:
    rule = _rule("G7")
    assert "current tip" in rule
    assert "While that tip fails" in rule
    assert "Closing and reopening" in rule
    assert "keeps the approval" in rule
    assert "re-run" in rule
    # The known gap is written down, not hidden.
    assert "can still turn the tip red once" in rule


def test_every_path_g7_names_as_a_checker_exists() -> None:
    listed = re.search(r"a checker \(([^)]*)\)", _rule("G7"))
    assert listed, "G7 does not list what counts as CI or a checker"
    paths = re.findall(r"`([^`]+)`", listed.group(1))
    assert paths
    for path in paths:
        assert (ROOT / path).exists(), f"G7 names {path}, which does not exist"


def test_every_file_ci_runs_is_a_checker_g7_names() -> None:
    paths = re.findall(r"`([^`]+)`", _rule("G7"))
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    run = sorted(set(re.findall(r"\b(scripts/[\w./-]+\.py)", workflow)))
    assert run, "ci.yml runs no script under scripts/"
    for script in [*run, "tests/conftest.py", "tests/spec_trace.toml"]:
        assert any(script.startswith(p) for p in paths), f"G7 misses {script}"


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
