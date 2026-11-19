"""The spec trace checker (ADR 0018): the specification, settings and report."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import jsonschema
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

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


ENVIRONMENT = {
    "python": "3.12.7",
    "architecture": "x86_64",
    "packages": {"django-neolith": "0.1.0a3", "Django": "5.2.19"},
}


def write_report(
    root: Path, tests: dict[str, tuple[list[str], str]], **extra: Mapping[str, object]
) -> Path:
    """A report as the pytest plugin writes it: test -> (IDs, outcome), and
    any other members of a test's entry by test."""
    path = root / "spec-report.json"
    entries = {
        test: {"ids": ids, "outcome": outcome, **extra.get(test, {})}
        for test, (ids, outcome) in tests.items()
    }
    report = {"format": 2, "environment": ENVIRONMENT, "tests": entries}
    path.write_text(json.dumps(report))
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
    ["not json", '{"format": 2, "tests": {}}', "[]", "[" * 100_000],
)
def test_unreadable_report_is_a_problem(repo: Repo, content: str) -> None:
    root = repo(SPEC)
    path = root / "spec-report.json"
    path.write_text(content)
    (only,) = problems(root, report=path)
    assert only.startswith("report spec-report.json: ")


@pytest.mark.parametrize(
    ("number", "shown"),
    [(1, "1"), (2.0, "2.0"), (True, "true"), ("2", '"2"'), (None, "null")],
)
def test_a_report_of_another_format_is_refused(
    repo: Repo, number: object, shown: str
) -> None:
    root = repo(SPEC)
    path = root / "spec-report.json"
    path.write_text(json.dumps({"format": number, "tests": {}}))
    assert problems(root, report=path) == [
        f"{path}: report format {shown}; this checker reads 2"
    ]


def _report(**changes: object) -> dict[str, object]:
    report: dict[str, object] = {
        "format": 2,
        "environment": json.loads(json.dumps(ENVIRONMENT)),
        "tests": {},
    }
    report.update(changes)
    return report


@pytest.mark.parametrize(
    "report",
    [
        _report(extra=1),
        {"format": 2, "tests": {}},
        _report(tests=[]),
        _report(environment=None),
        _report(environment={**ENVIRONMENT, "os": "linux"}),
        _report(environment={"python": "3.12", "architecture": "x86_64"}),
        _report(environment={**ENVIRONMENT, "python": 3.12}),
        _report(environment={**ENVIRONMENT, "packages": {"Django": "5.2"}}),
        _report(
            environment={
                **ENVIRONMENT,
                "packages": {"django-neolith": 1, "Django": "5.2"},
            }
        ),
        _report(
            environment={
                **ENVIRONMENT,
                "packages": {"django-neolith": None, "Django": None, "x": None},
            }
        ),
    ],
)
def test_a_report_not_as_the_plugin_writes_it_is_refused(
    repo: Repo, report: dict[str, object]
) -> None:
    root = repo(SPEC)
    path = root / "spec-report.json"
    path.write_text(json.dumps(report))
    assert problems(root, report=path) == [
        "report spec-report.json: not a format 2 spec report"
    ]


def test_a_package_that_is_not_installed_has_no_version(repo: Repo) -> None:
    root = repo(SPEC)
    environment = {**ENVIRONMENT, "packages": {"django-neolith": None, "Django": "6.1"}}
    path = root / "spec-report.json"
    path.write_text(json.dumps(_report(environment=environment)))
    trace = spec_trace.check(root, report=path)
    assert trace.problems == []
    assert trace.environment == environment


@pytest.mark.parametrize(
    "entry",
    [
        {"ids": "CON-1", "outcome": "passed"},
        {"ids": [1], "outcome": "passed"},
        {"ids": ["CON-1"]},
        {"outcome": "passed"},
        {"ids": ["CON-1"], "outcome": "ok"},
        {"ids": ["CON-1"], "outcome": "passed", "other": 1},
        {"ids": ["CON-1"], "outcome": "passed", "case": 1},
        {"ids": ["CON-1"], "outcome": "passed", "transport_outcomes": {}},
        {"ids": ["CON-1"], "outcome": "passed", "transport_outcomes": {"http": 1}},
        {"ids": ["CON-1"], "outcome": "passed", "transport_outcomes": ["same"]},
        ["CON-1"],
    ],
)
def test_malformed_report_entry_is_a_problem(repo: Repo, entry: object) -> None:
    root = repo(SPEC)
    path = root / "spec-report.json"
    path.write_text(json.dumps(_report(tests={"t::a": entry})))
    assert problems(root, report=path) == [
        "report spec-report.json: malformed entry for t::a"
    ]


def test_a_full_entry_is_read(repo: Repo) -> None:
    root = repo(SPEC)
    outcomes = {"in-process": "same", "http": "same"}
    entry = {"case": "a_case", "transport_outcomes": outcomes}
    report = write_report(root, {"t::a": (["CON-1"], "passed")}, **{"t::a": entry})
    trace = spec_trace.check(root, report=report)
    assert trace.tests == {
        "t::a": spec_trace.Ran(("CON-1",), "passed", "a_case", outcomes)
    }


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


