"""The spec trace checker (ADR 0018): the specification, settings and report."""

from __future__ import annotations

import json
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
    """Write a small spec/ tree, and optionally the settings, and return the root."""

    def make(spec: str, transports: str = "", settings: str | None = None) -> Path:
        (tmp_path / "spec").mkdir(exist_ok=True)
        (tmp_path / "tests").mkdir(exist_ok=True)
        (tmp_path / "spec" / "README.md").write_text(README)
        (tmp_path / "spec" / "01-contracts.md").write_text(spec)
        (tmp_path / "spec" / "02-transports.md").write_text(transports)
        settings_file = tmp_path / "tests" / "spec_trace.toml"
        settings_file.unlink(missing_ok=True)
        if settings is not None:
            settings_file.write_text(settings)
        return tmp_path

    return make


def write_report(root: Path, tests: dict[str, tuple[list[str], str]]) -> Path:
    """A report as the pytest plugin writes it: test -> (IDs, outcome)."""
    path = root / "spec-report.json"
    entries = {
        test: {"ids": ids, "outcome": outcome} for test, (ids, outcome) in tests.items()
    }
    path.write_text(json.dumps({"format": 1, "tests": entries}))
    return path


def problems(
    root: Path, enforce: tuple[str, ...] = (), report: Path | None = None
) -> list[str]:
    return spec_trace.check(root, enforce, report).problems


# The repository itself


def test_repository_spec_and_settings_are_consistent() -> None:
    # Coverage of the enforced documents needs the report of a full run, which
    # CI checks after pytest; here only its absence is reported.
    trace = spec_trace.check(spec_trace.ROOT)
    assert [p for p in trace.problems if not p.startswith("enforce: coverage")] == []
    assert trace.requirements


# Reading the specification


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


def test_lead_in_takes_its_strongest_word(repo: Repo) -> None:
    root = repo("A host MAY, and a provider MUST:\n\n- **CON-1.** answer;\n")
    assert spec_trace.check(root).requirements["CON-1"].level == "MUST"


def test_item_under_a_lead_in_needs_an_id(repo: Repo) -> None:
    root = repo(
        "A transport MUST:\n\n- **CON-1.** carry the context;\n"
        "- retry every call forever;\n- **CON-2.** enforce a deadline.\n"
    )
    assert problems(root) == [
        "01-contracts.md:4: item under a normative lead-in has no ID"
    ]


def test_lead_in_needs_a_list_after_it(repo: Repo) -> None:
    root = repo("A provider MUST do this:\n\nA paragraph.\n\nA host MUST:\n")
    assert problems(root) == [
        "01-contracts.md:1: normative lead-in has no list after it",
        "01-contracts.md:5: normative lead-in has no list after it",
    ]


def test_a_lead_in_does_not_reach_past_its_list(repo: Repo) -> None:
    root = repo(
        "A transport MUST:\n\n- **CON-1.** carry it;\n\nProse.\n\n- an ordinary item\n"
    )
    assert problems(root) == []


def test_lead_in_carrying_an_id_still_governs_its_list(repo: Repo) -> None:
    root = repo("**CON-1.** An app MUST:\n\n- log every call;\n- never sleep;\n")
    assert problems(root) == [
        "01-contracts.md:1: CON-1 is on a normative lead-in; "
        "give the IDs to its list items",
        "01-contracts.md:3: item under a normative lead-in has no ID",
        "01-contracts.md:4: item under a normative lead-in has no ID",
    ]


def test_lead_in_is_the_last_sentence_only(repo: Repo) -> None:
    root = repo(
        "**CON-1.** Apps MUST NOT do it. It has two kinds of field:\n\n"
        "- **Propagated fields**, passed on;\n- **Per-call fields**.\n\n"
        "**CON-2.** It MUST work. A transport MUST:\n\n- **CON-3.** carry it.\n"
    )
    trace = spec_trace.check(root)
    assert trace.problems == []
    assert trace.requirements["CON-3"].level == "MUST"


def test_wrapped_item_keeps_its_lead_in_open(repo: Repo) -> None:
    root = repo(
        "A transport MUST:\n\n- **CON-1.** enforce a deadline,\n"
        "  measured on a monotonic clock;\n- retry every call forever.\n"
    )
    assert problems(root) == [
        "01-contracts.md:5: item under a normative lead-in has no ID"
    ]


