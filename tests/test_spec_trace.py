"""The requirement-ID and spec-to-test trace check (ADR 0018)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from scripts import spec_trace

README = """\
# Spec

| # | Document | Defines | In 0.1 | IDs |
|---|---|---|---|---|
| 01 | [Contracts](01-contracts.md) | Contracts | yes | `CON` |
| 02 | [Transports](02-transports.md) | Transports | yes | `TRN` |
"""

Repo = Callable[..., Path]


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    """Write a small spec/ and tests/ tree and return its root."""

    def make(spec: str, tests: str = "", transports: str = "") -> Path:
        (tmp_path / "spec").mkdir(exist_ok=True)
        (tmp_path / "tests").mkdir(exist_ok=True)
        (tmp_path / "spec" / "README.md").write_text(README)
        (tmp_path / "spec" / "01-contracts.md").write_text(spec)
        (tmp_path / "spec" / "02-transports.md").write_text(transports)
        (tmp_path / "tests" / "test_example.py").write_text(tests)
        return tmp_path

    return make


def problems(root: Path, enforce: tuple[str, ...] = ()) -> list[str]:
    return spec_trace.check(root, enforce).problems


def test_repository_spec_and_citations_are_consistent() -> None:
    trace = spec_trace.check(spec_trace.ROOT)
    assert trace.problems == []
    assert trace.requirements


def test_areas_are_read_from_the_readme(repo: Repo) -> None:
    root = repo("")
    assert spec_trace.read_areas(root / "spec" / "README.md") == {
        "01": "CON",
        "02": "TRN",
    }


def test_requirements_are_collected_with_their_level(repo: Repo) -> None:
    root = repo(
        "- **CON-1.** A provider MAY serve several versions.\n"
        "- Anything else is breaking. **CON-2.** Old versions SHOULD stay.\n"
        "**CON-3.** A caller MAY read it. **CON-4.** A caller MUST NOT write it.\n"
    )
    trace = spec_trace.check(root)
    assert trace.problems == []
    levels = {rid: r.level for rid, r in trace.requirements.items()}
    assert levels == {
        "CON-1": "MAY",
        "CON-2": "SHOULD",
        "CON-3": "MAY",
        "CON-4": "MUST",
    }
    assert trace.requirements["CON-2"].line == 2


def test_list_items_take_the_level_of_their_lead_in(repo: Repo) -> None:
    root = repo("A transport MUST:\n\n- **CON-1.** carry the context;\n")
    trace = spec_trace.check(root)
    assert trace.problems == []
    assert trace.requirements["CON-1"].level == "MUST"


def test_a_lead_in_does_not_reach_past_its_list(repo: Repo) -> None:
    root = repo("A transport MUST:\n\n- item\n\nProse. **CON-1.** No keyword here.\n")
    assert problems(root) == ["01-contracts.md:5: CON-1 marks no normative statement"]


def test_normative_line_without_an_id_is_a_problem(repo: Repo) -> None:
    root = repo("Intro.\n\nA provider MUST serve the document.\n")
    assert problems(root) == [
        "01-contracts.md:3: normative statement has no requirement ID"
    ]


def test_lowercase_words_and_code_blocks_are_not_normative(repo: Repo) -> None:
    root = repo("Hosts must never race.\n\n```\nA MUST in a sketch\n```\n# MAY\n")
    assert problems(root) == []


def test_duplicate_id_is_a_problem(repo: Repo) -> None:
    root = repo("**CON-1.** It MUST work.\n\n**CON-1.** It MAY also work.\n")
    assert problems(root) == [
        "01-contracts.md:3: CON-1 is already defined at 01-contracts.md:1"
    ]


def test_id_from_another_area_is_a_problem(repo: Repo) -> None:
    root = repo("**TRN-1.** It MUST work.\n")
    assert problems(root) == ["01-contracts.md:1: TRN-1 uses another area; use CON-<n>"]


@pytest.mark.parametrize("rid", ["CON-01", "CON-0", "C-1"])
def test_malformed_id_is_a_problem(repo: Repo, rid: str) -> None:
    root = repo(f"**{rid}.** It MUST work.\n")
    assert problems(root) == [f"01-contracts.md:1: {rid} is malformed; use CON-<n>"]


def test_document_without_an_area_is_a_problem(repo: Repo) -> None:
    root = repo("")
    (root / "spec" / "07-extra.md").write_text("**EXT-1.** It MUST work.\n")
    assert problems(root) == ["07-extra.md: no ID area in the table in spec/README.md"]


def test_citations_are_read_from_markers(repo: Repo) -> None:
    root = repo(
        "**CON-1.** It MUST work.\n\n**CON-2.** It SHOULD work.\n",
        "import pytest\n\n"
        "pytestmark = pytest.mark.spec('CON-2')\n\n"
        "class TestThing:\n"
        "    @pytest.mark.spec('CON-1', 'CON-2')\n"
        "    def test_it(self) -> None: ...\n",
    )
    trace = spec_trace.check(root)
    assert trace.problems == []
    assert [(c.rid, c.owner) for c in trace.citations] == [
        ("CON-2", "<module>"),
        ("CON-1", "test_it"),
        ("CON-2", "test_it"),
    ]


def test_citing_an_unknown_id_is_a_problem(repo: Repo) -> None:
    root = repo(
        "**CON-1.** It MUST work.\n",
        "import pytest\n\n@pytest.mark.spec('CON-9')\ndef test_it() -> None: ...\n",
    )
    assert problems(root) == [
        "tests/test_example.py:3: cites unknown requirement CON-9"
    ]


def test_withdrawn_requirement_is_kept_and_cannot_be_cited(repo: Repo) -> None:
    root = repo(
        "- ~~**CON-1.**~~ Withdrawn in spec 0.2: replaced by CON-2.\n"
        "- **CON-2.** It MUST work.\n",
        "import pytest\n\n@pytest.mark.spec('CON-1')\ndef test_it() -> None: ...\n",
    )
    trace = spec_trace.check(root)
    assert trace.requirements["CON-1"].withdrawn
    assert trace.problems == [
        "tests/test_example.py:3: cites withdrawn requirement CON-1"
    ]


@pytest.mark.parametrize(
    "marker",
    ["pytest.mark.spec(REQ)", "pytest.mark.spec()", "pytest.mark.spec(id='CON-1')"],
)
def test_marker_arguments_must_be_literal_ids(repo: Repo, marker: str) -> None:
    root = repo(
        "**CON-1.** It MUST work.\n",
        f"import pytest\n\nREQ = 'CON-1'\n\n@{marker}\ndef test_it() -> None: ...\n",
    )
    assert len(problems(root)) == 1


def test_enforce_needs_a_test_for_each_must_level_requirement(repo: Repo) -> None:
    root = repo(
        "**CON-1.** It MUST work.\n\n**CON-2.** It SHOULD work.\n\n"
        "~~**CON-3.**~~ Withdrawn in spec 0.2: no longer needed.\n",
        transports="**TRN-1.** It MUST NOT fail.\n",
    )
    assert problems(root) == []
    assert problems(root, ("01",)) == [
        "01-contracts.md:1: CON-1 is must-level and no test cites it"
    ]


def test_main_reports_and_sets_the_exit_code(
    repo: Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repo("**CON-1.** It MUST work.\n")
    assert spec_trace.main(["--root", str(root)]) == 0
    assert "1 requirements, 0 cited by tests, 0 problems" in capsys.readouterr().out
    assert spec_trace.main(["--root", str(root), "--enforce", "01"]) == 1
    assert "problem: 01-contracts.md:1: CON-1" in capsys.readouterr().out