def test_main_expands_the_report_path(
    repo: Repo, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # As the plugin does: a shell does not expand "~" after "=".
    root = repo("**CON-1.** It MUST work.\n")
    write_report(root, {"tests/t.py::test_a": (["CON-1"], "passed")})
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("REPORTS", str(root))
    for value in ["~/spec-report.json", "$REPORTS/spec-report.json"]:
        assert spec_trace.main(["--root", str(root), f"--report={value}"]) == 0
        assert "1 covered by passing tests" in capsys.readouterr().out


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


# The conformance suite's inputs and its run (ADR 0032, the admitted set and
# Decision 9)

README_RUN = """\
# Spec

- **Status:** draft 0.1. Nothing here is stable.

| # | Document | Defines | In 0.1 | IDs |
|---|---|---|---|---|
| 01 | [Contracts](01-contracts.md) | Contracts | yes | `CON` |
| 02 | [Transports](02-transports.md) | Transports | outline only | `TRN` |
| 06 | [Conformance](06-conformance.md) | Conformance | yes | `CNF` |
"""
CONTRACTS = (
    "**CON-1.** It MUST work.\n\n**CON-2.** It SHOULD work.\n\n"
    "**CON-3.** It MUST NOT leak.\n\n"
    "~~**CON-4.**~~ Withdrawn in spec 0.2: no longer needed.\n"
)
BINDS = "## Requirements that bind others\n\n| ID | Binds |\n|---|---|\n"
CONFORMANCE = "**CNF-1.** Outcomes MUST agree.\n\n" + BINDS + "| `CON-3` | app code |\n"
CASE = {
    "name": "echo_round_trips",
    "part": "contract",
    "state": "None.",
    "procedure": "1. Call echo.",
    "expected": "The request, unchanged.",
    "requirements": ["CNF-1", "CON-1"],
}

Suite = Callable[..., Path]
# Keyword names for the files a test changes most.
_ALIASES = {
    "spec__README_md": "spec/README.md",
    "spec__06_conformance_md": "spec/06-conformance.md",
    "spec__conformance__cases__contract_json": "spec/conformance/cases/contract.json",
    "tests__spec_trace_toml": "tests/spec_trace.toml",
    "tests__test_a_py": "tests/test_a.py",
    "tests__test_b_py": "tests/test_b.py",
}


@pytest.fixture
def suite(tmp_path: Path) -> Suite:
    """Write a spec/ tree with documents 01, 02 and 06, a README that names a
    version, and the files given (a relative path to its text, or None to
    leave it out); return the root."""

    def make(**files: str | None) -> Path:
        tree: dict[str, str | None] = {
            "spec/README.md": README_RUN,
            "spec/01-contracts.md": CONTRACTS,
            "spec/02-transports.md": "**TRN-1.** It MUST move.\n",
            "spec/06-conformance.md": CONFORMANCE,
            "tests/test_a.py": "def test_a(): pass\n",
        }
        tree.update({_ALIASES.get(name, name): text for name, text in files.items()})
        for name, text in tree.items():
            if text is not None:
                (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
                (tmp_path / name).write_text(text, encoding="utf-8")
        return tmp_path

    return make


def cases_file(*cases: object) -> str:
    return json.dumps(list(cases))


# Spec 06's requirements that bind others


@pytest.mark.parametrize(
    "row",
    ["| `CON-3` | app code |", "| CON-3 | app code |", "| CON-3 | app code | x |"],
)
def test_requirements_that_bind_others_are_read(suite: Suite, row: str) -> None:
    root = suite(spec__06_conformance_md="**CNF-1.** It MUST agree.\n\n" + BINDS + row)
    trace = spec_trace.check(root)
    assert trace.problems == []
    assert trace.binds_others == {"CON-3"}


def test_without_the_heading_nothing_binds_others(suite: Suite) -> None:
    text = "**CNF-1.** It MUST agree.\n\n```\n" + BINDS + "| CON-3 | app |\n```\n"
    trace = spec_trace.check(suite(spec__06_conformance_md=text))
    assert trace.problems == []
    assert trace.binds_others == set()


@pytest.mark.parametrize(
    ("rows", "problem"),
    [
        ("| CON-2 | x |", 'CON-2 under "Requirements that bind others" is not a '
         "live must-level requirement"),
        ("| CON-4 | x |", 'CON-4 under "Requirements that bind others" is not a '
         "live must-level requirement"),
        ("| CON-9 | x |", 'CON-9 under "Requirements that bind others" is not a '
         "live must-level requirement"),
        ("| con-3 | x |", '"con-3" under "Requirements that bind others" is not a '
         "requirement ID"),
        ("| CON-3, CON-1 | x |", '"CON-3, CON-1" under "Requirements that bind '
         'others" is not a requirement ID'),
        ("| **CON-3** | x |", '"**CON-3**" under "Requirements that bind others" '
         "is not a requirement ID"),
        ("| CON-3 | x |\n| `CON-3` | y |", 'CON-3 is listed twice under '
         '"Requirements that bind others"'),
        ("| CON-3 |  |", 'CON-3 under "Requirements that bind others" does not say '
         "whom it binds"),
        ("| `CON-3 | x |", '"`CON-3" under "Requirements that bind others" is not '
         "a requirement ID"),
        ("| `` | x |", '"``" under "Requirements that bind others" is not '
         "a requirement ID"),
    ],
)  # fmt: skip
def test_a_row_that_binds_others_must_name_a_live_must(
    suite: Suite, rows: str, problem: str
) -> None:
    root = suite(spec__06_conformance_md="**CNF-1.** It MUST agree.\n\n" + BINDS + rows)
    trace = spec_trace.check(root)
    (only,) = trace.problems
    assert only.split(": ", 1)[1] == problem
    assert only.startswith("06-conformance.md:")
    # A refused row binds no one; a repeated one is counted once.
    assert trace.binds_others == ({"CON-3"} if "twice" in problem else set())


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("## Requirements that bind others\n\nNone.\n\n## Next\n\n| ID | Binds |\n"
         "|---|---|\n| CON-3 | x |\n",
         'no table under "Requirements that bind others"'),
        ("## Requirements that bind others\n\n| Requirement | Binds |\n|---|---|\n"
         "| CON-3 | x |\n",
         'the table under "Requirements that bind others" needs the columns ID '
         "and Binds, once each"),
        ("## Requirements that bind others\n\n| ID | Whom |\n|---|---|\n"
         "| CON-3 | x |\n",
         'the table under "Requirements that bind others" needs the columns ID '
         "and Binds, once each"),
        ("## Requirements that bind others\n\n| ID | ID | Binds |\n|---|---|---|\n"
         "| CON-3 | CON-1 | x |\n",
         'the table under "Requirements that bind others" needs the columns ID '
         "and Binds, once each"),
        (BINDS + "| CON-3 | x |\n\nAnd:\n\n| ID | Binds |\n|---|---|\n"
         "| CON-1 | x |\n",
         'a second table under "Requirements that bind others"'),
        (BINDS + "| CON-3 | x |\n\n" + BINDS + "| CON-1 | x |\n",
         'a second heading "Requirements that bind others"'),
    ],
)  # fmt: skip
def test_the_table_that_binds_others_has_one_form(
    suite: Suite, text: str, problem: str
) -> None:
    root = suite(spec__06_conformance_md="**CNF-1.** It MUST agree.\n\n" + text)
    (only,) = spec_trace.check(root).problems
    assert only.split(": ", 1)[1] == problem


# Case files


@pytest.mark.parametrize(
    "name", ["contract.json", "wire-http.json", "routing-a1.json", "lifecycle.json"]
)
def test_a_case_file_is_read(suite: Suite, name: str) -> None:
    part = name.split(".")[0].split("-")[0]
    case = {**CASE, "part": part}
    root = suite(**{f"spec/conformance/cases/{name}": cases_file(case)})
    trace = spec_trace.check(root)
    assert trace.problems == []
    assert trace.cases == {
        "echo_round_trips": spec_trace.Case(
            "echo_round_trips", f"conformance/cases/{name}", ("CNF-1", "CON-1")
        )
    }


NAMING = (
    ": a case file is named <part>.json or <part>-<topic>.json, where <part> is "
    "contract, wire, routing or lifecycle"
)


@pytest.mark.parametrize(
    "name",
    [
        "other.json",
        "Contract.json",
        "contract.JSON",
        "contract-.json",
        "contract-Topic.json",
        "contract-1st.json",
        "contract-a-b.json",
        "contract_a.json",
        "contract-a_b.json",
        "contract.json.bak",
        "contract-é.json",
        "contracts.json",
        ".contract.json",
        "README.txt",
    ],
)
def test_a_case_file_has_one_form_of_name(suite: Suite, name: str) -> None:
    root = suite(**{f"spec/conformance/cases/{name}": cases_file(CASE)})
    assert problems(root) == [f"conformance/cases/{name}{NAMING}"]