def test_indented_paragraph_belongs_to_its_item(repo: Repo) -> None:
    root = repo(
        "A transport MUST:\n\n- **CON-1.** carry the context;\n\n"
        "  The context includes the deadline.\n\n- **CON-2.** enforce it.\n"
    )
    trace = spec_trace.check(root)
    assert trace.problems == []
    assert trace.requirements["CON-2"].level == "MUST"


def test_normative_continuation_needs_its_own_id(repo: Repo) -> None:
    root = repo("- **CON-1.** It MUST work.\n\n  Hosts MUST log it.\n")
    assert problems(root) == [
        "01-contracts.md:3: normative statement has no requirement ID"
    ]


def test_wrapped_paragraph_is_one_statement(repo: Repo) -> None:
    root = repo("**CON-1.** A provider\nMUST serve the document.\n")
    trace = spec_trace.check(root)
    assert trace.problems == []
    assert trace.requirements["CON-1"].level == "MUST"


def test_table_row_with_a_normative_word_needs_an_id(repo: Repo) -> None:
    root = repo("| Kind | Rule |\n|---|---|\n| timeout | MUST retry |\n")
    assert problems(root) == [
        "01-contracts.md:3: normative statement has no requirement ID"
    ]


def test_normative_line_without_an_id_is_a_problem(repo: Repo) -> None:
    root = repo("Intro.\n\nA provider MUST serve the document.\n")
    assert problems(root) == [
        "01-contracts.md:3: normative statement has no requirement ID"
    ]


def test_normative_text_before_the_first_id_is_a_problem(repo: Repo) -> None:
    root = repo("Providers MUST log. **CON-1.** The transport MUST run inline.\n")
    assert problems(root) == [
        "01-contracts.md:1: normative text before the first requirement ID"
    ]


def test_an_id_marks_exactly_one_normative_word(repo: Repo) -> None:
    root = repo(
        "**CON-1.** Providers MUST ignore it; callers MUST ignore it too.\n\n"
        "**CON-2.** No keyword here.\n"
    )
    assert problems(root) == [
        "01-contracts.md:1: CON-1 holds 2 normative words; "
        "give each requirement its own ID",
        "01-contracts.md:3: CON-2 marks no normative statement",
    ]


def test_lowercase_words_and_code_blocks_are_not_normative(repo: Repo) -> None:
    root = repo("Hosts must never race.\n\n```\nA MUST in a sketch\n```\n")
    assert problems(root) == []


@pytest.mark.parametrize("word", ["SHALL", "SHALL NOT", "RECOMMENDED", "OPTIONAL"])
def test_unlisted_normative_word_is_a_problem(repo: Repo, word: str) -> None:
    root = repo(f"**CON-1.** A provider {word} serve it.\n")
    assert problems(root) == [
        f"01-contracts.md:1: {word} is not one of the normative words in "
        "spec/README.md; use MUST, MUST NOT, REQUIRED, SHOULD, MAY"
    ]


def test_heading_with_a_normative_word_is_a_problem(repo: Repo) -> None:
    root = repo("## Providers MUST serve it\n\n**CON-1.** It MUST work.\n")
    assert problems(root) == ["01-contracts.md:1: heading uses a normative word"]


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


@pytest.mark.parametrize("name", ["7-extra.md", "07-Extra.md", "notes.md"])
def test_unnumbered_document_is_a_problem(repo: Repo, name: str) -> None:
    root = repo("")
    (root / "spec" / name).write_text("It MUST work.\n")
    assert problems(root) == [
        f"{name}: not a top-level document named NN-lowercase-words.md"
    ]


def test_two_documents_with_one_number_are_a_problem(repo: Repo) -> None:
    root = repo("")
    (root / "spec" / "01-other.md").write_text("")
    assert problems(root) == ["01-other.md: another document has number 01"]


def test_repeated_number_or_area_in_the_readme_is_a_problem(repo: Repo) -> None:
    root = repo("")
    with (root / "spec" / "README.md").open("a") as readme:
        readme.write("| 01 | [Again](01-contracts.md) | Again | yes | `XYZ` |\n")
        readme.write("| 03 | [HTTP](03-http.md) | HTTP | yes | `CON` |\n")
    assert problems(root) == [
        "README.md: document 01 is listed twice",
        "README.md: area CON is listed twice",
    ]


