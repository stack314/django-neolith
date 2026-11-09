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

    def make(
        spec: str, tests: str = "", transports: str = "", settings: str | None = None
    ) -> Path:
        (tmp_path / "spec").mkdir(exist_ok=True)
        (tmp_path / "tests").mkdir(exist_ok=True)
        (tmp_path / "spec" / "README.md").write_text(README)
        (tmp_path / "spec" / "01-contracts.md").write_text(spec)
        (tmp_path / "spec" / "02-transports.md").write_text(transports)
        (tmp_path / "tests" / "test_example.py").write_text(tests)
        settings_file = tmp_path / "tests" / "spec_trace.toml"
        settings_file.unlink(missing_ok=True)
        if settings is not None:
            settings_file.write_text(settings)
        return tmp_path

    return make


def problems(root: Path, enforce: tuple[str, ...] = ()) -> list[str]:
    return spec_trace.check(root, enforce).problems


def cites(*rids: str, mark: str = "pytest.mark") -> str:
    """A test module with one test citing ``rids``."""
    args = ", ".join(repr(rid) for rid in rids)
    return f"import pytest\n\n@{mark}.spec({args})\ndef test_it() -> None: ...\n"


# The repository itself


def test_repository_spec_citations_and_settings_are_consistent() -> None:
    trace = spec_trace.check(spec_trace.ROOT)
    assert trace.problems == []
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
    assert problems(root) == [f"{name}: not named NN-lowercase-words.md"]


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
    root = repo(line + "- **CON-2.** It MUST work.\n", cites("CON-1"))
    trace = spec_trace.check(root)
    assert trace.requirements["CON-1"].withdrawn
    assert trace.requirements["CON-1"].level is None
    assert trace.problems == [
        "tests/test_example.py:3: cites withdrawn requirement CON-1"
    ]


def test_withdrawn_requirement_says_when_and_why(repo: Repo) -> None:
    root = repo("- ~~**CON-1.** A connector MUST retry.~~ Gone.\n")
    assert problems(root) == [
        "01-contracts.md:1: CON-1 is struck through but does not say "
        '"Withdrawn in spec <version>: <reason>"'
    ]


# Citations


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


@pytest.mark.parametrize("mark", ["pytest.mark", "pt.mark", "mark"])
def test_citing_an_unknown_id_is_a_problem(repo: Repo, mark: str) -> None:
    root = repo("**CON-1.** It MUST work.\n", cites("NOPE-1", mark=mark))
    assert problems(root) == [
        "tests/test_example.py:3: cites unknown requirement NOPE-1"
    ]


@pytest.mark.parametrize(
    "marker",
    [
        "pytest.mark.spec(REQ)",
        "pytest.mark.spec()",
        "pytest.mark.spec(id='CON-1')",
    ],
)
def test_marker_arguments_must_be_literal_ids(repo: Repo, marker: str) -> None:
    root = repo(
        "**CON-1.** It MUST work.\n",
        f"import pytest\n\nREQ = 'CON-1'\n\n@{marker}\ndef test_it() -> None: ...\n",
    )
    assert len(problems(root)) == 1


def test_marker_used_through_an_alias_is_a_problem(repo: Repo) -> None:
    root = repo(
        "**CON-1.** It MUST work.\n",
        "import pytest\n\ncites = pytest.mark.spec\n\n"
        "@cites('NOPE-1')\ndef test_it() -> None: ...\n",
    )
    assert problems(root) == [
        "tests/test_example.py:3: use the spec marker as a direct call "
        "with literal IDs, so the trace can read it"
    ]


@pytest.mark.parametrize(
    "decorator",
    [
        "@pytest.mark.skip(reason='later')",
        "@pytest.mark.xfail",
        "@pytest.mark.skipif(True, reason='never')",
    ],
)
def test_skipped_test_does_not_cover_its_requirement(
    repo: Repo, decorator: str
) -> None:
    root = repo(
        "**CON-1.** It MUST work.\n",
        f"import pytest\n\nclass TestIt:\n    {decorator}\n"
        "    @pytest.mark.spec('CON-1')\n    def test_it(self) -> None: ...\n",
    )
    trace = spec_trace.check(root, ("01",))
    assert trace.citations[0].skipped
    assert trace.problems == [
        "01-contracts.md:1: CON-1 is must-level and no running test cites it"
    ]


def test_skip_in_pytestmark_reaches_the_module(repo: Repo) -> None:
    root = repo(
        "**CON-1.** It MUST work.\n",
        "import pytest\n\npytestmark = [pytest.mark.django_db, pytest.mark.skip]\n\n"
        "@pytest.mark.spec('CON-1')\ndef test_it() -> None: ...\n",
    )
    assert spec_trace.check(root).citations[0].skipped


def test_skip_on_the_class_reaches_its_tests(repo: Repo) -> None:
    root = repo(
        "**CON-1.** It MUST work.\n",
        "import pytest\n\n@pytest.mark.skip\nclass TestIt:\n"
        "    @pytest.mark.spec('CON-1')\n    def test_it(self) -> None: ...\n",
    )
    assert spec_trace.check(root).citations[0].skipped


# Enforcement and the settings file


SPEC = (
    "**CON-1.** It MUST work.\n\n**CON-2.** It SHOULD work.\n\n"
    "~~**CON-3.**~~ Withdrawn in spec 0.2: no longer needed.\n"
)


def test_enforce_needs_a_test_for_each_must_level_requirement(repo: Repo) -> None:
    root = repo(SPEC, transports="**TRN-1.** It MUST NOT fail.\n")
    assert problems(root) == []
    assert problems(root, ("01",)) == [
        "01-contracts.md:1: CON-1 is must-level and no running test cites it"
    ]
    assert problems(repo(SPEC, cites("CON-1")), ("01",)) == []


def test_enforced_documents_are_read_from_the_settings(repo: Repo) -> None:
    root = repo(SPEC, settings='enforce = ["01"]\n')
    assert problems(root) == [
        "01-contracts.md:1: CON-1 is must-level and no running test cites it"
    ]


def test_not_tested_entry_stands_in_for_a_test(repo: Repo) -> None:
    root = repo(
        SPEC,
        settings='enforce = ["01"]\n\n[not-tested]\nCON-1 = "Binds app code."\n',
    )
    trace = spec_trace.check(root)
    assert trace.problems == []
    assert trace.not_tested == {"CON-1": "Binds app code."}
    assert "not tested" in spec_trace.render(trace)


@pytest.mark.parametrize(
    ("rid", "reason", "tests", "problem"),
    [
        ("CON-1", "", "", "needs a reason"),
        ("CON-9", "x", "", "is not a live requirement"),
        ("CON-3", "x", "", "is not a live requirement"),
        ("CON-2", "x", "", "is not must-level, so it needs no entry"),
        ("CON-1", "x", cites("CON-1"), "is cited by a test; remove the entry"),
    ],
)
def test_not_tested_entries_are_checked(
    repo: Repo, rid: str, reason: str, tests: str, problem: str
) -> None:
    root = repo(SPEC, tests, settings=f'[not-tested]\n{rid} = "{reason}"\n')
    assert problems(root) == [f"tests/spec_trace.toml: [not-tested] {rid} {problem}"]


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
    out = capsys.readouterr().out
    assert "1 requirements, 0 cited by tests, 0 marked not tested, 0 problems" in out
    assert spec_trace.main(["--root", str(root), "--enforce", "01"]) == 1
    assert "problem: 01-contracts.md:1: CON-1" in capsys.readouterr().out