def test_a_directory_among_the_case_files_is_refused(suite: Suite) -> None:
    root = suite(**{"spec/conformance/cases/other/a.json": "[]"})
    assert problems(root) == [f"conformance/cases/other{NAMING}"]
    shutil.rmtree(root / "spec/conformance/cases/other")
    root = suite(**{"spec/conformance/cases/contract.json/a.json": "[]"})
    assert problems(root) == ["conformance/cases/contract.json: is not a file"]


def test_the_cases_folder_is_a_directory(suite: Suite) -> None:
    root = suite(**{"spec/conformance/cases": "[]"})
    assert problems(root) == ["conformance/cases: is not a directory"]


@pytest.mark.parametrize(
    ("content", "problem"),
    [
        ("not json", "cannot be read"),
        ('{"name": "a"}', "is not a JSON array of cases"),
        ("[" * 100_000, "cannot be read"),
        ("[1]", "case 1: is not an object"),
        ("[[]]", "case 1: is not an object"),
    ],
)
def test_a_case_file_is_a_json_array_of_objects(
    suite: Suite, content: str, problem: str
) -> None:
    root = suite(spec__conformance__cases__contract_json=None)
    path = root / "spec/conformance/cases/contract.json"
    path.parent.mkdir(parents=True)
    path.write_text(content)
    (only,) = problems(root)
    assert only.startswith(f"conformance/cases/contract.json: {problem}")


def test_a_case_file_in_another_encoding_is_refused(suite: Suite) -> None:
    root = suite()
    path = root / "spec/conformance/cases/contract.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(cases_file(CASE).encode("utf-16"))
    (only,) = problems(root)
    assert only.startswith("conformance/cases/contract.json: cannot be read: ")


def _without(member: str) -> dict[str, object]:
    return {k: v for k, v in CASE.items() if k != member}


@pytest.mark.parametrize(
    ("case", "problem"),
    [
        *[(_without(m), f"{m}: is missing") for m in CASE],
        ({**CASE, "tags": []}, '"tags": is not a member of a case'),
        ({**CASE, "name": "A"}, 'name: "A" is not [a-z][a-z0-9_]*'),
        ({**CASE, "name": "1a"}, 'name: "1a" is not [a-z][a-z0-9_]*'),
        ({**CASE, "name": "a-b"}, 'name: "a-b" is not [a-z][a-z0-9_]*'),
        ({**CASE, "name": ""}, 'name: "" is not [a-z][a-z0-9_]*'),
        ({**CASE, "name": "a\n"}, 'name: "a\\n" is not [a-z][a-z0-9_]*'),
        ({**CASE, "name": "é"}, 'name: "\\u00e9" is not [a-z][a-z0-9_]*'),
        ({**CASE, "name": 1}, "name: 1 is not [a-z][a-z0-9_]*"),
        ({**CASE, "part": "wire"}, 'part: "wire" is not contract, the file\'s part'),
        ({**CASE, "state": None}, "state: is not a string"),
        ({**CASE, "procedure": ["1."]}, "procedure: is not a string"),
        ({**CASE, "expected": 1}, "expected: is not a string"),
        ({**CASE, "requirements": []}, "requirements: is not a non-empty list of "
         "requirement IDs"),
        ({**CASE, "requirements": "CON-1"}, "requirements: is not a non-empty list "
         "of requirement IDs"),
        ({**CASE, "requirements": None}, "requirements: is not a non-empty list "
         "of requirement IDs"),
        ({**CASE, "requirements": ["CON-9"]}, 'requirements: "CON-9" is not a live '
         "requirement"),
        ({**CASE, "requirements": ["CON-4"]}, 'requirements: "CON-4" is not a live '
         "requirement"),
        ({**CASE, "requirements": [1]}, "requirements: 1 is not a live requirement"),
        ({**CASE, "requirements": ["CON-1", "CON-1"]}, "requirements: CON-1 is "
         "listed twice"),
    ],
)  # fmt: skip
def test_a_case_has_exactly_its_members(
    suite: Suite, case: dict[str, object], problem: str
) -> None:
    root = suite(spec__conformance__cases__contract_json=None)
    path = root / "spec/conformance/cases/contract.json"
    path.parent.mkdir(parents=True)
    path.write_text(cases_file(CASE | {"name": "first"}, case))
    assert problems(root) == [f"conformance/cases/contract.json: case 2: {problem}"]


def test_a_member_given_twice_is_refused(suite: Suite) -> None:
    text = cases_file(CASE)[:-2] + ', "name": "other"}]'
    root = suite(**{"spec/conformance/cases/contract.json": text})
    assert problems(root) == [
        "conformance/cases/contract.json: case 1: name: is given twice"
    ]


def test_a_case_name_is_listed_once(suite: Suite) -> None:
    root = suite(
        **{
            "spec/conformance/cases/contract.json": cases_file(CASE, CASE),
            "spec/conformance/cases/wire.json": cases_file(CASE | {"part": "wire"}),
        }
    )
    assert problems(root) == [
        "echo_round_trips: listed twice in conformance/cases/contract.json",
        "echo_round_trips: listed in conformance/cases/contract.json and "
        "conformance/cases/wire.json",
    ]


# The conformance_case marker


def _marked(root: Path, cases: dict[str, tuple[list[str], str | None]]) -> Path:
    """A report of tests by name: (IDs, case), each passed."""
    return write_report(
        root,
        {test: (ids, "passed") for test, (ids, _) in cases.items()},
        **{test: {"case": case} for test, (_, case) in cases.items() if case},
    )


def test_a_case_run_by_a_test_that_cites_its_ids_is_in_step(suite: Suite) -> None:
    root = suite(**{"spec/conformance/cases/contract.json": cases_file(CASE)})
    report = _marked(
        root,
        {
            "t::a": (["CNF-1", "CON-1", "CON-2"], "echo_round_trips"),
            "t::b[1]": (["CNF-1", "CON-1"], "echo_round_trips"),
            "t::c": (["CON-1"], None),
        },
    )
    assert problems(root, report=report) == []


@pytest.mark.parametrize(
    ("tests", "problem"),
    [
        ({"t::a": (["CNF-1", "CON-1"], "other_case")},
         "t::a: case other_case is not listed"),
        ({"t::a": (["CNF-1", "CON-1"], "Other Case")},
         't::a: case "Other Case" is not listed'),
        ({"t::a": (["CNF-1"], "echo_round_trips")},
         "t::a: case echo_round_trips lists CON-1, which the test does not cite"),
        ({"t::a": (["CNF-1", "CON-1"], None)},
         "case echo_round_trips: no test runs it"),
    ],
)  # fmt: skip
def test_a_marker_and_the_case_files_are_held_in_step(
    suite: Suite, tests: dict[str, tuple[list[str], str | None]], problem: str
) -> None:
    root = suite(**{"spec/conformance/cases/contract.json": cases_file(CASE)})
    found = problems(root, report=_marked(root, tests))
    assert problem in found
    assert len(found) == (2 if "not listed" in problem else 1)