def test_document_without_an_area_is_a_problem(repo: Repo) -> None:
    root = repo("")
    (root / "spec" / "07-extra.md").write_text("**EXT-1.** It MUST work.\n")
    assert problems(root) == ["07-extra.md: no ID area in the table in spec/README.md"]


# Code fences


@pytest.mark.parametrize("fence", ["```", "~~~", "````", "  ```"])
def test_fenced_code_is_skipped(repo: Repo, fence: str) -> None:
    root = repo(
        f"{fence} text\nA MUST in a sketch\n{fence.strip()}\n\n"
        "**CON-1.** It MUST work.\n"
    )
    trace = spec_trace.check(root)
    assert trace.problems == []
    assert list(trace.requirements) == ["CON-1"]


@pytest.mark.parametrize(
    "closing",
    ["~~~", "``", "```` x", "    ```"],
    ids=["other-char", "shorter", "info", "indented"],
)
def test_fence_closes_only_on_a_matching_fence(repo: Repo, closing: str) -> None:
    root = repo(
        f"````\nsketch\n{closing}\nA MUST hidden\n````\n\n**CON-1.** It MUST work.\n"
    )
    trace = spec_trace.check(root)
    assert trace.problems == []
    assert list(trace.requirements) == ["CON-1"]


def test_unclosed_fence_is_a_problem(repo: Repo) -> None:
    root = repo(
        "**CON-1.** It MUST work.\n\nExample:\n\n```\ncall()\n\nProviders MUST retry.\n"
    )
    assert problems(root) == ["01-contracts.md:5: code fence is never closed"]


def test_backtick_fence_info_cannot_hold_a_backtick(repo: Repo) -> None:
    root = repo("``` a`b\nA provider MUST serve it.\n")
    assert problems(root) == [
        "01-contracts.md:1: normative statement has no requirement ID"
    ]


def test_indented_code_is_skipped(repo: Repo) -> None:
    root = repo("Intro.\n\n    A provider MUST serve it.\n")
    assert problems(root) == []


def test_fence_left_open_in_a_list_item_ends_with_the_item(repo: Repo) -> None:
    # The review's probe: an unclosed fence under TRN-2 must not hide the rest.
    root = repo(
        "",
        transports=(
            "A transport MUST:\n\n- **TRN-1.** serialize the request;\n"
            "- **TRN-2.** carry the call context intact;\n\n"
            "  ```\n  context -> headers\n"
            "- **TRN-3.** map every failure onto a standard error kind.\n\n"
            "## In-process transport\n\n"
            "Providers MUST retry every call forever.\n\n"
            "Example:\n\n```text\ncall()\n```\n"
        ),
    )
    trace = spec_trace.check(root)
    assert list(trace.requirements) == ["TRN-1", "TRN-2", "TRN-3"]
    assert trace.problems == [
        "02-transports.md:6: code fence is never closed",
        "02-transports.md:12: normative statement has no requirement ID",
    ]


def test_item_with_no_paragraph_under_a_lead_in_needs_an_id(repo: Repo) -> None:
    root = repo("A transport MUST:\n\n- **CON-1.** carry it;\n- ```\n  x\n  ```\n")
    assert problems(root) == [
        "01-contracts.md:4: item under a normative lead-in has no ID"
    ]


def test_normative_word_in_an_html_block_needs_an_id(repo: Repo) -> None:
    root = repo("<!-- A provider MUST hide this. -->\n")
    assert problems(root) == [
        "01-contracts.md:1: normative statement has no requirement ID"
    ]


# Lead-ins without a colon, and other block shapes


def test_paragraph_before_a_list_is_a_lead_in(repo: Repo) -> None:
    root = repo(
        "**CON-1.** An app MUST do all of the following.\n\n"
        "- log every call;\n- never sleep.\n"
    )
    assert problems(root) == [
        "01-contracts.md:1: CON-1 is on a normative lead-in; "
        "give the IDs to its list items",
        "01-contracts.md:3: item under a normative lead-in has no ID",
        "01-contracts.md:4: item under a normative lead-in has no ID",
    ]


LEAD_IN = (
    "A transport MUST:\n\n- **TRN-1.** carry it;\n- **TRN-2.** enforce a deadline;\n"
)


