"""The runbook's steps for pull requests left open when a release branch goes.

``docs/releasing.md`` section 8 sorts each pull request that was open into the
release branch after GitHub deleted it: moved, recovered, or left alone. The
sorting is a ``jq`` program in the runbook, run here on the shapes of
pull request the REST API returns, so that a merged pull request or one closed
on purpose is never reopened. The text around it is checked for the claims a
reader acts on.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

RUNBOOK = Path(__file__).resolve().parents[1] / "docs" / "releasing.md"
BRANCH = "release/0.1.0a4"
MERGED_AT = "2026-11-21T11:00:00Z"


def _section(number: int) -> str:
    text = RUNBOOK.read_text(encoding="utf-8")
    match = re.search(rf"^## {number}\. .*?(?=^## \d+\. |\Z)", text, re.M | re.S)
    assert match, f"no section {number}"
    return match.group(0)


def _sorter() -> str:
    found = re.search(r"jq -r --arg merged [^\n]*?'\n(.*?)\n\s*'", _section(8), re.S)
    assert found, "section 8 has no jq program that sorts the pull requests"
    return found.group(1)


def _pull(**fields: object) -> dict[str, object]:
    pull: dict[str, object] = {
        "number": 147,
        "state": "closed",
        "merged": False,
        "closed_at": MERGED_AT,
        "base": {"ref": BRANCH},
        "head": {"repo": {"fork": True}},
    }
    return pull | fields


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not installed")
@pytest.mark.parametrize(
    ("pull", "verdict"),
    [
        pytest.param(_pull(), "recover", id="closed-by-the-deletion"),
        pytest.param(
            _pull(closed_at="2026-11-21T11:00:01Z"), "recover", id="closed-after"
        ),
        pytest.param(
            _pull(closed_at="2026-11-20T09:00:00Z"), "leave", id="closed-on-purpose"
        ),
        pytest.param(_pull(merged=True), "leave", id="merged-before-the-release"),
        pytest.param(
            _pull(state="open", base={"ref": "main"}, closed_at=None),
            "move",
            id="open-on-main",
        ),
        pytest.param(
            _pull(state="open", base={"ref": BRANCH}, closed_at=None),
            "leave",
            id="still-open-on-the-old-branch",
        ),
    ],
)
def test_the_sorter_recovers_only_what_the_deletion_closed(
    pull: dict[str, object], verdict: str
) -> None:
    jq = shutil.which("jq")
    assert jq
    done = subprocess.run(  # noqa: S603
        [jq, "-r", "--arg", "merged", MERGED_AT, "--arg", "base", BRANCH, _sorter()],
        input=json.dumps(pull),
        capture_output=True,
        text=True,
        check=True,
    )
    assert f" {verdict}" in done.stdout.split("fork=")[1], done.stdout


def test_the_runbook_cites_what_github_documents() -> None:
    for number in (4, 8):
        assert "documentation does not say" not in _section(number)
    assert "same repository" in _section(8)


def test_the_list_is_made_again_right_before_the_merge() -> None:
    merge = re.search(r"- \[ \] \*\*\(maintainer\)\*\* Run the list[^\n]*", _section(4))
    assert merge
    assert "again right before merging" in merge.group(0)


def test_the_reason_for_running_ci_again_is_not_the_old_base() -> None:
    assert "while the old base was still set" not in _section(8)
    assert "waiting for approval" in _section(8)


def test_the_contributor_comment_is_for_recovered_pull_requests_only() -> None:
    text = _section(8)
    comment = re.search(r"- \[ \] [^\n]*comment[^\n]*", text)
    assert comment
    assert "recovered" in comment.group(0)
    assert "moved" in text[comment.end() :].split("- [ ]")[0]


def test_a_hotfix_aims_the_moves_at_the_open_release_branch() -> None:
    step = re.search(r"^4\. .*?(?=^5\. )", _section(9), re.M | re.S)
    assert step
    assert 'NEXT_BRANCH="$OPEN_BRANCH"' in step.group(0)