# [not-implemented]


@pytest.mark.parametrize(
    ("rid", "reason", "outcome", "problem"),
    [
        ("CON-1", "x", None, "CON-1: is a MUST; only SHOULD and MAY requirements "
         "may be left unimplemented"),
        ("CON-9", "x", None, "CON-9: is not a live requirement"),
        ("CON-4", "x", None, "CON-4: is not a live requirement"),
        ("CON-2", " ", None, "CON-2: needs a reason"),
        ("CON-2", "x", "passed", "CON-2: is covered by a passing test; remove the "
         "entry"),
    ],
)  # fmt: skip
def test_not_implemented_entries_are_checked(
    suite: Suite, rid: str, reason: str, outcome: str | None, problem: str
) -> None:
    settings = f'[not-implemented]\n{rid} = "{reason}"\n'
    root = suite(tests__spec_trace_toml=settings)
    tests = {"t::a": (["CON-2"], outcome)} if outcome else {}
    report = write_report(root, tests)
    assert problems(root, report=report) == [
        f"tests/spec_trace.toml: [not-implemented] {problem}"
    ]


def test_not_implemented_must_be_a_table(suite: Suite) -> None:
    root = suite(tests__spec_trace_toml='not-implemented = "x"\n')
    assert problems(root) == [
        "tests/spec_trace.toml: [not-implemented] must be a table"
    ]


# The status of each requirement, and whether a run conforms


def run_of(
    root: Path,
    tests: dict[str, tuple[list[str], str]],
    **extra: Mapping[str, object],
) -> spec_trace.Trace:
    return spec_trace.check(
        root, report=write_report(root, tests, **extra), conformance=True
    )


@pytest.mark.parametrize(
    ("outcomes", "listed", "status"),
    [
        (["passed", "failed"], ("binds", "not-tested"), "failed"),
        (["passed", "error"], (), "failed"),
        (["passed", "skipped"], ("binds", "not-tested"), "passed"),
        (["skipped", "xfailed", "xpassed", "not run"], ("binds", "not-tested"),
         "not-applicable"),
        ([], ("binds", "not-tested"), "not-applicable"),
        (["skipped"], ("not-tested",), "not-tested"),
        ([], (), "uncovered"),
        (["xpassed"], (), "uncovered"),
    ],
)  # fmt: skip
def test_a_must_takes_the_first_status_that_applies(
    suite: Suite, outcomes: list[str], listed: tuple[str, ...], status: str
) -> None:
    # CON-3 is a MUST; spec 06 lists it as binding others only when "binds"
    # is listed.
    conformance = CONFORMANCE if "binds" in listed else "**CNF-1.** It MUST agree.\n"
    settings = (
        '[not-tested]\nCON-3 = "Binds app code."\n' if "not-tested" in listed else ""
    )
    root = suite(spec__06_conformance_md=conformance, tests__spec_trace_toml=settings)
    tests = {f"t::{n}": (["CON-3"], outcome) for n, outcome in enumerate(outcomes)}
    trace = run_of(root, tests)
    assert trace.run is not None
    record = trace.run["requirements"]["CON-3"]
    assert record["status"] == status
    assert ("reason" in record) == (status == "not-tested")
    if status == "not-tested":
        assert record["reason"] == "Binds app code."


@pytest.mark.parametrize(
    ("outcomes", "listed", "status"),
    [
        (["failed"], True, "failed"),
        (["passed"], True, "passed"),
        (["skipped"], True, "not-implemented"),
        ([], False, "uncovered"),
    ],
)
def test_a_should_takes_the_first_status_that_applies(
    suite: Suite, outcomes: list[str], listed: bool, status: str
) -> None:
    settings = '[not-implemented]\nCON-2 = "Left out in 0.1."\n' if listed else ""
    root = suite(tests__spec_trace_toml=settings)
    tests = {f"t::{n}": (["CON-2"], outcome) for n, outcome in enumerate(outcomes)}
    trace = run_of(root, tests)
    assert trace.run is not None
    record = trace.run["requirements"]["CON-2"]
    assert record["status"] == status
    if status == "not-implemented":
        assert record["reason"] == "Left out in 0.1."


def test_an_id_that_binds_others_and_is_not_tested_is_not_applicable(
    suite: Suite,
) -> None:
    root = suite(tests__spec_trace_toml='[not-tested]\nCON-3 = "Binds app code."\n')
    trace = run_of(root, {"t::a": (["CON-1", "CNF-1"], "passed")})
    assert trace.problems == []
    assert trace.run is not None
    assert trace.run["requirements"]["CON-3"] == {
        "document": "01",
        "level": "MUST",
        "status": "not-applicable",
        "tests": [],
    }
    assert trace.run["conforms"] is True
    assert trace.nonconforming == []


def test_the_run_has_exactly_its_members(suite: Suite) -> None:
    root = suite()
    outcomes = {"in-process": "same", "http": "same"}
    trace = run_of(
        root,
        {"t::b": (["CON-1", "CNF-1"], "passed"), "t::a": (["CON-1"], "skipped")},
        **{"t::b": {"transport_outcomes": outcomes}},
    )
    run = trace.run
    assert run is not None
    assert list(run) == [
        "format",
        "spec",
        "suite",
        "implementation",
        "environment",
        "requirements",
        "conforms",
    ]
    assert run["format"] == 1
    assert run["spec"] == "0.1"
    assert run["implementation"] == {"name": "django-neolith", "version": "0.1.0a3"}
    assert run["environment"] == ENVIRONMENT
    # Live IDs of every level, in the specification's order; CON-4 is withdrawn.
    assert list(run["requirements"]) == ["CON-1", "CON-2", "CON-3", "TRN-1", "CNF-1"]
    assert run["requirements"]["CON-1"] == {
        "document": "01",
        "level": "MUST",
        "status": "passed",
        "tests": [
            {"test": "t::a", "outcome": "skipped"},
            {"test": "t::b", "outcome": "passed", "transport_outcomes": outcomes},
        ],
    }
    assert run["requirements"]["CON-2"]["status"] == "uncovered"
    # TRN-1 is a MUST of a document outside 0.1, so it does not count.
    assert run["requirements"]["TRN-1"]["status"] == "uncovered"
    assert run["conforms"] is True


@pytest.mark.parametrize(
    ("tests", "reasons"),
    [
        ({"t::a": (["CON-1"], "passed")}, ["CNF-1 uncovered"]),
        ({"t::a": (["CON-1", "CNF-1", "CON-2"], "passed"),
          "t::b": (["CON-2"], "failed")}, ["CON-2 failed"]),
        ({"t::a": (["CON-1", "CNF-1"], "passed"), "t::b": (["TRN-1"], "error")},
         ["TRN-1 failed"]),
        ({"t::a": (["CON-1", "CNF-1"], "passed")}, []),
    ],
)  # fmt: skip
def test_a_run_conforms_only_when_no_requirement_keeps_it_from_it(
    suite: Suite, tests: dict[str, tuple[list[str], str]], reasons: list[str]
) -> None:
    trace = run_of(suite(), tests)
    assert trace.run is not None
    assert trace.nonconforming == reasons
    assert trace.run["conforms"] is (reasons == [])