@pytest.mark.parametrize(
    ("first", "added"),
    [
        (LEAD_IN, "* retry every failed call forever.\n"),
        (
            "A transport MUST:\n\n1. **TRN-1.** carry it;\n2. **TRN-2.** enforce it;\n",
            "3) retry every failed call forever.\n",
        ),
        (LEAD_IN, "\n<!-- added in 0.2 -->\n\n- retry every failed call forever.\n"),
    ],
    ids=["changed-bullet", "changed-delimiter", "html-comment"],
)
def test_list_split_off_a_governed_list_is_governed(
    repo: Repo, first: str, added: str
) -> None:
    root = repo("", transports=first + added)
    (only,) = problems(root)
    assert only.endswith(": item under a normative lead-in has no ID")


def test_lead_in_reaches_its_list_across_an_html_block(repo: Repo) -> None:
    root = repo(
        "",
        transports="A transport MUST:\n\n<!-- note -->\n\n- **TRN-1.** carry it;\n"
        "- retry forever.\n",
    )
    assert problems(root) == [
        "02-transports.md:6: item under a normative lead-in has no ID"
    ]


@pytest.mark.parametrize(
    "between",
    ["Other rules follow.\n", "## Routing\n", "---\n", "```\nsketch\n```\n"],
    ids=["paragraph", "heading", "rule", "code"],
)
def test_other_blocks_end_the_lead_in(repo: Repo, between: str) -> None:
    root = repo("", transports=LEAD_IN + "\n" + between + "\n- an ordinary item.\n")
    assert problems(root) == []


def test_nested_governed_list_does_not_govern_the_next_item(repo: Repo) -> None:
    root = repo(
        "- A host MUST:\n  - **CON-1.** boot;\n  - **CON-2.** stop.\n"
        "- An ordinary item.\n"
    )
    assert problems(root) == []


def test_plain_colon_paragraph_after_a_governed_list_continues_it(
    repo: Repo,
) -> None:
    root = repo("", transports=LEAD_IN + "\nAnd also:\n\n- retry forever.\n")
    assert problems(root) == [
        "02-transports.md:8: item under a normative lead-in has no ID"
    ]


def test_ordered_list_resuming_its_numbers_is_governed(repo: Repo) -> None:
    root = repo(
        "",
        transports="A transport MUST:\n\n1. **TRN-1.** carry it;\n"
        "2. **TRN-2.** enforce it;\n\nA note.\n\n3. retry forever.\n",
    )
    assert problems(root) == [
        "02-transports.md:8: item under a normative lead-in has no ID"
    ]


@pytest.mark.parametrize(
    "between", ["A note.\n\n1. restart.\n", "## Routing\n\n3. other.\n"]
)
def test_new_numbering_or_section_ends_an_ordered_list(
    repo: Repo, between: str
) -> None:
    root = repo(
        "",
        transports="A transport MUST:\n\n1. **TRN-1.** carry it;\n"
        "2. **TRN-2.** enforce it;\n\n" + between,
    )
    assert problems(root) == []


def test_clause_after_a_semicolon_is_the_last_clause(repo: Repo) -> None:
    root = repo(
        "**CON-1.** A failure MUST stop the host; nothing is deferred.\n\n"
        "1. **Declare.** Read every declaration.\n2. **Resolve.** Find providers.\n"
    )
    assert problems(root) == []


def test_ordered_items_with_a_parenthesis_are_items(repo: Repo) -> None:
    root = repo("A transport MUST:\n\n1) **CON-1.** carry it;\n2) retry forever.\n")
    assert problems(root) == [
        "01-contracts.md:4: item under a normative lead-in has no ID"
    ]


def test_id_in_a_heading_is_a_problem(repo: Repo) -> None:
    root = repo("## **CON-1.** Contracts\n")
    assert problems(root) == ["01-contracts.md:1: CON-1 is in a heading"]


def test_hash_without_a_space_is_not_a_heading(repo: Repo) -> None:
    root = repo("#tag A provider MUST serve it.\n")
    assert problems(root) == [
        "01-contracts.md:1: normative statement has no requirement ID"
    ]


def test_setext_heading_is_a_heading(repo: Repo) -> None:
    root = repo("Providers MUST serve\n====================\n")
    assert problems(root) == ["01-contracts.md:1: heading uses a normative word"]


def test_document_in_a_subdirectory_is_a_problem(repo: Repo) -> None:
    root = repo("")
    (root / "spec" / "extra").mkdir()
    (root / "spec" / "extra" / "01-more.md").write_text("It MUST work.\n")
    assert problems(root) == [
        "extra/01-more.md: not a top-level document named NN-lowercase-words.md"
    ]