STATUS = (
    "README.md: one Status line must name the specification version, as "
    '"- **Status:** draft 0.1."'
)


@pytest.mark.parametrize(
    ("readme", "problem"),
    [
        *[(README_RUN.replace("draft 0.1. Nothing", line), STATUS)
          for line in ["draft. Nothing", "draft 0.1 or 0.2. Nothing",
                       "draft spec 0.1. Nothing", "draft 0.1x. Nothing"]],
        (README_RUN + "- **Status:** final 0.1.\n", STATUS),
        (README_RUN.replace("In 0.1", "In 0.2"),
         'README.md:5: the table of documents needs a "#" and an "In 0.1" column, '
         "once each"),
        (README_RUN.replace("| IDs |", "| In 0.1 |"),
         'README.md:5: the table of documents needs a "#" and an "In 0.1" column, '
         "once each"),
        (README_RUN + "\n| # | In 0.1 |\n|---|---|\n| 01 | no |\n",
         'README.md: 2 tables have a "#" or an "In 0.1" column; one table of '
         "documents has both"),
        ("| # | In 0.1 |\n|---|---|\n| 01 | no |\n\n" + README_RUN,
         'README.md: 2 tables have a "#" or an "In 0.1" column; one table of '
         "documents has both"),
        (README_RUN + "| 07 | [Next](07-next.md) | Next | no | `NXT` |\n",
         'README.md:5: the "#" column names "01", "02", "06", "07"; spec/ holds '
         "01, 02, 06, each once"),
        (README_RUN.replace("| 06 |", "| 6 |"),
         'README.md:5: the "#" column names "01", "02", "6"; spec/ holds 01, 02, '
         "06, each once"),
        (README_RUN.replace("| 02 |", "| 01 |"),
         'README.md:5: the "#" column names "01", "01", "06"; spec/ holds 01, 02, '
         "06, each once"),
        (README_RUN.replace("In 0.1", "Ships")
         + "\n| # | In 0.1 |\n|---|---|\n| 0 | yes |\n",
         'README.md: 2 tables have a "#" or an "In 0.1" column; one table of '
         "documents has both"),
        (README_RUN.replace("outline only", "Yes"),
         'README.md:8: "Yes" in the In 0.1 column; write yes, no or outline only'),
    ],
)  # fmt: skip
def test_a_run_needs_the_version_and_its_documents(
    suite: Suite, readme: str, problem: str
) -> None:
    trace = run_of(
        suite(spec__README_md=readme), {"t::a": (["CON-1", "CNF-1"], "passed")}
    )
    # A "#" cell that is not a document's number also breaks the area table,
    # which the checker reports on its own.
    readme_problems = [p for p in trace.problems if p.startswith("README.md")]
    assert readme_problems[-1] == problem
    assert trace.run is not None
    assert trace.run["conforms"] is False
    assert trace.nonconforming[0] == "the specification's version or documents"


def test_a_run_needs_a_report_that_was_read(suite: Suite) -> None:
    root = suite()
    path = root / "spec-report.json"
    path.write_text("[]")
    trace = spec_trace.check(root, report=path, conformance=True)
    assert trace.run is None


def test_the_suite_hashes_name_its_files(suite: Suite) -> None:
    root = suite(**{"spec/conformance/cases/contract.json": cases_file(CASE)})
    first = spec_trace.suite(root)
    assert first["reference"] is None
    assert first["cases"] != hashlib.sha256(b"").hexdigest()
    # Bytecode is not part of the suite.
    (root / "tests/__pycache__").mkdir()
    (root / "tests/__pycache__/test_a.pyc").write_bytes(b"\0")
    assert spec_trace.suite(root) == first
    # An empty directory holds no file, so it changes nothing.
    (root / "tests/helpers").mkdir()
    assert spec_trace.suite(root) == first
    # A test's content, a test's name, or a new file changes it.
    for change in (
        lambda: (root / "tests/test_a.py").write_text("def test_a(): pass\n\n"),
        lambda: (root / "tests/test_a.py").rename(root / "tests/test_b.py"),
        lambda: (root / "tests/helpers/data.json").write_text("{}"),
    ):
        before = spec_trace.suite(root)["tests"]
        change()
        assert spec_trace.suite(root)["tests"] != before
    reference = root / "spec/conformance/reference.v1.openapi.json"
    reference.write_text("{}")
    assert spec_trace.suite(root)["reference"] == hashlib.sha256(b"{}").hexdigest()


def test_the_suite_hash_frames_each_file(suite: Suite) -> None:
    # Moving bytes from one file's end to the next file's start, or into its
    # name, changes the hash.
    one = suite(tests__test_a_py="ab", tests__test_b_py="c")
    first = spec_trace.suite(one)["tests"]
    (one / "tests/test_a.py").write_text("a")
    (one / "tests/test_b.py").write_text("bc")
    assert spec_trace.suite(one)["tests"] != first
    expected = hashlib.sha256()
    for name, data in [("tests/test_a.py", b"a"), ("tests/test_b.py", b"bc")]:
        expected.update(name.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
    assert spec_trace.suite(one)["tests"] == expected.hexdigest()


# --conformance on the command line


def test_main_writes_the_run(suite: Suite, capsys: pytest.CaptureFixture[str]) -> None:
    root = suite()
    report = write_report(root, {"t::a": (["CON-1"], "passed")})
    out = root / "run.json"
    out.write_text("an earlier run")
    argv = ["--root", str(root), "--report", str(report), "--conformance", str(out)]
    assert spec_trace.main(argv) == 0
    printed = capsys.readouterr().out
    assert (
        "conformance run: conforms false\ndoes not conform: CNF-1 uncovered" in printed
    )
    run = json.loads(out.read_text())
    assert run["conforms"] is False
    assert run["requirements"]["CNF-1"]["status"] == "uncovered"


def test_main_leaves_no_earlier_run_when_the_report_cannot_be_read(
    suite: Suite,
) -> None:
    root = suite()
    report = root / "spec-report.json"
    report.write_text("not json")
    out = root / "run.json"
    out.write_text("an earlier run")
    argv = ["--root", str(root), "--report", str(report), "--conformance", str(out)]
    assert spec_trace.main(argv) == 1
    assert out.read_text() == ""


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--conformance", "run.json"], "--conformance needs --report"),
        (["--report", "r.json", "--conformance", ""],
         "--conformance needs a file path, not ''"),
        (["--report", "r.json", "--conformance", "."],
         "--conformance needs a file path, not '.'"),
        (["--report", "r.json", "--conformance", "out/"],
         "--conformance needs a file path, not 'out/'"),
        (["--report", "r.json", "--conformance", "out/.."],
         "--conformance needs a file path, not 'out/..'"),
        (["--report", "r.json", "--conformance", "missing/run.json"],
         "--conformance cannot write {root}/missing/run.json: No such file or "
         "directory"),
    ],
)  # fmt: skip
def test_main_refuses_a_conformance_path_it_cannot_use(
    suite: Suite,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
    message: str,
) -> None:
    root = suite()
    monkeypatch.chdir(root)
    with pytest.raises(SystemExit) as exit_:
        spec_trace.main(["--root", str(root), *args])
    assert exit_.value.code == 2
    assert capsys.readouterr().err.endswith(f"error: {message.format(root=root)}\n")