def test_item_lead_in_governs_only_its_nested_items(repo: Repo) -> None:
    root = repo(
        "- **CON-1.** It MUST work.\n- A host MUST:\n  - **CON-2.** boot;\n"
        "  - stop.\n- An ordinary item.\n"
    )
    assert problems(root) == [
        "01-contracts.md:4: item under a normative lead-in has no ID"
    ]


# Withdrawn requirements


@pytest.mark.parametrize(
    "line",
    [
        "- ~~**CON-1.** A connector MUST retry.~~ Withdrawn in spec 0.2: unsafe.\n",
        "- ~~**CON-1.**~~ Withdrawn in spec 0.2: replaced by CON-2.\n",
    ],
)
def test_withdrawn_requirement_is_kept_and_cannot_be_cited(
    repo: Repo, line: str
) -> None:
    root = repo(line + "- **CON-2.** It MUST work.\n")
    report = write_report(root, {"tests/t.py::test_a": (["CON-1"], "passed")})
    trace = spec_trace.check(root, report=report)
    assert trace.requirements["CON-1"].withdrawn
    assert trace.requirements["CON-1"].level is None
    assert trace.problems == ["tests/t.py::test_a: cites withdrawn requirement CON-1"]


def test_live_text_after_a_withdrawal_needs_an_id(repo: Repo) -> None:
    root = repo(
        "~~**CON-1.** It MUST retry.~~ Withdrawn in spec 0.2: unsafe. "
        "Providers MUST log.\n"
    )
    assert problems(root) == [
        "01-contracts.md:1: normative text after withdrawn CON-1 has no requirement ID"
    ]


def test_withdrawn_requirement_says_when_and_why(repo: Repo) -> None:
    root = repo("- ~~**CON-1.** A connector MUST retry.~~ Gone.\n")
    assert problems(root) == [
        "01-contracts.md:1: CON-1 is struck through but does not say "
        '"Withdrawn in spec <version>: <reason>"'
    ]


# Coverage from the report


SPEC = (
    "**CON-1.** It MUST work.\n\n**CON-2.** It SHOULD work.\n\n"
    "~~**CON-3.**~~ Withdrawn in spec 0.2: no longer needed.\n"
)


@pytest.mark.parametrize("outcome", ["passed", "skipped", "not run"])
def test_unknown_citation_in_the_report_is_a_problem(repo: Repo, outcome: str) -> None:
    root = repo(SPEC)
    report = write_report(root, {"tests/t.py::test_a": (["NOPE-1"], outcome)})
    assert problems(root, report=report) == [
        "tests/t.py::test_a: cites unknown requirement NOPE-1"
    ]


@pytest.mark.parametrize(
    "outcome", ["skipped", "xfailed", "xpassed", "failed", "error", "not run"]
)
def test_only_a_passing_test_covers(repo: Repo, outcome: str) -> None:
    root = repo(SPEC)
    report = write_report(root, {"tests/t.py::test_a": (["CON-1"], outcome)})
    assert problems(root, ("01",), report) == [
        "01-contracts.md:1: CON-1 is must-level and no passing test cites it"
    ]


def test_passing_test_covers_and_only_must_level_is_enforced(repo: Repo) -> None:
    root = repo(SPEC, transports="**TRN-1.** It MUST NOT fail.\n")
    report = write_report(root, {"tests/t.py::test_a": (["CON-1"], "passed")})
    trace = spec_trace.check(root, ("01",), report)
    assert trace.problems == []
    assert [c.test for c in trace.covering("CON-1")] == ["tests/t.py::test_a"]


def test_enforced_documents_are_read_from_the_settings(repo: Repo) -> None:
    root = repo(SPEC, settings='enforce = ["01"]\n')
    report = write_report(root, {})
    assert problems(root, report=report) == [
        "01-contracts.md:1: CON-1 is must-level and no passing test cites it"
    ]


def test_enforcing_without_a_report_is_a_problem(repo: Repo) -> None:
    root = repo(SPEC, settings='enforce = ["01"]\n')
    assert problems(root) == [
        "enforce: coverage cannot be checked without --report; run pytest with "
        "--spec-report first"
    ]