def test_main_expands_the_conformance_path(
    suite: Suite, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = suite()
    report = write_report(root, {})
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("RUNS", str(root))
    for value in ["~/a.json", "$RUNS/b.json"]:
        argv = ["--root", str(root), "--report", str(report), f"--conformance={value}"]
        spec_trace.main(argv)
    assert json.loads((root / "a.json").read_text())["format"] == 1
    assert json.loads((root / "b.json").read_text())["format"] == 1


# The repository's own run


def test_a_run_of_the_tree_does_not_conform_and_says_why(tmp_path: Path) -> None:
    # Every live requirement of the tree is cited by a passing test, apart
    # from those in [not-tested], and every listed case is run by a test that
    # cites its IDs. The run still does not conform, for exactly the must-level
    # [not-tested] entries of the version's documents that spec 06 does not
    # list as binding others, each with its reason (OVR-5 among them).
    tree = spec_trace.check(spec_trace.ROOT)
    tests = {
        f"t::{rid}": ([rid], "passed")
        for rid, requirement in tree.requirements.items()
        if not requirement.withdrawn and rid not in tree.not_tested
    }
    cases = {
        f"t::case_{name}": (list(case.requirements), "passed")
        for name, case in tree.cases.items()
    }
    extra = {f"t::case_{name}": {"case": name} for name in tree.cases}
    report = write_report(tmp_path, tests | cases, **extra)
    trace = spec_trace.check(spec_trace.ROOT, report=report, conformance=True)
    assert trace.problems == []
    assert trace.run is not None
    version, documents = spec_trace.read_version(
        spec_trace.ROOT / "spec" / "README.md", tree
    )
    assert documents is not None
    expected = [
        f"{rid} not-tested: {reason}"
        for rid, reason in tree.not_tested.items()
        if rid not in tree.binds_others
        and tree.requirements[rid].document[:2] in documents
    ]
    assert "OVR-5" in tree.not_tested
    assert sorted(trace.nonconforming) == sorted(expected)
    assert trace.run["conforms"] is False
    for rid in tree.binds_others:
        assert trace.run["requirements"][rid]["status"] == "not-applicable"
    assert trace.run["spec"] == version


def test_the_run_names_the_version_the_openapi_document_states() -> None:
    from neolith import _openapi

    version, _ = spec_trace.read_version(
        spec_trace.ROOT / "spec" / "README.md", spec_trace.Trace()
    )
    assert version == _openapi._SPEC_VERSION


# The case-file check agrees with a reference: a JSON Schema of the admitted
# set, plus unique names across the file (which a schema cannot state). And no
# input, however malformed, makes the checker raise.

_CASE_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": list(CASE),
        "properties": {
            "name": {"type": "string", "pattern": r"^[a-z][a-z0-9_]*\Z"},
            "part": {"const": "contract"},
            "state": {"type": "string"},
            "procedure": {"type": "string"},
            "expected": {"type": "string"},
            "requirements": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {"enum": ["CON-1", "CON-2", "CON-3", "CNF-1", "TRN-1"]},
            },
        },
    },
}
_NAMES = st.from_regex(r"[a-z][a-z0-9_]{0,3}", fullmatch=True) | st.sampled_from(
    ["A", "1a", "a-b", "", "a\n", "é", "_a"]
)
_TEXT = st.text(max_size=3) | st.none() | st.integers()
_LIVE = st.sampled_from(["CON-1", "CON-2", "CNF-1", "TRN-1"])
_IDS = _LIVE | st.sampled_from(["CON-4", "CON-9", "con-1", 1, ["CON-1"]])
_MEMBER = {
    "name": _NAMES | _TEXT,
    "part": st.sampled_from(["wire", "Contract"]) | _TEXT,
    "state": _TEXT,
    "procedure": _TEXT,
    "expected": _TEXT,
    "requirements": st.lists(_IDS, max_size=3) | _TEXT,
}


@st.composite
def _cases(draw: st.DrawFn) -> object:
    """Cases that are admitted, or one change away from it."""
    cases = []
    for _ in range(draw(st.integers(0, 3))):
        case: dict[str, object] = dict(CASE)
        case["name"] = draw(_NAMES)
        case["requirements"] = draw(
            st.lists(_LIVE, min_size=1, max_size=3, unique=True)
        )
        change = draw(
            st.sampled_from(["none", "none", "none", "replace", "drop", "add"])
        )
        member = draw(st.sampled_from(list(CASE)))
        if change == "replace":
            case[member] = draw(_MEMBER[member])
        elif change == "drop":
            del case[member]
        elif change == "add":
            case[draw(st.sampled_from(["extra", "Name", "tags"]))] = draw(_TEXT)
        cases.append(case)
    return cases