@pytest.mark.parametrize(
    "content",
    ["not json", '{"format": 2, "tests": {}}', "[]", '{"format": 1, "tests": []}'],
)
def test_unreadable_report_is_a_problem(repo: Repo, content: str) -> None:
    root = repo(SPEC)
    path = root / "spec-report.json"
    path.write_text(content)
    (only,) = problems(root, report=path)
    assert only.startswith("report spec-report.json: ")


def test_malformed_report_entry_is_a_problem(repo: Repo) -> None:
    root = repo(SPEC)
    path = root / "spec-report.json"
    path.write_text('{"format": 1, "tests": {"t::a": {"ids": "CON-1"}}}')
    assert problems(root, report=path) == [
        "report spec-report.json: malformed entry for t::a"
    ]


def test_not_tested_entry_stands_in_for_a_test(repo: Repo) -> None:
    root = repo(
        SPEC,
        settings='enforce = ["01"]\n\n[not-tested]\nCON-1 = "Binds app code."\n',
    )
    trace = spec_trace.check(root, report=write_report(root, {}))
    assert trace.problems == []
    assert trace.not_tested == {"CON-1": "Binds app code."}
    assert "not tested" in spec_trace.render(trace)


@pytest.mark.parametrize(
    ("rid", "reason", "outcome", "problem"),
    [
        ("CON-1", "", None, "needs a reason"),
        ("CON-9", "x", None, "is not a live requirement"),
        ("CON-3", "x", None, "is not a live requirement"),
        ("CON-2", "x", None, "is not must-level, so it needs no entry"),
        ("CON-1", "x", "passed", "is covered by a passing test; remove the entry"),
    ],
)
def test_not_tested_entries_are_checked(
    repo: Repo, rid: str, reason: str, outcome: str | None, problem: str
) -> None:
    root = repo(SPEC, settings=f'[not-tested]\n{rid} = "{reason}"\n')
    tests = {"tests/t.py::test_a": (["CON-1"], outcome)} if outcome else {}
    report = write_report(root, tests)
    assert problems(root, report=report) == [
        f"tests/spec_trace.toml: [not-tested] {rid} {problem}"
    ]


def test_skipped_test_does_not_clear_a_not_tested_entry(repo: Repo) -> None:
    root = repo(SPEC, settings='[not-tested]\nCON-1 = "Binds app code."\n')
    report = write_report(root, {"tests/t.py::test_a": (["CON-1"], "skipped")})
    assert problems(root, report=report) == []


# The settings file


@pytest.mark.parametrize("number", ["1", "07", "01-contracts"])
def test_enforcing_a_missing_document_is_a_problem(repo: Repo, number: str) -> None:
    message = f"enforce: {number!r} is not the two-digit number of a document in spec/"
    root = repo(SPEC, settings=f'enforce = ["{number}"]\n')
    assert problems(root) == [message]
    assert problems(repo(SPEC), (number,)) == [message]


@pytest.mark.parametrize(
    ("settings", "problem"),
    [
        ('enforced = ["01"]\n', "unknown key 'enforced'"),
        ('[not_tested]\nCON-1 = "x"\n', "unknown key 'not_tested'"),
        ("enforce = 1\n", "enforce must be a list of strings"),
        ('not-tested = "x"\n', "[not-tested] must be a table"),
    ],
)
def test_settings_mistakes_are_problems(
    repo: Repo, settings: str, problem: str
) -> None:
    assert problems(repo(SPEC, settings=settings)) == [
        f"tests/spec_trace.toml: {problem}"
    ]


def test_unreadable_settings_are_a_problem(repo: Repo) -> None:
    (only,) = problems(repo(SPEC, settings="enforce = [\n"))
    assert only.startswith("tests/spec_trace.toml: ")


def test_main_reports_and_sets_the_exit_code(
    repo: Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repo("**CON-1.** It MUST work.\n")
    assert spec_trace.main(["--root", str(root)]) == 0
    assert "coverage not measured (no --report)" in capsys.readouterr().out
    report = write_report(root, {"tests/t.py::test_a": (["CON-1"], "passed")})
    assert spec_trace.main(["--root", str(root), "--report", str(report)]) == 0
    out = capsys.readouterr().out
    assert "1 requirements, 1 covered by passing tests, 0 marked not tested" in out
    empty = write_report(root, {})
    argv = ["--root", str(root), "--report", str(empty), "--enforce", "01"]
    assert spec_trace.main(argv) == 1
    assert "problem: 01-contracts.md:1: CON-1" in capsys.readouterr().out