_JSON = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats() | st.text(max_size=3),
    lambda inner: (
        st.lists(inner, max_size=3)
        | st.dictionaries(st.text(max_size=3), inner, max_size=3)
    ),
    max_leaves=10,
)
_SETTINGS = settings(
    derandomize=True,
    database=None,
    deadline=None,
    max_examples=200,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@_SETTINGS
@given(cases=_cases())
def test_the_case_check_agrees_with_the_admitted_set(
    suite: Suite, cases: object
) -> None:
    root = suite(spec__conformance__cases__contract_json=json.dumps(cases))
    validator = jsonschema.Draft202012Validator(_CASE_SCHEMA)
    names = [c.get("name") for c in cases] if isinstance(cases, list) else []
    admitted = validator.is_valid(cases) and len(names) == len(set(names))
    found = problems(root)
    assert (found == []) is admitted, found


@_SETTINGS
@given(value=_JSON, where=st.sampled_from(["cases", "report", "entry"]))
def test_no_input_makes_the_checker_raise(
    suite: Suite, value: object, where: str
) -> None:
    root = suite(spec__conformance__cases__contract_json="[]")
    report = write_report(root, {})
    if where == "cases":
        (root / "spec/conformance/cases/contract.json").write_text(json.dumps(value))
    elif where == "report":
        report.write_text(json.dumps(value))
    else:
        report.write_text(json.dumps(_report(tests={"t::a": value})))
    trace = spec_trace.check(root, report=report, conformance=True)
    assert all(isinstance(problem, str) for problem in trace.problems)
    spec_trace.render(trace).encode("utf-8", "backslashreplace")


# What the red-team pass found: a refused input takes no effect, and the
# suite's hash neither crashes nor misses a file


def test_a_report_with_an_unreadable_entry_counts_no_test(suite: Suite) -> None:
    # The unreadable entry may be the failure that keeps the run from
    # conforming, so nothing is counted and no run is made.
    root = suite()
    path = root / "spec-report.json"
    tests = {
        "t::1": {"ids": ["CON-1", "CNF-1"], "outcome": "passed"},
        "t::2": {"ids": ["CON-1"], "outcome": "failed", "transport_outcomes": {}},
    }
    path.write_text(json.dumps(_report(tests=tests)))
    trace = spec_trace.check(root, report=path, conformance=True)
    assert trace.problems == ["report spec-report.json: malformed entry for t::2"]
    assert not trace.measured
    assert trace.citations == []
    assert trace.run is None


@pytest.mark.parametrize(
    ("settings", "rid", "status"),
    [
        ('[not-implemented]\nCON-1 = "x"\n', "CON-1", "uncovered"),
        ('[not-tested]\nCON-2 = "x"\n', "CON-2", "uncovered"),
        ('[not-tested]\nCON-3 = "x"\n', "CON-3", "not-applicable"),
    ],
)
def test_a_refused_settings_entry_gives_no_status(
    suite: Suite, settings: str, rid: str, status: str
) -> None:
    root = suite(tests__spec_trace_toml=settings)
    trace = run_of(root, {})
    assert trace.run is not None
    assert trace.run["requirements"][rid]["status"] == status
    assert "reason" not in trace.run["requirements"][rid]


def test_the_suite_hash_reads_files_in_path_order(
    suite: Suite, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Whatever order the file system lists them in.
    root = suite(**{"tests/b.py": "b", "tests/a/z.py": "z", "tests/a.py": "a"})
    (root / "tests/test_a.py").unlink()
    expected = hashlib.sha256()
    for name in ["tests/a.py", "tests/a/z.py", "tests/b.py"]:
        data = (root / name).read_bytes()
        expected.update(name.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
    scandir = os.scandir

    @contextlib.contextmanager
    def reversed_scandir(path: str) -> Iterator[list[os.DirEntry[str]]]:
        with scandir(path) as listing:
            yield list(listing)[::-1]

    assert spec_trace.suite(root)["tests"] == expected.hexdigest()
    monkeypatch.setattr(os, "scandir", reversed_scandir)
    assert spec_trace.suite(root)["tests"] == expected.hexdigest()


def test_the_suite_hash_follows_links_to_directories(suite: Suite) -> None:
    root = suite()
    shared = root / "shared"
    shared.mkdir()
    (shared / "data.json").write_text("{}")
    (root / "tests/shared").symlink_to(shared, target_is_directory=True)
    before = spec_trace.suite(root)["tests"]
    (shared / "data.json").write_text("[]")
    assert spec_trace.suite(root)["tests"] != before


def test_a_file_name_that_is_not_utf8_is_hashed(suite: Suite) -> None:
    root = suite()
    name = os.fsdecode(b"bad\xff.py")
    (root / "tests" / name).write_text("x")
    (root / "spec/conformance/cases").mkdir(parents=True)
    (root / "spec/conformance/cases" / os.fsdecode(b"x\xff.json")).write_text("[]")
    found: list[str] = []
    hashes = spec_trace.suite(root, found)
    assert found == []
    assert hashes["tests"] is not None
    assert hashes["cases"] is not None
    (only,) = problems(root)
    assert only.endswith(NAMING)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_a_file_the_hash_cannot_read_is_a_problem(suite: Suite) -> None:
    root = suite()
    secret = root / "tests/secret.py"
    secret.write_text("x")
    secret.chmod(0)
    try:
        trace = run_of(root, {})
    finally:
        secret.chmod(0o600)
    assert trace.problems == [
        "tests/secret.py: cannot be read for the suite's hash: Permission denied"
    ]
    assert trace.run is not None
    assert trace.run["suite"]["tests"] is None


def test_a_test_id_that_is_not_text_is_printed_escaped(
    suite: Suite, capsys: pytest.CaptureFixture[str]
) -> None:
    root = suite()
    path = root / "spec-report.json"
    tests = {"t::\ud800": {"ids": [], "outcome": "passed", "case": "nope"}}
    path.write_text(json.dumps(_report(tests=tests)))
    assert spec_trace.main(["--root", str(root), "--report", str(path)]) == 1
    assert "problem: t::\\ud800: case nope is not listed" in capsys.readouterr().out


def test_a_refused_entry_leaves_the_other_tables_entry_for_its_id(
    suite: Suite,
) -> None:
    settings = (
        '[not-tested]\nCON-2 = "x"\nCON-1 = "Shown later."\n\n'
        '[not-implemented]\nCON-2 = "Left out."\nCON-1 = "y"\n'
    )
    trace = run_of(suite(tests__spec_trace_toml=settings), {})
    assert trace.run is not None
    records = trace.run["requirements"]
    assert (records["CON-2"]["status"], records["CON-2"]["reason"]) == (
        "not-implemented",
        "Left out.",
    )
    assert (records["CON-1"]["status"], records["CON-1"]["reason"]) == (
        "not-tested",
        "Shown later.",
    )


def test_a_report_with_a_member_given_twice_is_refused(suite: Suite) -> None:
    root = suite()
    path = root / "spec-report.json"
    entry = '{"ids": ["CON-1"], "outcome": "%s"}'
    text = json.dumps(_report(tests={})).replace(
        '"tests": {}',
        '"tests": {"t::a": ' + entry % "failed" + ', "t::a": ' + entry % "passed" + "}",
    )
    path.write_text(text)
    trace = spec_trace.check(root, report=path, conformance=True)
    assert trace.problems == [
        'report spec-report.json: cannot be read: "t::a" is given twice in one object'
    ]
    assert trace.run is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
def test_a_directory_that_cannot_be_read_is_a_problem(suite: Suite) -> None:
    root = suite(
        **{"tests/sub/test_b.py": "x", "spec/conformance/cases/wire.json": "[]"}
    )
    for folder in ("tests/sub", "spec/conformance/cases"):
        (root / folder).chmod(0)
    try:
        found: list[str] = []
        hashes = spec_trace.suite(root, found)
        trace = spec_trace.check(root)
    finally:
        for folder in ("tests/sub", "spec/conformance/cases"):
            (root / folder).chmod(0o700)
    assert hashes["tests"] is None
    assert hashes["cases"] is None
    assert sorted(found) == [
        "spec/conformance/cases: cannot be read for the suite's hash: "
        "Permission denied",
        "tests/sub: cannot be read for the suite's hash: Permission denied",
    ]
    assert trace.problems == ["conformance/cases: cannot be read: Permission denied"]


def test_the_suite_hash_does_not_depend_on_which_name_is_listed_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # tests/x and a link tests/y to it: both names are read, in either order.
    root = tmp_path
    (root / "tests/x").mkdir(parents=True)
    (root / "tests/x/f.py").write_text("f")
    (root / "tests/y").symlink_to(root / "tests/x", target_is_directory=True)
    first = spec_trace.suite(root)["tests"]
    scandir = os.scandir

    @contextlib.contextmanager
    def reversed_scandir(path: str) -> Iterator[list[os.DirEntry[str]]]:
        with scandir(path) as listing:
            yield list(listing)[::-1]

    monkeypatch.setattr(os, "scandir", reversed_scandir)
    assert spec_trace.suite(root)["tests"] == first
    expected = hashlib.sha256()
    for name in ["tests/x/f.py", "tests/y/f.py"]:
        expected.update(name.encode() + b"\0" + b"1" + b"\0" + b"f")
    assert first == expected.hexdigest()


# The second review: around each input, what is absent, given twice, of the
# wrong kind or unreadable is a problem, never a default


def test_a_readme_whose_columns_move_is_read_by_name(suite: Suite) -> None:
    readme = (
        "- **Status:** draft 0.1. Nothing here is stable until 0.1 ships.\n\n"
        "| In 0.1 | Document | # | IDs |\n|---|---|---|---|\n"
        "| yes | [C](01-contracts.md) | 01 | `CON` |\n"
        "| no | [T](02-transports.md) | 02 | `TRN` |\n"
        "| yes | [Conformance](06-conformance.md) | 06 | `CNF` |\n"
    )
    root = suite(spec__README_md=README_RUN)
    trace = spec_trace.check(root)
    assert spec_trace.read_version(root / "spec/README.md", trace) == (
        "0.1",
        {"01", "06"},
    )
    (root / "spec/README.md").write_text(readme)
    assert spec_trace.read_version(root / "spec/README.md", trace) == (
        "0.1",
        {"01", "06"},
    )
    assert [p for p in trace.problems if p.startswith("README")] == []


def test_a_document_marked_no_does_not_count(suite: Suite) -> None:
    # TRN-1 is a MUST with no test, in a document marked no.
    root = suite(spec__README_md=README_RUN.replace("outline only", "no"))
    trace = run_of(root, {"t::a": (["CON-1", "CNF-1"], "passed")})
    assert trace.problems == []
    assert trace.run is not None
    assert trace.run["requirements"]["TRN-1"]["status"] == "uncovered"
    assert trace.run["conforms"] is True
    root = suite(spec__README_md=README_RUN.replace("outline only", "yes"))
    trace = run_of(root, {"t::a": (["CON-1", "CNF-1"], "passed")})
    assert trace.nonconforming == ["TRN-1 uncovered"]


@pytest.mark.parametrize(
    ("settings", "problem"),
    [
        (b'enforce = ["\xff"]\n', "'utf-8' codec can't decode"),
        (b"a = " + b"[" * 3000 + b"]" * 3000 + b"\n", ""),
    ],
)
def test_settings_that_cannot_be_read_are_a_problem(
    suite: Suite, settings: bytes, problem: str
) -> None:
    root = suite()
    (root / "tests/spec_trace.toml").write_bytes(settings)
    (only,) = problems(root)
    assert only.startswith(f"tests/spec_trace.toml: {problem}")


def test_settings_that_are_not_a_file_are_a_problem(suite: Suite) -> None:
    root = suite()
    (root / "tests/spec_trace.toml").mkdir()
    assert problems(root) == ["tests/spec_trace.toml: Is a directory"]
    (root / "tests/spec_trace.toml").rmdir()
    (root / "tests/spec_trace.toml").symlink_to(root / "nowhere")
    assert problems(root) == ["tests/spec_trace.toml: No such file or directory"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_settings_that_cannot_be_opened_are_a_problem(suite: Suite) -> None:
    root = suite(tests__spec_trace_toml="enforce = []\n")
    (root / "tests/spec_trace.toml").chmod(0)
    try:
        assert problems(root) == ["tests/spec_trace.toml: Permission denied"]
    finally:
        (root / "tests/spec_trace.toml").chmod(0o600)


def _link(root: Path, name: str, target: str) -> None:
    (root / name).parent.mkdir(parents=True, exist_ok=True)
    (root / name).symlink_to(target)


@pytest.mark.parametrize(
    ("make", "problem"),
    [
        (lambda r: _link(r, "tests/loop", "loop"),
         "tests/loop: cannot be read for the suite's hash: Too many levels of "
         "symbolic links"),
        (lambda r: _link(r, "tests/broken", "nowhere"),
         "tests/broken: cannot be read for the suite's hash: neither a file nor "
         "a directory"),
        (lambda r: os.mkfifo(r / "tests/pipe"),
         "tests/pipe: cannot be read for the suite's hash: neither a file nor a "
         "directory"),
        (lambda r: _link(r, "tests/a/b", ".."),
         "tests/a/b: cannot be read for the suite's hash: a link to a directory "
         "that holds it"),
        (lambda r: _link(r, "tests/a/b/c", "../.."),
         "tests/a/b/c: cannot be read for the suite's hash: a link to a directory "
         "that holds it"),
        (lambda r: _link(r, "tests/up", "."),
         "tests/up: cannot be read for the suite's hash: a link to a directory "
         "that holds it"),
        (lambda r: _link(r, "spec/conformance/cases/wire.json", "wire.json"),
         "spec/conformance/cases/wire.json: cannot be read for the suite's hash: "
         "Too many levels of symbolic links"),
    ],
)  # fmt: skip
def test_what_the_suite_hash_cannot_read_is_a_problem(
    suite: Suite, make: Callable[[Path], None], problem: str
) -> None:
    root = suite(**{"spec/conformance/cases/contract.json": "[]"})
    make(root)
    found: list[str] = []
    hashes = spec_trace.suite(root, found)
    assert found == [problem]
    assert None in hashes.values()


def test_a_case_file_that_is_not_a_file_is_refused(suite: Suite) -> None:
    root = suite()
    _link(root, "spec/conformance/cases/wire.json", "wire.json")
    assert problems(root) == ["conformance/cases/wire.json: is not a file"]


def test_main_writes_no_run_when_the_check_finds_a_problem(suite: Suite) -> None:
    # The report was read, but a test runs a case no case file lists.
    root = suite()
    report = write_report(
        root, {"t::a": (["CON-1"], "passed")}, **{"t::a": {"case": "x"}}
    )
    out = root / "run.json"
    argv = ["--root", str(root), "--report", str(report), "--conformance", str(out)]
    assert spec_trace.main(argv) == 1
    assert out.read_text() == ""


def test_a_report_that_does_not_exist_is_a_problem(suite: Suite) -> None:
    root = suite()
    (only,) = problems(root, report=root / "missing.json")
    assert only.startswith("report missing.json: cannot be read: ")


def test_a_list_among_a_cases_requirements_is_refused(suite: Suite) -> None:
    case = {**CASE, "requirements": [["CON-1"]]}
    root = suite(**{"spec/conformance/cases/contract.json": cases_file(case)})
    assert problems(root) == [
        'conformance/cases/contract.json: case 1: requirements: ["CON-1"] is not '
        "a live requirement"
    ]
