"""The documentation site's staging, reference groups and HTML rules (ADR 0024).

Everything here but the build tests runs without Sphinx, on every supported
Python. The build tests need the ``docs`` dependency group (Python 3.13 or
newer) and are skipped where it is not installed.
"""

import ast
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from markdown_it import MarkdownIt

import neolith
from scripts import citations, docs

ROOT = Path(__file__).resolve().parents[1]
REPO = "https://github.com/stack314/django-neolith"
SHA = "0" * 40


def _links(problems: list[str] | None = None) -> docs.Links:
    links = docs.Links(ROOT, docs.mirror_table(ROOT), REPO, SHA)
    if problems is not None:
        links.problems = problems
    return links


# The reference groups


def test_the_groups_name_every_public_name_once() -> None:
    assert docs.group_problems(docs.public_names(neolith)[0]) == []
    grouped = [name for _, _, names in docs.API_GROUPS.values() for name in names]
    assert sorted(grouped) == sorted(set(neolith.__all__) - {"__version__"})


@pytest.mark.parametrize(
    ("public", "problem"),
    [
        pytest.param(
            [*docs.public_names(neolith)[0], "NewThing"],
            "NewThing is public and in no group",
            id="ungrouped",
        ),
        pytest.param(
            [n for n in docs.public_names(neolith)[0] if n != "connect"],
            "group apps names connect, which is not public",
            id="not-public",
        ),
    ],
)
def test_groups_and_public_names_that_disagree_are_refused(
    public: list[str], problem: str
) -> None:
    assert any(problem in said for said in docs.group_problems(public))


def test_a_name_in_two_groups_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    groups = dict(docs.API_GROUPS)
    title, intro, names = groups["errors"]
    groups["errors"] = (title, intro, (*names, "contract"))
    monkeypatch.setattr(docs, "API_GROUPS", groups)
    assert docs.group_problems(docs.public_names(neolith)[0]) == [
        "contract is in groups contracts and errors"
    ]


def test_each_group_page_documents_its_names_by_their_public_path() -> None:
    pages, problems = docs.api_pages(neolith)
    assert problems == []
    for slug, (title, _, names) in docs.API_GROUPS.items():
        page = pages[f"reference/api/{slug}.md"]
        assert page.startswith(f"# {title}\n")
        found = re.findall(r"^\.\. auto\w+:: (\S+)$", page, re.MULTILINE)
        assert found == [f"neolith.{name}" for name in names]
    assert (
        ".. autodata:: neolith.Binary\n   :no-value:" in pages["reference/api/forms.md"]
    )
    assert ".. autoexception:: neolith.UsageError" in pages["reference/api/errors.md"]
    assert (
        ".. autoclass:: neolith.Contract\n   :members:"
        in (pages["reference/api/contracts.md"])
    )


def test_the_requirement_index_lists_every_requirement_with_its_anchor() -> None:
    records = citations.Records(ROOT)
    page, problems = docs.requirements_page(ROOT / "spec", docs.mirror_table(ROOT))
    assert problems == []
    listed = re.findall(
        r"^\| \[([A-Z]+-\d+)\]\(\.\./spec/([\w-]+)\.md#([a-z]+-\d+)\)",
        page,
        re.MULTILINE,
    )
    assert {rid for rid, _, _ in listed} == records.live | records.withdrawn
    levels = dict(re.findall(r"^\| \[([A-Z]+-\d+)\].*\| (\w*) \|$", page, re.MULTILINE))
    for rid in records.withdrawn:
        assert levels[rid] == "Withdrawn", rid
    for rid in records.live:
        assert levels[rid] in ("MUST", "SHOULD", "MAY"), rid
    for rid, stem, anchor in listed:
        assert anchor == rid.lower()
        assert f"{stem}.md" == records.requirement_files[rid]


# Mirroring


def test_the_mirror_table_holds_every_spec_document_and_record() -> None:
    table = docs.mirror_table(ROOT)
    for path in (ROOT / "spec").glob("[0-9][0-9]-*.md"):
        assert table[f"spec/{path.name}"] == f"spec/{path.name}"
    for path in (ROOT / "docs" / "adr").glob("[0-9][0-9][0-9][0-9]-*.md"):
        assert table[f"docs/adr/{path.name}"] == f"adr/{path.name}"
    assert "docs/adr/template.md" not in table
    assert "CODE_OF_CONDUCT.md" not in table


def test_every_file_of_spec_conformance_is_mirrored_at_its_own_path() -> None:
    files = docs.mirror_files(ROOT)
    on_disk = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "spec" / "conformance").rglob("*")
        if path.is_file()
    )
    assert "spec/conformance/reference.v1.openapi.json" in on_disk
    assert list(files) == on_disk
    assert all(site == repo for repo, site in files.items())


def test_the_files_are_copied_byte_for_byte_and_only_json(tmp_path: Path) -> None:
    root, html = tmp_path / "root", tmp_path / "html"
    base = root / "spec" / "conformance"
    (base / "cases").mkdir(parents=True)
    # The nearest admitted files: escapes, non-ASCII text, an "@" that is no
    # address, a nested folder, and one name in several objects.
    admitted = {
        "spec/conformance/reference.v1.openapi.json": (
            b'{"a": "\\u00e9\xc3\xa9", "pattern": "^[^@]+$"}\n'
        ),
        # One member name in two objects is no repetition; the largest double
        # and exact integer, a surrogate pair and U+FFFD are I-JSON.
        "spec/conformance/cases/contract.json": (
            b'[{"a": 1}, {"a": {"a": 2}}, 1.7976931348623157e+308, -9007199254740991,'
            b' "\\ud83d\\ude00", "\xef\xbf\xbd"]'
        ),
    }
    for name, raw in admitted.items():
        (root / name).write_bytes(raw)
    refused = {
        "notes.md": (b"# Notes\n", "not a JSON file"),
        "broken.json": (b"{", "not I-JSON text in UTF-8"),
        "latin.json": (b'"\xe9"', "not I-JSON text in UTF-8"),
        "repeated.json": (
            b'{"by": "a\\u0040example.org", "by": "x"}',
            "not I-JSON text in UTF-8",
        ),
        "nested.json": (
            b'[{"a": 1, "b": {"c": 1, "c": 2}}]',
            "not I-JSON text in UTF-8",
        ),
        "nan.json": (b'{"n": NaN}', "not I-JSON text in UTF-8"),
        "infinity.json": (b"[-Infinity]", "not I-JSON text in UTF-8"),
        "huge.json": (b"[1e400]", "not I-JSON text in UTF-8"),
        "tiny.json": (b"[-1e400]", "not I-JSON text in UTF-8"),
        "inexact.json": (b"[9007199254740992]", "not I-JSON text in UTF-8"),
        "surrogate.json": (b'["\\ud800"]', "not I-JSON text in UTF-8"),
        "surrogate-key.json": (b'{"\\udfff": 1}', "not I-JSON text in UTF-8"),
        "noncharacter.json": ('["\uffff"]'.encode(), "not I-JSON text in UTF-8"),
        "noncharacter-escaped.json": (b'["\\ufdd0"]', "not I-JSON text in UTF-8"),
        "plane.json": ('{"k": "\U0010fffe"}'.encode(), "not I-JSON text in UTF-8"),
        "bom.json": (b"\xef\xbb\xbf[]", "not I-JSON text in UTF-8"),
        "mail.json": (b'{"by": "a@example.org"}', "holds an email address"),
        "escaped.json": (
            b'{"by": "a\\u0040example.org"}',
            "holds an email address",
        ),
        "key.json": (
            b'{"a\\u0040example.org": 1}',
            "holds an email address",
        ),
        "token.json": (
            b'["ghp_' + b"a" * 36 + b'"]',
            "holds something shaped like a secret",
        ),
        "escaped-token.json": (
            b'{"t": "gh\\u0070_' + b"a" * 36 + b'"}',
            "holds something shaped like a secret",
        ),
    }
    for name, (raw, _) in refused.items():
        (base / name).write_bytes(raw)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.json").write_text("{}", "utf-8")
    (base / "linked.json").symlink_to(outside / "secret.json")
    (base / "folder").symlink_to(outside)
    (base / "dangling.json").symlink_to(tmp_path / "missing.json")
    problems = docs.copy_files(root, html)
    expected = {
        f"spec/conformance/{name}: {why}; it is not copied"
        for name, (_, why) in refused.items()
    } | {
        f"spec/conformance/{name}: a symbolic link; it is not copied"
        for name in ("linked.json", "folder", "dangling.json")
    }
    assert sorted(problems) == sorted(expected)
    copied = sorted(
        path.relative_to(html).as_posix() for path in html.rglob("*") if path.is_file()
    )
    assert copied == sorted(admitted)
    for name, raw in admitted.items():
        assert (html / name).read_bytes() == raw


@pytest.mark.parametrize("linked", ["spec", "spec/conformance"])
def test_a_linked_folder_above_the_files_is_never_followed(
    tmp_path: Path, linked: str
) -> None:
    root, html, outside = tmp_path / "root", tmp_path / "html", tmp_path / "outside"
    (outside / "conformance").mkdir(parents=True)
    (outside / "conformance" / "secret.json").write_text("{}", "utf-8")
    (root / linked).parent.mkdir(parents=True, exist_ok=True)
    (root / linked).symlink_to(outside if linked == "spec" else outside / "conformance")
    assert docs.mirror_files(root) == {linked: linked}
    assert docs.copy_files(root, html) == [
        f"{linked}: a symbolic link; it is not copied"
    ]
    assert not html.exists()


def test_a_build_copies_the_files_of_spec_conformance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(argv: list[str]) -> int:
        Path(argv[-1]).mkdir(parents=True)
        return 0

    _fake_sphinx(monkeypatch, run)
    result = docs.build(ROOT, tmp_path)
    assert result.problems == ["inventory problem"]
    for repo_path, site_path in docs.mirror_files(ROOT).items():
        assert (result.path / site_path).read_bytes() == (ROOT / repo_path).read_bytes()


@pytest.mark.parametrize(
    ("source", "written", "staged"),
    [
        pytest.param(
            "docs/roadmap.md",
            "[0015](adr/0015-specification-first-django-reference-implementation.md)",
            "[0015](../adr/0015-specification-first-django-reference-implementation.md)",
            id="mirrored",
        ),
        pytest.param(
            "spec/00-overview.md",
            "[02](02-transports.md#routing)",
            "[02](02-transports.md#routing)",
            id="mirrored-same-folder-with-fragment",
        ),
        pytest.param(
            "README.md",
            "[records](docs/adr/)",
            "[records](adr/index.md)",
            id="folder-to-its-readme",
        ),
        pytest.param(
            "CONTRIBUTING.md",
            "[License](LICENSE)",
            f"[License]({REPO}/blob/{SHA}/LICENSE)",
            id="unmirrored-file",
        ),
        pytest.param(
            "CONTRIBUTING.md",
            "[tests](tests)",
            f"[tests]({REPO}/tree/{SHA}/tests)",
            id="unmirrored-folder",
        ),
        pytest.param(
            "CONTRIBUTING.md",
            '[License](<LICENSE> "the licence")',
            f'[License](<{REPO}/blob/{SHA}/LICENSE> "the licence")',
            id="angle-brackets-and-title",
        ),
        pytest.param(
            "CONTRIBUTING.md",
            "[vision][v]\n\n[v]: VISION.md",
            "[vision][v]\n\n[v]: vision.md",
            id="reference-definition",
        ),
        pytest.param(
            "CONTRIBUTING.md",
            "[x](https://example.com/VISION.md) [y](#terms) [z](mailto:a)",
            "[x](https://example.com/VISION.md) [y](#terms) [z](mailto:a)",
            id="external-and-fragment-unchanged",
        ),
        pytest.param(
            "CONTRIBUTING.md",
            "`[a](VISION.md)` and [b](VISION.md)",
            "`[a](VISION.md)` and [b](vision.md)",
            id="code-span-unchanged",
        ),
        pytest.param(
            "CONTRIBUTING.md",
            "```\n[a](VISION.md)\n```\n\n[b](VISION.md)",
            "```\n[a](VISION.md)\n```\n\n[b](vision.md)",
            id="fence-unchanged",
        ),
        pytest.param(
            "CONTRIBUTING.md",
            "[License](LICENSE#l3)",
            f"[License]({REPO}/blob/{SHA}/LICENSE#l3)",
            id="unmirrored-with-fragment",
        ),
        pytest.param(
            "CONTRIBUTING.md",
            "[vision](VISION%2Emd)",
            "[vision](vision.md)",
            id="percent-encoded",
        ),
    ],
)
def test_links_lead_to_their_staged_targets_and_nothing_else_changes(
    source: str, written: str, staged: str
) -> None:
    problems: list[str] = []
    table = docs.mirror_table(ROOT)
    table.setdefault(source, "page.md")
    links = docs.Links(ROOT, table, REPO, SHA, problems)
    assert docs.rewrite_links(written, source, links) == staged
    assert problems == []
    assert docs.same_text(written, staged) is None


@pytest.mark.parametrize(
    ("written", "problem"),
    [
        ("[x](NOWHERE.md)", "links to NOWHERE.md, which does not exist"),
        ("[x](../outside.md)", "links to ../outside.md, outside the repository"),
        ("[x](/VISION.md)", "links to /VISION.md, an absolute path"),
    ],
)
def test_a_link_that_leads_nowhere_is_refused(written: str, problem: str) -> None:
    problems: list[str] = []
    docs.rewrite_links(written, "CONTRIBUTING.md", _links(problems))
    assert problems == [f"CONTRIBUTING.md: {problem}"]


@pytest.mark.parametrize(
    "staged",
    [
        pytest.param("See the [vision](VISION.md), now.", id="word"),
        pytest.param("See the vision, now.", id="link-dropped"),
        pytest.param('See the [vision](VISION.md "t"), now', id="title"),
        pytest.param("See the [vision](VISION.md), now.\n\n`x`", id="appended"),
        pytest.param("See the *vision*(VISION.md), now", id="markup"),
    ],
)
def test_a_change_beyond_a_link_target_is_refused(staged: str) -> None:
    assert docs.same_text("See the [vision](VISION.md), now", staged) is not None


@pytest.mark.parametrize(
    ("original", "staged"),
    [
        pytest.param("a\n\nb\n", "a\n\n\nb\n", id="lines-moved"),
        pytest.param(
            "[t][a]\n\n[a]: x\n[b]: y\n", "[t][a]\n\n[a]: x\n[c]: y\n", id="definition"
        ),
    ],
)
def test_a_change_markdown_shows_no_text_for_is_refused(
    original: str, staged: str
) -> None:
    assert docs.same_text(original, staged) is not None


def _mirrored(
    monkeypatch: pytest.MonkeyPatch, root: Path, text: str
) -> tuple[dict[str, str], list[str]]:
    """Mirror ``text`` as CONTRIBUTING.md, beside a VISION.md, under ``root``.

    It goes through ``docs._mirror``, the step staging runs, so what is
    checked is the proof staging reports. The commit is pinned, so that the
    links are built with one wherever the tests run, not ``main``.
    """
    monkeypatch.setattr(docs, "commit", lambda root: SHA)
    (root / "pyproject.toml").write_text(
        f'[project]\nurls = {{Repository = "{REPO}"}}\n', encoding="utf-8"
    )
    (root / "CONTRIBUTING.md").write_text(text, encoding="utf-8")
    (root / "VISION.md").write_text("# Vision\n", encoding="utf-8")
    table = {
        "CONTRIBUTING.md": "project/contributing.md",
        "VISION.md": "project/vision.md",
    }
    problems: list[str] = []
    pages = docs._mirror(root, table, problems)
    return pages, problems


@pytest.mark.parametrize(
    ("text", "refusal"),
    [
        pytest.param(
            "~~~\nx = '`'\n~~~\n\n[x](VISION.md)\n\nUse `y`.\n",
            "'VISION.md' staged as 'VISION.md', expected 'vision.md'",
            id="backticks-across-blocks",
        ),
        pytest.param(
            "One ` here.\n\n[y](nothere.md)\n\nAnd ` there.\n",
            "CONTRIBUTING.md: links to nothere.md, which does not exist",
            id="missing-link-between-backticks",
        ),
        pytest.param(
            "> [v]: VISION.md\n\nSee [the vision][v].\n",
            "expected 'vision.md'",
            id="definition-in-a-quote",
        ),
        pytest.param(
            "See [the vision](\nVISION.md).\n",
            "expected 'vision.md'",
            id="destination-on-the-next-line",
        ),
    ],
)
def test_a_link_the_rewrite_misses_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str, refusal: str
) -> None:
    # Each input is one where a regex of the rewrite reads the Markdown
    # differently from markdown-it-py; the proof refuses the result, and
    # staging reports it once.
    _, problems = _mirrored(monkeypatch, tmp_path, text)
    assert len([problem for problem in problems if refusal in problem]) == 1, problems
    assert len(problems) == len(set(problems)), problems


@pytest.mark.parametrize(
    ("link", "problem"),
    [
        pytest.param("[y](nothere.md)", "which does not exist", id="missing"),
        pytest.param("[y](/VISION.md)", "an absolute path", id="absolute"),
        pytest.param("[y](../VISION.md)", "outside the repository", id="outside"),
        pytest.param("[y][v]\n\n[v]: nothere.md", "which does not exist", id="ref"),
    ],
)
def test_a_bad_link_the_rewrite_finds_is_reported_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, link: str, problem: str
) -> None:
    # The rewrite and the proof both read this link; only the proof's
    # problems are reported, so staging names it once, not once for each.
    _, problems = _mirrored(monkeypatch, tmp_path, f"See {link}\n")
    assert len(problems) == 1, problems
    assert problems[0].startswith("CONTRIBUTING.md: links to "), problems
    assert problems[0].endswith(problem), problems


@pytest.mark.parametrize(
    "link",
    [
        pytest.param("[x](<docs/site/a b.txt>)", id="angle-brackets"),
        pytest.param("[x](docs/site/a%20b.txt)", id="percent-encoded"),
        pytest.param("[x][a]\n\n[a]: <docs/site/a b.txt>", id="definition"),
    ],
)
def test_a_link_to_an_unmirrored_file_with_a_blank_is_proved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, link: str
) -> None:
    (tmp_path / "docs" / "site").mkdir(parents=True)
    (tmp_path / "docs" / "site" / "a b.txt").write_text("x", encoding="utf-8")
    pages, problems = _mirrored(monkeypatch, tmp_path, f"See {link}\n")
    assert problems == []
    blob = f"<{REPO}/blob/{SHA}/docs/site/a b.txt>"
    assert blob in pages["project/contributing.md"]


def test_every_mirrored_target_is_proved() -> None:
    problems: list[str] = []
    pages = docs._mirror(ROOT, docs.mirror_table(ROOT), problems)
    assert problems == []
    assert set(pages) == set(docs.mirror_table(ROOT).values())


def test_a_target_with_a_blank_is_written_in_angle_brackets(tmp_path: Path) -> None:
    (tmp_path / "a b.txt").write_text("x", "utf-8")
    problems: list[str] = []
    links = docs.Links(tmp_path, {"README.md": "page.md"}, REPO, SHA, problems)
    staged = docs.rewrite_links("[x](a%20b.txt)\n", "README.md", links)
    assert staged == f"[x](<{REPO}/blob/{SHA}/a b.txt>)\n"
    assert problems == []
    assert docs.same_text("[x](a%20b.txt)\n", staged) is None


def test_a_change_of_link_target_alone_is_admitted() -> None:
    original = "See the [vision](VISION.md), now"
    assert docs.same_text(original, "See the [vision](other.md), now") is None


_WORDS = st.sampled_from(
    ["a", "b c", "*x*", "_y_", "(p)", "[q]", "\\(", "\\[", "<", ">", "!", "#"]
)
_DESTINATIONS = st.sampled_from(
    [
        "VISION.md",
        "spec/01-contracts.md#con-1",
        "docs/adr/",
        "docs/adr/0001-record-architecture-decisions.md",
        "LICENSE",
        "tests",
        "https://example.com/x",
        "#terms",
    ]
)


@st.composite
def _markdown(draw: st.DrawFn) -> str:
    pieces = []
    definitions = []
    for index in range(draw(st.integers(1, 6))):
        pieces.append(draw(_WORDS))
        destination = draw(_DESTINATIONS)
        form = draw(st.integers(0, 6))
        if form == 0:
            pieces.append(f"[t{index}]({destination})")
        elif form == 1:
            pieces.append(f'[t{index}]({destination} "title")')
        elif form == 2:
            pieces.append(f"[t{index}](<{destination}>)")
        elif form == 3:
            pieces.append(f"![i{index}]({destination})")
        elif form == 4:
            pieces.append(f"[t{index}][r{index}]")
            definitions.append(f"[r{index}]: {destination}")
        elif form == 5:
            pieces.append(f"`[c{index}]({destination})`")
        else:
            pieces.append(f"*[t{index}]({destination})*")
    text = " ".join(pieces)
    return text + ("\n\n" + "\n".join(definitions) if definitions else "") + "\n"


@settings(max_examples=300, deadline=None)
@given(_markdown())
def test_staging_rewrites_every_link_and_only_links(text: str) -> None:
    problems: list[str] = []
    links = _links(problems)
    staged = docs.rewrite_links(text, "CONTRIBUTING.md", links)
    assert problems == []
    proof = _links([])
    assert (
        docs.same_text(text, staged, lambda t: proof.target(t, "CONTRIBUTING.md"))
        is None
    )
    markdown = MarkdownIt("commonmark").enable(["table", "strikethrough"])
    before = list(docs._destinations(markdown.parse(text)))
    after = list(docs._destinations(markdown.parse(staged)))
    expected = [
        markdown.normalizeLink(links.target(href, "CONTRIBUTING.md")) for href in before
    ]
    assert after == expected


def test_every_mirrored_document_stages_word_for_word() -> None:
    links = _links()
    for repo_path in docs.mirror_table(ROOT):
        original = (ROOT / repo_path).read_text(encoding="utf-8")
        staged = docs.rewrite_links(original, repo_path, links)
        assert docs.same_text(original, staged) is None, repo_path
    assert links.problems == []


@pytest.mark.parametrize(
    "text",
    ["write to someone@example.org", "a.b+c@mail.example.co.uk is here"],
)
def test_an_email_address_is_found(text: str) -> None:
    assert docs.email_lines("first\n" + text) == [2]


@pytest.mark.parametrize(
    "text", ['@contract("inventory.v1")', "a @ b.c", "user@localhost"]
)
def test_a_decorator_or_bare_at_sign_is_not_an_email_address(text: str) -> None:
    assert docs.email_lines(text) == []


def _copy_root(tmp_path: Path) -> Path:
    """A copy of the files staging reads, to break one at a time."""
    root = tmp_path / "repo"
    skip = shutil.ignore_patterns("__pycache__", "*.pyc")
    for name in ("src", "spec", "docs"):
        shutil.copytree(ROOT / name, root / name, ignore=skip)
    for name in (
        "VISION.md",
        "CONTRIBUTING.md",
        "CHANGELOG.md",
        "CODE_OF_CONDUCT.md",
        "pyproject.toml",
        "LICENSE",
    ):
        if (ROOT / name).is_dir():
            shutil.copytree(ROOT / name, root / name, ignore=skip)
        else:
            shutil.copy(ROOT / name, root / name)
    return root


def _append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as file:
        file.write(text)


def _break_encoding(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with (root / "CHANGELOG.md").open("ab") as file:
        file.write(b"\n\xe9\n")


def _break_link(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _append(root / "VISION.md", "\nSee [this](NOWHERE.md).\n")


def _break_text(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rewrite = docs.rewrite_links

    def changed(text: str, source: str, links: docs.Links) -> str:
        staged = rewrite(text, source, links)
        return staged + "\nAdded.\n" if source == "CHANGELOG.md" else staged

    monkeypatch.setattr(docs, "rewrite_links", changed)


def _break_group(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    title, intro, names = docs.API_GROUPS["errors"]
    monkeypatch.setitem(docs.API_GROUPS, "errors", (title, intro, (*names, "Bogus")))


def _break_name(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    title, intro, names = docs.API_GROUPS["errors"]
    monkeypatch.setitem(docs.API_GROUPS, "errors", (title, intro, (*names, "Ghost")))
    monkeypatch.setattr(neolith, "__all__", [*neolith.__all__, "Ghost"])


def _break_all(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(neolith, "__all__", "contract")


def _break_spec(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _append(root / "spec" / "01-contracts.md", "\n**CON-1.** A second thing MUST be.\n")


def _break_index(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fails(spec_dir: Path, table: object) -> tuple[str, list[str]]:
        raise RuntimeError("index")

    monkeypatch.setattr(docs, "requirements_page", fails)


def _break_api(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fails(package: object) -> tuple[dict[str, str], list[str]]:
        raise RuntimeError("reference")

    monkeypatch.setattr(docs, "api_pages", fails)


_STAGE_BREAKS = [
    (_break_encoding, "CHANGELOG.md: cannot be staged: UnicodeDecodeError"),
    (_break_link, "VISION.md: links to NOWHERE.md, which does not exist"),
    (_break_text, "CHANGELOG.md: staging changed more than links"),
    (_break_group, "group errors names Bogus, which is not public"),
    (_break_name, "neolith.Ghost: cannot be documented: AttributeError"),
    (_break_all, "neolith.__all__ is not a list of names"),
    (_break_spec, "CON-1 is already defined"),
    (_break_index, "the requirement index cannot be generated: RuntimeError('index')"),
    (_break_api, "the API reference cannot be generated: RuntimeError('reference')"),
]


@pytest.mark.parametrize(
    ("brake", "problem"), _STAGE_BREAKS, ids=[b.__name__ for b, _ in _STAGE_BREAKS]
)
def test_staging_reports_each_problem_with_every_other(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    brake: object,
    problem: str,
) -> None:
    root = _copy_root(tmp_path)
    _append(
        root / "docs" / "site" / "concepts" / "index.md", "\nWrite to a@example.org.\n"
    )
    assert callable(brake)
    brake(root, monkeypatch)
    result = docs.stage(root, tmp_path / "out", {"index.md/x.md": "# X\n"})
    said = "\n".join(result.problems)
    assert problem in said, said
    assert "concepts/index.md: an email address on line" in said, said
    assert "index.md/x.md: cannot be written" in said, said
    assert not (result.path / "concepts" / "index.md").exists()
    assert (result.path / "project" / "vision.md").is_file()


def test_a_group_documents_only_public_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    title, intro, names = docs.API_GROUPS["errors"]
    monkeypatch.setitem(
        docs.API_GROUPS, "errors", (title, intro, (*names, "__version__"))
    )
    result = docs.stage(ROOT, tmp_path)
    errors = (result.path / "reference" / "api" / "errors.md").read_text("utf-8")
    assert "__version__" not in errors
    assert "group errors names __version__, which is not public" in result.problems


def test_staging_leaves_out_only_the_bad_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _copy_root(tmp_path)
    _break_text(root, monkeypatch)
    _break_group(root, monkeypatch)
    result = docs.stage(root, tmp_path / "out")
    assert not (result.path / "project" / "changelog.md").exists()
    assert (result.path / "reference" / "requirements.md").is_file()
    errors = (result.path / "reference" / "api" / "errors.md").read_text("utf-8")
    assert "Bogus" not in errors
    assert "neolith.UsageError" in errors


@pytest.mark.parametrize(
    ("where", "problem"),
    [
        pytest.param(lambda root, tmp: root, "holds the repository", id="root"),
        pytest.param(
            lambda root, tmp: root.parent, "holds the repository", id="parent"
        ),
        pytest.param(lambda root, tmp: root / "src", "not under build/", id="src"),
        pytest.param(
            lambda root, tmp: root / "docs" / "x", "not under build/", id="docs"
        ),
        pytest.param(
            lambda root, tmp: tmp / "busy", "not empty and not a docs build", id="busy"
        ),
    ],
)
def test_staging_refuses_an_unsafe_place_to_build(
    tmp_path: Path, where: object, problem: str
) -> None:
    root = _copy_root(tmp_path)
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "notes.txt").write_text("keep", "utf-8")
    assert callable(where)
    with pytest.raises(docs.DocsError, match=problem):
        docs.stage(root, where(root, tmp_path))
    assert (root / "src" / "neolith" / "__init__.py").is_file()
    assert (root / "docs" / "site" / "index.md").is_file()
    assert (tmp_path / "busy" / "notes.txt").is_file()


def test_staging_builds_into_an_empty_place_a_marked_one_or_build(
    tmp_path: Path,
) -> None:
    root = _copy_root(tmp_path)
    for out in (tmp_path / "new", tmp_path / "new", root / "build" / "docs"):
        assert docs.stage(root, out).problems == []
        assert (out / docs.MARKER).exists()


def test_staging_writes_every_mirrored_and_generated_page(tmp_path: Path) -> None:
    result = docs.stage(ROOT, tmp_path)
    assert result.problems == []
    source = result.path
    for staged in docs.mirror_table(ROOT).values():
        assert (source / staged).is_file(), staged
    for slug in docs.API_GROUPS:
        assert (source / "reference" / "api" / f"{slug}.md").is_file()
    assert (source / "reference" / "requirements.md").is_file()
    assert (source / "index.md").is_file()
    assert not (source / "conf.py").exists()


def test_the_version_decides_a_development_build(tmp_path: Path) -> None:
    assert docs.development(ROOT) is (".dev" in neolith.__version__)
    package = tmp_path / "src" / "neolith"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    assert docs.development(tmp_path) is False
    (package / "__init__.py").write_text('__version__ = "0.2.0.dev0"\n', "utf-8")
    assert docs.development(tmp_path) is True


def test_an_inventory_is_cached_by_its_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = docs.inventory_file("django", tmp_path)
    monkeypatch.setitem(
        docs.INVENTORIES,
        "django",
        ("https://docs.djangoproject.com/en/6.0/", "_objects/"),
    )
    assert docs.inventory_file("django", tmp_path) != before


def test_an_inventory_that_cannot_be_fetched_is_a_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(docs, "INVENTORIES", {"nowhere": ("http://127.0.0.1:9/", "x")})
    assert docs.fetch_inventories(tmp_path)[0].startswith(
        "cannot fetch the nowhere inventory from http://127.0.0.1:9/x"
    )
    docs.inventory_file("nowhere", tmp_path).write_bytes(b"cached")
    assert docs.fetch_inventories(tmp_path) == []


# Citations


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("as CON-30 says", [("requirement", "CON-30", "CON-30")]),
        (
            "CON-30 and 31",
            [("requirement", "CON-30", "CON-30"), ("requirement", "CON-31", "31")],
        ),
        (
            "CON-30 through CON-33",
            [
                ("requirement", "CON-30", "CON-30"),
                ("requirement", "CON-33", "CON-33"),
            ],
        ),
        ("ADRs 0017 and 0019", [("ADR", "0017", "ADRs 0017"), ("ADR", "0019", "0019")]),
        ("spec 01", [("spec document", "01", "spec 01")]),
        ("spec 0.1", []),
    ],
)
def test_each_citation_spans_the_words_that_name_it(
    text: str, found: list[tuple[str, str, str]]
) -> None:
    assert [
        (c.kind, c.key, text[c.start : c.end]) for c in citations.find(text)
    ] == found


# The HTML rules

_GOOD_TEXT = "Contracts are declared on a Protocol class and checked at import."
_ROBOTS = '<meta name="robots" content="noindex">'
_BANNER = f'<div class="bd-header-announcement"><div>{docs.BANNER}</div></div>'
_FOOTER = f"<footer><p>{docs.FOOTER}</p></footer>"
_PLANNED_ID = "neolith-planned-0123"


def _page(
    main: str = _GOOD_TEXT,
    *,
    head: str = _ROBOTS,
    banner: str = _BANNER,
    footer: str = _FOOTER,
    h1: str = "<h1>Title</h1>",
    body: str = "",
) -> str:
    return (
        f"<html><head>{head}</head><body>{banner}{body}"
        f"<main>{h1}<p>{main}</p></main>{footer}</body></html>"
    )


def _site(tmp_path: Path, pages: dict[str, str]) -> Path:
    html = tmp_path / "html"
    for path, text in {"index.html": _page(), **pages}.items():
        (html / path).parent.mkdir(parents=True, exist_ok=True)
        (html / path).write_text(text, encoding="utf-8")
    return html


def _found(
    tmp_path: Path,
    page: str,
    path: str = "concepts/index.html",
    others: dict[str, str] | None = None,
) -> list[str]:
    html = _site(tmp_path, {path: page, **(others or {})})
    return docs.check_html(html, ROOT, planned={path: [_PLANNED_ID]})


_SPEC = {
    "spec/01-contracts/index.html": _page(
        '<strong id="con-21">CON-21.</strong> ' + "The rule this page holds, withdrawn."
    )
}


def test_a_good_page_passes(tmp_path: Path) -> None:
    page = _page(
        _GOOD_TEXT + '<a href="../">home</a> <a href="#x" id="x">here</a>'
        ' <a href="https://docs.python.org/3/">Python</a>'
        '<svg xmlns="http://www.w3.org/2000/svg"></svg>'
    )
    assert _found(tmp_path, page) == []


def _person() -> str:
    people = docs.people(ROOT)
    assert people
    return people[0]


def _planned(text: str) -> str:
    return f'<div class="admonition planned" id="{_PLANNED_ID}"><p>{text}</p></div>'


_LINK = '<a href="{}">x</a>' + _GOOD_TEXT

_REFUSED = [
    # Every page.
    ("footer", _page(footer=""), "no footer"),
    ("footer-outside", _page(_GOOD_TEXT + docs.FOOTER, footer=""), "no footer"),
    ("noindex", _page(head=""), "no noindex on a development build"),
    (
        "noindex-other-meta",
        _page(head='<meta name="description" content="noindex">'),
        "no noindex",
    ),
    ("banner", _page(banner=""), "no development banner"),
    (
        "banner-outside",
        _page(_GOOD_TEXT + docs.BANNER, banner=""),
        "no development banner",
    ),
    ("email", _page("write to someone@example.org"), "the email address"),
    ("email-entity", _page("write to someone&#64;example.org"), "the email address"),
    (
        "email-cloaked",
        _page(
            '<a href="mailto:someone&#37;&#52;&#48;example&#46;org">someone'
            "<span>&#64;</span>example<span>&#46;</span>org</a>"
        ),
        "the email address someone@example.org",
    ),
    ("mailto", _page(_LINK.format("mailto:x")), "links to mailto:x"),
    ("secret-github", _page("token ghp_" + "a" * 36), "shaped like a secret"),
    ("secret-aws", _page("key AKIA" + "A" * 16), "shaped like a secret"),
    ("secret-slack", _page("xoxb-" + "1" * 12), "shaped like a secret"),
    ("secret-sk", _page("sk-" + "a" * 24), "shaped like a secret"),
    ("secret-pypi", _page("pypi-" + "A" * 60), "shaped like a secret"),
    ("secret-google", _page("AIza" + "B" * 35), "shaped like a secret"),
    (
        "secret-discord",
        _page("M" + "a" * 25 + ".abcdef." + "b" * 30),
        "shaped like a secret",
    ),
    (
        "secret-private-key",
        _page("-----BEGIN RSA " + "PRIVATE KEY-----"),
        "shaped like a secret",
    ),
    (
        "secret-slack-hook",
        _page("https://hooks.slack.com/services/" + "T" * 9),
        "shaped like a secret",
    ),
    (
        "secret-deploy-hook",
        _page(
            "https://api.cloudflare.com/client/v4/workers/builds/deploy_hooks/"
            + "a" * 20
        ),
        "shaped like a secret",
    ),
    (
        "secret-assigned",
        _page("api_key = 'Q2xhdWRlIGlzIGEgY2F0'"),
        "shaped like a secret",
    ),
    (
        "secret-entropy",
        _page("value 7fK2pQ9xLmZ4vB8nR1tY6wE3sA5dG0hJ"),
        "shaped like a secret",
    ),
    ("secret-entity", _page("token gh&#112;_" + "a" * 36), "shaped like a secret"),
    (
        "foreign-script",
        _page(head=_ROBOTS + '<script src="https://cdn.example.com/x.js"></script>'),
        "loads https://cdn.example.com/x.js from another origin",
    ),
    (
        "foreign-video",
        _page(body='<video src="https://x.example.com/v.mp4"></video>'),
        "from another origin",
    ),
    (
        "foreign-object",
        _page(body='<object data="//x.example.com/o"></object>'),
        "from another origin",
    ),
    (
        "foreign-embed",
        _page(body='<embed src="https://x.example.com/e">'),
        "from another origin",
    ),
    (
        "foreign-srcset",
        _page(body='<img src="a.png" srcset="https://x.example.com/b.png 2x">'),
        "from another origin",
    ),
    (
        "foreign-any-attribute",
        _page(body='<div data-src="https://x.example.com/p.png"></div>'),
        "from another origin",
    ),
    (
        "foreign-style-import",
        _page(
            head=_ROBOTS + '<style>@import url("https://x.example.com/a.css");</style>'
        ),
        "its CSS loads from another origin",
    ),
    (
        "foreign-style-import-string",
        _page(head=_ROBOTS + '<style>@import "https://x.example.com/a.css";</style>'),
        "its CSS loads from another origin",
    ),
    (
        "foreign-image-set",
        _page(
            body="<div style='background: image-set("
            '"https://x.example.com/a.png" 1x)\'></div>'
        ),
        "its CSS loads from another origin",
    ),
    (
        "foreign-style-attribute",
        _page(body='<div style="background: url(//x.example.com/a.png)"></div>'),
        "its CSS loads from another origin",
    ),
    (
        "missing-page",
        _page(_LINK.format("../missing/")),
        "../missing/, which does not exist",
    ),
    (
        "missing-html",
        _page(_LINK.format("nope.html")),
        "nope.html, which does not exist",
    ),
    (
        "missing-file",
        _page('<img src="pic.png">' + _GOOD_TEXT),
        "pic.png, which does not exist",
    ),
    (
        "missing-stylesheet",
        _page(head=_ROBOTS + '<link rel="stylesheet" href="x.css">'),
        "x.css, which does not exist",
    ),
    ("missing-anchor", _page(_LINK.format("../#nowhere")), "an anchor the page lacks"),
    ("absolute-link", _page(_LINK.format("/concepts/")), "an absolute path"),
    ("outside-link", _page(_LINK.format("../../x/")), "outside the site"),
    ("no-heading", _page(h1=""), "<main> has no heading"),
    ("no-text", _page(""), "<main> has no text"),
    (
        "script-is-not-text",
        _page("<script>" + _GOOD_TEXT + "</script>"),
        "<main> has no text",
    ),
    # Pages the site writes: every rule reads the whole page as a reader gets it.
    (
        "private",
        _page("See neolith._forms for the helpers of each form."),
        "names a private module",
    ),
    (
        "private-title",
        _page(body='<a title="neolith._host.Connector" href="#">x</a>'),
        "names a private module",
    ),
    (
        "private-href",
        _page(_LINK.format("../neolith._host/")),
        "names a private module",
    ),
    (
        "private-highlighted",
        _page(
            '<pre><span class="n">neolith</span><span class="o">.</span>'
            '<span class="n">_host</span></pre>'
        ),
        "names a private module",
    ),
    (
        "private-emphasis",
        _page("neolith.<em>_host</em> holds it."),
        "names a private module",
    ),
    ("private-entity", _page("neolith&#46;_host holds it."), "names a private module"),
    (
        "private-any-attribute",
        _page(body='<div data-name="neolith._host"></div>'),
        "names a private module",
    ),
    (
        "email-in-link-only",
        _page(_LINK.format("mailto:someone%40example.org")),
        "the email address someone@example.org",
    ),
    (
        "official",
        _page("The official way to declare a contract is shown."),
        "says official",
    ),
    (
        "official-title",
        _page(head=_ROBOTS + "<title>The official docs</title>"),
        "says official",
    ),
    (
        "official-footer",
        _page(footer=f"<footer><p>{docs.FOOTER} Official.</p></footer>"),
        "says official",
    ),
    (
        "official-alt",
        _page(body='<img src="index.html" alt="the official logo">'),
        "says official",
    ),
    (
        "official-after-main",
        _page("</main><p>The official one.</p><main>"),
        "says official",
    ),
    (
        "how-written",
        _page("This page was written with Claude for the reader."),
        "says how the code is written",
    ),
    (
        "how-written-lower",
        _page("this page is ai-generated by an llm, then read."),
        "says how the code is written",
    ),
    (
        "how-written-agent",
        _page("This text was drafted by an assistant, then read."),
        "says how the code is written",
    ),
    (
        "install-pip",
        _page("Install it with pip install django-neolith today."),
        "shows an install command",
    ),
    (
        "install-pip-flag",
        _page("Run pip -q install django-neolith to get it."),
        "shows an install command",
    ),
    (
        "install-pip-version",
        _page("Run pip3.13 install django-neolith to get it."),
        "shows an install command",
    ),
    (
        "install-uv-tool",
        _page("Run uv tool install django-neolith to get it."),
        "shows an install command",
    ),
    (
        "install-rye",
        _page("Run rye add django-neolith in the project."),
        "shows an install command",
    ),
    (
        "install-easy",
        _page("Run easy_install django-neolith in the shell."),
        "shows an install command",
    ),
    (
        "install-pin",
        _page("Require django-neolith==0.1 in the requirements file."),
        "shows an install command",
    ),
    (
        "install-range",
        _page("Require django-neolith>=0.1 in the requirements file."),
        "shows an install command",
    ),
    (
        "install-compatible",
        _page("Require django-neolith~=0.1 in the requirements."),
        "shows an install command",
    ),
    (
        "planned",
        _page("The HTTP transport will carry calls to a detached app."),
        "describes planned behaviour",
    ),
    (
        "planned-not-yet",
        _page("The HTTP transport is not implemented yet, sadly."),
        "describes planned behaviour",
    ),
    (
        "planned-coming",
        _page("The HTTP transport is coming in step 5 of the plan."),
        "describes planned behaviour",
    ),
    (
        "planned-plan",
        _page("We plan to add an HTTP transport after this one."),
        "describes planned behaviour",
    ),
    (
        "planned-step",
        _page("Step 5 adds the HTTP transport to the host."),
        "describes planned behaviour",
    ),
    (
        "planned-scheduled",
        _page("The HTTP transport is scheduled for step 5 now."),
        "describes planned behaviour",
    ),
    (
        "planned-lookalike",
        _page(_GOOD_TEXT + '<div class="admonition planned">It will ship.</div>'),
        "describes planned behaviour",
    ),
    (
        "planned-upper-case-mark",
        _page(_GOOD_TEXT + '<div DATA-NEOLITH-PLANNED="5">It will ship.</div>'),
        "describes planned behaviour",
    ),
    (
        "person",
        _page(f"Thanks to {_person()} for the design of this page."),
        "names a person",
    ),
    (
        "person-lower-case",
        _page(f"Thanks to {_person().lower()} for the design of it."),
        "names a person",
    ),
    (
        "person-title",
        _page(body=f'<abbr title="by {_person()}">x</abbr>'),
        "names a person",
    ),
    (
        "citation-unknown",
        _page("As CON-9999 says, it holds for a call."),
        "cites unknown requirement CON-9999",
    ),
    (
        "citation-withdrawn",
        _page("As CON-21 says, it holds for a call."),
        "cites withdrawn requirement CON-21",
    ),
    (
        "citation-withdrawn-anchor",
        _page(_LINK.format("../spec/01-contracts/#con-21")),
        "links to withdrawn requirement CON-21",
    ),
]


@pytest.mark.parametrize(
    ("page", "problem"), [p[1:] for p in _REFUSED], ids=[p[0] for p in _REFUSED]
)
def test_each_rule_refuses_its_page(tmp_path: Path, page: str, problem: str) -> None:
    found = _found(tmp_path, page, others=_SPEC)
    assert any(problem in said for said in found), found


@pytest.mark.parametrize(
    ("words", "problem"),
    [
        ("The official HTTP transport is described.", "says official"),
        ("Install it with pip install django-neolith.", "shows an install command"),
        (
            "This block was written with Claude, then read.",
            "says how the code is written",
        ),
        ("Thanks to {person} for this block of text.", "names a person"),
        ("As CON-9999 says, it holds.", "cites unknown requirement"),
        ("See neolith._host for it.", "names a private module"),
    ],
)
def test_a_planned_block_exempts_only_the_planned_words_rule(
    tmp_path: Path, words: str, problem: str
) -> None:
    block = _planned("It will carry calls. " + words.format(person=_person()))
    found = _found(tmp_path, _page(_GOOD_TEXT + block))
    assert any(problem in said for said in found), found
    assert not any("planned behaviour" in said for said in found), found


def test_planned_words_inside_a_recorded_planned_block_pass(tmp_path: Path) -> None:
    block = _planned("The HTTP transport will carry calls to a detached app.")
    assert _found(tmp_path, _page(_GOOD_TEXT + block)) == []


def test_a_planned_block_the_extension_did_not_record_is_not_exempt(
    tmp_path: Path,
) -> None:
    block = _planned("The HTTP transport will carry calls to a detached app.")
    html = _site(tmp_path, {"concepts/index.html": _page(_GOOD_TEXT + block)})
    found = docs.check_html(html, ROOT, planned={})
    assert any("planned behaviour" in said for said in found), found


def test_the_planned_blocks_are_read_from_the_record_beside_the_html(
    tmp_path: Path,
) -> None:
    block = _planned("The HTTP transport will carry calls to a detached app.")
    html = _site(tmp_path, {"concepts/index.html": _page(_GOOD_TEXT + block)})
    record = html.parent / docs.PLANNED_RECORD
    assert any("planned behaviour" in said for said in docs.check_html(html, ROOT))
    record.write_text(json.dumps({"concepts/index.html": [_PLANNED_ID]}), "utf-8")
    assert docs.check_html(html, ROOT) == []
    record.write_text("{", "utf-8")
    assert any(
        f"{docs.PLANNED_RECORD} cannot be read" in said
        for said in docs.check_html(html, ROOT)
    )


def test_every_match_of_a_rule_is_reported(tmp_path: Path) -> None:
    found = _found(
        tmp_path, _page("The official way, officially. It will be, and is planned.")
    )
    assert sum("says official" in said for said in found) == 2, found
    assert sum("planned behaviour" in said for said in found) == 2, found


def test_a_file_that_cannot_be_read_is_a_problem_and_the_rest_are_checked(
    tmp_path: Path,
) -> None:
    html = _site(tmp_path, {"concepts/index.html": _page("The official way.")})
    (html / "bad.html").write_bytes(b"\xff\xfe")
    found = docs.check_html(html, ROOT)
    assert any(said.startswith("bad.html: cannot be checked") for said in found), found
    assert any("says official" in said for said in found), found


def test_a_secret_in_a_script_file_is_refused(tmp_path: Path) -> None:
    html = _site(tmp_path, {"_static/x.js": "var k = 'AKIA" + "B" * 16 + "';"})
    assert docs.check_html(html, ROOT) == [
        "_static/x.js: holds 1 thing(s) shaped like a secret"
    ]


def test_a_stylesheet_loading_from_another_origin_is_refused(tmp_path: Path) -> None:
    html = _site(
        tmp_path,
        {"_static/x.css": "a { background: url('https://x.example.com/p.png') }"},
    )
    assert docs.check_html(html, ROOT) == [
        "_static/x.css: its CSS loads from another origin"
    ]


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(_GOOD_TEXT + " See neolith._compat.", id="private-module"),
        pytest.param(_GOOD_TEXT + " python -m pip install -e .", id="install"),
        pytest.param(_GOOD_TEXT + " The step will add it.", id="future"),
        pytest.param(_GOOD_TEXT + " As CON-21 said, once.", id="withdrawn"),
        pytest.param(
            _GOOD_TEXT + " eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9xyz", id="example-token"
        ),
    ],
)
def test_a_mirrored_page_is_word_for_word_and_exempt_from_the_site_text_rules(
    tmp_path: Path, text: str
) -> None:
    assert _found(tmp_path, _page(text), "project/contributing/index.html") == []


def test_the_requirement_index_may_list_a_withdrawn_requirement(tmp_path: Path) -> None:
    page = _page(_GOOD_TEXT + ' <a href="../../spec/01-contracts/#con-21">CON-21</a>')
    found = _found(tmp_path, page, "reference/requirements/index.html", _SPEC)
    assert found == []


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Nothing is installable yet; read the specification.", id="yet"),
        pytest.param(
            "An office is not a contract, and an officer is not one.", id="office"
        ),
        pytest.param(
            "The neolith package exports contract and action.", id="neolith-dot"
        ),
        pytest.param(
            "Use @contract on a Protocol class, as spec 01 says.", id="decorator"
        ),
        pytest.param(
            "A spinning top is not a pipeline or a hatchback.", id="near-install"
        ),
        pytest.param(
            "Add the app to INSTALLED_APPS, then install its contract.",
            id="install-word",
        ),
        pytest.param(
            "A name reserved for a later standard kind is refused.", id="later-kind"
        ),
        pytest.param(
            "The digest 0123456789abcdef0123456789abcdef is hex.", id="hex-digest"
        ),
        pytest.param(
            '<a href="../con%63epts/">this page</a> and ' + _GOOD_TEXT,
            id="encoded-link",
        ),
        pytest.param(
            '<a href="#%78" id="x">here</a> and ' + _GOOD_TEXT, id="encoded-anchor"
        ),
        pytest.param(
            '<a href="#y">there</a><a name="y"></a> and ' + _GOOD_TEXT,
            id="named-anchor",
        ),
        pytest.param(
            "The package is neolith.<div>_private names are not shown.</div> "
            + _GOOD_TEXT,
            id="blocks-apart",
        ),
        pytest.param(
            '<img src="../index.html" srcset="../index.html 1x, ../index.html 2x"> '
            + _GOOD_TEXT,
            id="srcset-local",
        ),
    ],
)
def test_the_nearest_legitimate_text_passes(tmp_path: Path, text: str) -> None:
    assert _found(tmp_path, _page(text)) == []


def test_the_search_page_needs_no_text_without_javascript(tmp_path: Path) -> None:
    assert _found(tmp_path, _page("", h1=""), "search/index.html") == []


def test_a_release_build_needs_no_banner_and_refuses_no_install(tmp_path: Path) -> None:
    page = _page(_GOOD_TEXT + " pip install django-neolith", head="", banner="")
    html = _site(tmp_path, {"index.html": page})
    assert docs.check_html(html, ROOT, development=False) == []


def test_the_people_are_the_authors_the_holder_and_the_committers(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nauthors = [{ name = "Ada Lovelace and project contributors" }]\n',
        encoding="utf-8",
    )
    (tmp_path / "LICENSE").write_text(
        "Copyright (c) 2026, Grace Hopper\n", encoding="utf-8"
    )
    git = [
        "git",
        "-c",
        "user.name=Alan Turing",
        "-c",
        "user.email=7+aturing@users.noreply.github.com",
    ]
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "x"]):
        subprocess.run([*git, *args], cwd=tmp_path, check=True)  # noqa: S603
    assert docs.people(tmp_path) == [
        "Ada",
        "Alan",
        "aturing",
        "Grace",
        "Hopper",
        "Lovelace",
        "Turing",
    ]
    html = _site(
        tmp_path, {"concepts/index.html": _page("Thanks to @aturing for it, as ever.")}
    )
    assert any("names a person" in said for said in docs.check_html(html, tmp_path))


# The command line, and the steps around Sphinx


def _fake_sphinx(monkeypatch: pytest.MonkeyPatch, run: object) -> None:
    for name in ("sphinx", "sphinx.cmd"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    module = types.ModuleType("sphinx.cmd.build")
    module.build_main = run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sphinx.cmd.build", module)
    monkeypatch.setattr(docs, "fetch_inventories", lambda: ["inventory problem"])


def _warning_run(argv: list[str]) -> int:
    warnings = Path(argv[argv.index("-w") + 1])
    source = argv[-2]
    warnings.write_text(
        f"{source}/a.md:3: WARNING: one\n  more\n{source}/b.md: WARNING: two\n", "utf-8"
    )
    Path(argv[-1]).mkdir(parents=True)
    return 1


def test_a_build_reports_staging_the_inventories_and_every_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_sphinx(monkeypatch, _warning_run)
    result = docs.build(ROOT, tmp_path, {"bad.md": "# Bad\n\nmail a@example.org\n"})
    assert result.problems == [
        "bad.md: an email address on line 3; the page is left out",
        "inventory problem",
        "a.md:3: WARNING: one more",
        "b.md: WARNING: two",
    ]


def test_a_build_that_fails_without_a_warning_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(argv: list[str]) -> int:
        print("crashed here")
        return 2

    _fake_sphinx(monkeypatch, run)
    said = docs.build(ROOT, tmp_path).problems
    assert said[-1].startswith("Sphinx failed with status 2:\ncrashed here"), said


def test_a_build_that_raises_is_a_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(argv: list[str]) -> int:
        raise RuntimeError("boom")

    _fake_sphinx(monkeypatch, run)
    assert "Sphinx raised RuntimeError('boom')" in docs.build(ROOT, tmp_path).problems


def test_a_check_reports_every_step_and_a_version_it_cannot_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    html = _site(tmp_path, {"concepts/index.html": _page("The official way.")})
    monkeypatch.setattr(
        docs, "build", lambda root, out, extra: docs.Result(html, ["built"])
    )

    def unreadable(root: Path) -> bool:
        raise docs.DocsError("src/neolith/__init__.py: no __version__")

    monkeypatch.setattr(docs, "development", unreadable)
    found = docs.check(ROOT, tmp_path)
    assert found[:2] == [
        "built",
        "src/neolith/__init__.py: no __version__; checked as a development build",
    ]
    assert any("says official" in said for said in found), found


def test_a_check_says_when_no_html_was_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "html"
    monkeypatch.setattr(
        docs, "build", lambda root, out, extra: docs.Result(missing, [])
    )
    assert docs.check(ROOT, tmp_path) == [f"{missing}: no HTML was built"]


@pytest.mark.parametrize(
    ("command", "problems", "status"),
    [
        ("check", [], 0),
        ("check", ["a problem"], 1),
        ("build", [], 0),
        ("build", ["a problem"], 1),
        ("stage", [], 0),
        ("stage", ["a problem"], 1),
        ("publish", [], 0),
        ("publish", ["a problem"], 1),
    ],
)
def test_the_exit_status_is_1_exactly_when_a_problem_is_found(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    problems: list[str],
    status: int,
) -> None:
    called: list[str] = []

    def step(name: str) -> object:
        def run(root: Path, out: Path, extra: object = None) -> docs.Result:
            called.append(name)
            return docs.Result(out, list(problems))

        return run

    def check(root: Path, out: Path, extra: object = None) -> list[str]:
        called.append("check")
        return list(problems)

    monkeypatch.setattr(docs, "stage", step("stage"))
    monkeypatch.setattr(docs, "build", step("build"))
    monkeypatch.setattr(docs, "check", check)
    monkeypatch.setattr(docs, "publish", step("publish"))
    assert docs.main([command, "--out", "x"]) == status
    assert called == [command]
    out, err = capsys.readouterr()
    if problems:
        assert "a problem" in err
        assert "docs: 1 problem(s)" in err
    else:
        assert out == f"docs: every check passed ({command})\n"


def test_a_place_that_is_not_safe_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    assert docs.main(["stage", "--out", str(ROOT)]) == 1
    assert "holds the repository" in capsys.readouterr().err


# Publishing (ADR 0024): the Worker, the site's root files and the URL contract


def _config() -> dict[str, object]:
    loaded = json.loads((ROOT / "wrangler.json").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_the_worker_serves_the_published_directory_at_the_base_path() -> None:
    config = _config()
    assert config["name"] == "neolith-docs"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(config["compatibility_date"]))
    assert config["assets"] == {
        "directory": (docs.OUT / "site").relative_to(ROOT).as_posix(),
        "base_path": docs.BASE_PATH,
        "html_handling": "auto-trailing-slash",
        "not_found_handling": "404-page",
    }
    assert docs.BASE_PATH == "/neolith"
    assert docs.SITE_URL.endswith(docs.BASE_PATH + "/")


def test_the_worker_serves_assets_only_on_the_routes_the_dashboard_sets() -> None:
    config = _config()
    # No script, no workers.dev copy of the site, and no route: a deploy
    # leaves the routes set in the dashboard as they are.
    assert config["workers_dev"] is False
    assert config["preview_urls"] is False
    assert not {"main", "route", "routes"} & config.keys()


def test_the_build_command_publishes_as_ci_does() -> None:
    script = (ROOT / "scripts" / "workers_build.sh").read_text(encoding="utf-8")
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    install = '-m pip install . --group docs "Django~=5.2.0"'
    assert install in script
    assert install in ci
    assert "scripts/docs.py publish\n" in script
    assert "scripts/docs.py publish --out" in ci
    assert "set -eu\n" in script


def _published(tmp_path: Path) -> Path:
    """A published site without Sphinx: an empty page for each staged page."""
    source = docs.stage(ROOT, tmp_path / "out").path
    site = tmp_path / "out" / "site"
    for page in source.rglob("*.md"):
        rel = page.relative_to(source).with_suffix("").as_posix()
        rel = rel.removesuffix("index").rstrip("/")
        target = site / docs.DEV / rel / "index.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("<p>page</p>", encoding="utf-8")
    for name, text in docs.root_files(ROOT).items():
        (site / name).write_text(text, encoding="utf-8")
    return site


def test_the_published_site_has_every_root_file_and_no_problem(
    tmp_path: Path,
) -> None:
    site = _published(tmp_path)
    assert sorted(docs.root_files(ROOT)) == [
        "404.html",
        "_redirects",
        "llms.txt",
        "versions.json",
    ]
    assert docs.root_problems(ROOT, site) == []


def test_every_redirect_line_is_valid() -> None:
    rules, problems = docs.redirect_rules(docs.root_files(ROOT)["_redirects"])
    assert problems == []
    assert len(rules) == len(docs.REDIRECTS)
    dynamic = [rule for rule in rules if rule.source.endswith("*")]
    assert rules[-len(dynamic) :] == dynamic
    assert {rule.code for rule in rules} <= {301, 302}


@pytest.mark.parametrize(
    ("line", "problem"),
    [
        ("/neolith/a/ /neolith/b/", "no status code"),
        ("/neolith/a/ /neolith/b/ 200", "status 200 is not a redirect"),
        ("/neolith/a/ /neolith/b/ 301 x", "not source, destination and code"),
        ("/elsewhere/ /neolith/b/ 301", "not under /neolith/"),
        ("/neolith/a/*/b/ /neolith/b/ 301", "a splat only at the end"),
        ("/neolith/a/ /neolith/:splat 301", ":splat without a splat"),
        ("/neolith/a/ b/ 301", "neither a path under /neolith/ nor a URL"),
        ("/neolith/a/ http://example.com/ 301", "neither a path under /neolith/ nor"),
        ("/neolith/a /neolith/b/ 301", "does not end in a slash"),
        ("/neolithic/ /neolith/b/ 301", "not under /neolith/"),
    ],
)
def test_a_bad_redirect_line_is_refused(line: str, problem: str) -> None:
    _, problems = docs.redirect_rules(line + "\n")
    assert len(problems) == 1, problems
    assert problem in problems[0]
    assert problems[0].startswith("_redirects line 1: ")


def test_a_source_without_its_slash_is_valid_beside_its_slashed_form() -> None:
    text = (
        "/neolith/ /neolith/dev/ 302\n/neolith /neolith/dev/ 302\n"
        "/neolith/a /neolith/b/ 301\n/neolith/a/ /neolith/b/ 301\n"
    )
    rules, problems = docs.redirect_rules(text)
    assert problems == []
    assert [rule.source for rule in rules] == [
        "/neolith/",
        "/neolith",
        "/neolith/a",
        "/neolith/a/",
    ]


def test_a_repeated_source_and_a_static_rule_after_a_splat_are_refused() -> None:
    text = (
        "# a comment\n\n/neolith/a/* /neolith/b/:splat 301\n"
        "/neolith/a/x/ /neolith/b/ 301\n/neolith/a/x/ /neolith/c/ 301\n"
    )
    _, problems = docs.redirect_rules(text)
    assert problems == [
        "_redirects line 4: a static rule after a splat; static rules come first",
        "_redirects line 5: a static rule after a splat; static rules come first",
        "_redirects line 5: /neolith/a/x/ is already a source on line 4",
    ]


def _old_urls() -> list[str]:
    """Every page of the site this one replaces, by the URL its sitemap lists."""
    urls = [
        "/neolith/",
        "/neolith/latest/",
        "/neolith/decisions/",
        "/neolith/spec/",
        "/neolith/reference/",
        "/neolith/tutorial/",
        "/neolith/how-to/",
    ]
    for staged in docs.mirror_table(ROOT).values():
        section, _, name = staged.removesuffix(".md").partition("/")
        if name != "index":
            old = {"adr": "decisions"}.get(section, section)
            urls.append(f"/neolith/{old}/{name}/")
    return urls


def test_every_old_url_reaches_a_page(tmp_path: Path) -> None:
    site = _published(tmp_path)
    rules, _ = docs.redirect_rules(docs.root_files(ROOT)["_redirects"])
    assert set(_old_urls()) <= set(docs.old_urls(ROOT))
    for url in docs.old_urls(ROOT):
        hops, end = docs.follow(rules, url)
        assert hops, url
        if not end.startswith(REPO + "/"):
            assert docs.asset(site, end) is not None, (url, end)
    assert len(_old_urls()) > 40  # every record, spec document and project page


def test_record_0011_moves_permanently_to_its_new_name(tmp_path: Path) -> None:
    site = _published(tmp_path)
    rules, _ = docs.redirect_rules(docs.root_files(ROOT)["_redirects"])
    new = "adr/0011-the-host-serves-contract-endpoints-itself/"
    hops, end = docs.follow(
        rules, "/neolith/decisions/0011-django-ninja-for-contract-endpoints/"
    )
    assert hops == [(301, f"/neolith/latest/{new}"), (302, f"/neolith/dev/{new}")]
    assert docs.asset(site, end) == site / "dev" / new / "index.html"


def test_the_home_and_latest_lead_to_dev_until_the_first_release(
    tmp_path: Path,
) -> None:
    site = _published(tmp_path)
    rules, _ = docs.redirect_rules(docs.root_files(ROOT)["_redirects"])
    for start in ("/neolith", "/neolith/", "/neolith/latest", "/neolith/latest/"):
        assert docs.follow(rules, start) == ([(302, "/neolith/dev/")], "/neolith/dev/")
    assert docs.follow(rules, "/neolith/latest/spec/") == (
        [(302, "/neolith/dev/spec/")],
        "/neolith/dev/spec/",
    )
    # Every page of dev is reachable under latest.
    for page in (site / "dev").rglob("index.html"):
        rel = page.parent.relative_to(site / "dev").as_posix()
        url = "/neolith/latest/" + ("" if rel == "." else rel + "/")
        _, end = docs.follow(rules, url)
        assert docs.asset(site, end) == page, url


def test_the_page_kept_on_github_leads_there() -> None:
    rules, _ = docs.redirect_rules(docs.root_files(ROOT)["_redirects"])
    hops, _ = docs.follow(rules, "/neolith/reference/django-host/")
    assert hops == [(301, f"{REPO}/blob/main/docs/django-host.md")]
    assert (ROOT / "docs" / "django-host.md").is_file()


def test_a_redirect_to_no_page_is_a_problem(tmp_path: Path) -> None:
    site = _published(tmp_path)
    shutil.rmtree(site / "dev" / "spec")
    said = docs.root_problems(ROOT, site)
    assert "_redirects: /neolith/spec/ leads to /neolith/dev/spec/, no page" in said


def test_a_redirect_loop_is_a_problem() -> None:
    rules, _ = docs.redirect_rules(
        "/neolith/a/ /neolith/b/ 301\n/neolith/b/ /neolith/a/ 301\n"
    )
    with pytest.raises(docs.DocsError, match="more than 5 redirects"):
        docs.follow(rules, "/neolith/a/")


def test_a_path_maps_to_its_asset_as_the_worker_serves_it(tmp_path: Path) -> None:
    site = _published(tmp_path)
    assert docs.asset(site, "/neolith/dev/") == site / "dev" / "index.html"
    assert docs.asset(site, "/neolith/llms.txt") == site / "llms.txt"
    assert docs.asset(site, "/neolith/dev") is None  # a 307 to the slash first
    assert docs.asset(site, "/neolith/missing/") is None
    assert docs.asset(site, "/elsewhere/") is None
    assert docs.asset(site, "/neolith/../out/") is None


def test_versions_lists_only_dev_before_the_first_release() -> None:
    versions = json.loads(docs.root_files(ROOT)["versions.json"])
    assert versions == [
        {"name": "dev (not released)", "version": "dev", "url": "/neolith/dev/"}
    ]


def test_llms_txt_lists_the_dev_pages_and_says_they_are_not_released(
    tmp_path: Path,
) -> None:
    text = docs.root_files(ROOT)["llms.txt"]
    assert text.startswith("# Neolith\n\n> Neolith lets a Django app run")
    assert "development version" in text
    assert "not released" in text
    links = re.findall(r"\]\(([^)]+)\)", text)
    assert len(links) >= 6
    assert all(link.startswith(docs.SITE_URL + "dev/") for link in links)
    site = _published(tmp_path)
    for link in links:
        path = docs.BASE_PATH + "/" + link.removeprefix(docs.SITE_URL)
        assert docs.asset(site, path) is not None, link


def test_a_link_in_llms_txt_to_no_page_is_a_problem(tmp_path: Path) -> None:
    site = _published(tmp_path)
    shutil.rmtree(site / "dev" / "concepts")
    assert docs.root_problems(ROOT, site) == [
        f"llms.txt: links to {docs.SITE_URL}dev/concepts/, no page"
    ]


def test_the_404_page_is_noindex_with_the_banner_and_the_footer(
    tmp_path: Path,
) -> None:
    text = docs.root_files(ROOT)["404.html"]
    page = docs._Page()
    page.feed(text)
    page.close()
    assert list(docs._page_problems(page, "404.html", development=True)) == []
    assert page.hrefs == ["/neolith/dev/"]
    assert "<link" not in text
    assert "<script" not in text


@pytest.mark.parametrize(
    ("old", "new", "problem"),
    [
        ('content="noindex, nofollow"', 'content="all"', "no noindex"),
        (docs.BANNER, "Hello.", "no development banner"),
        (docs.FOOTER, "Hello.", "no footer"),
        ('href="/neolith/dev/"', 'href="/neolith/gone/"', "/neolith/gone/, no page"),
        ('href="/neolith/dev/"', 'href="dev/"', "dev/, not a path from the root"),
    ],
)
def test_a_bad_404_page_is_refused(
    tmp_path: Path, old: str, new: str, problem: str
) -> None:
    site = _published(tmp_path)
    path = site / "404.html"
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")
    said = docs.root_problems(ROOT, site)
    assert len(said) == 1, said
    assert said[0].startswith("404.html: ")
    assert problem in said[0]


def test_a_missing_root_file_is_a_problem(tmp_path: Path) -> None:
    site = _published(tmp_path)
    (site / "versions.json").unlink()
    assert docs.root_problems(ROOT, site) == ["versions.json: missing"]


def test_publishing_builds_a_development_version_whatever_the_version_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(docs.DEVELOPMENT_VARIABLE, raising=False)
    seen: list[object] = []

    def check(root: Path, out: Path, extra: object = None) -> list[str]:
        seen.append(os.environ.get(docs.DEVELOPMENT_VARIABLE))
        seen.append(docs.development_version("0.1.0"))
        html = out / "html"
        for rel in ("", "spec/", "concepts/", "reference/", "adr/", "project/"):
            (html / rel).mkdir(parents=True, exist_ok=True)
            (html / rel / "index.html").write_text("<p>page</p>", "utf-8")
        (html / "project" / "changelog").mkdir()
        (html / "project" / "changelog" / "index.html").write_text("x", "utf-8")
        return ["a problem of the check"]

    monkeypatch.setattr(docs, "check", check)
    result = docs.publish(ROOT, tmp_path / "out")
    assert seen == ["1", True]
    assert docs.DEVELOPMENT_VARIABLE not in os.environ
    assert docs.development_version("0.1.0") is False
    assert result.path == tmp_path / "out" / "site"
    assert (result.path / "dev" / "spec" / "index.html").is_file()
    for name, text in docs.root_files(ROOT).items():
        assert (result.path / name).read_text(encoding="utf-8") == text
    # The check's problems are kept; the old URLs of pages this fake build
    # lacks are problems too.
    assert result.problems[0] == "a problem of the check"
    assert any("leads to /neolith/dev/adr/0" in p for p in result.problems)


def test_publishing_restores_the_variable_it_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(docs.DEVELOPMENT_VARIABLE, "0")

    def check(root: Path, out: Path, extra: object = None) -> list[str]:
        raise RuntimeError("boom")

    monkeypatch.setattr(docs, "check", check)
    with pytest.raises(RuntimeError):
        docs.publish(ROOT, tmp_path / "out")
    assert os.environ[docs.DEVELOPMENT_VARIABLE] == "0"


def test_a_development_version_is_a_dev_one_or_one_publishing_asks_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(docs.DEVELOPMENT_VARIABLE, raising=False)
    assert docs.development_version("0.1.0a4.dev0") is True
    assert docs.development_version("0.1.0a3") is False
    monkeypatch.setenv(docs.DEVELOPMENT_VARIABLE, "1")
    assert docs.development_version("0.1.0a3") is True
    monkeypatch.setenv(docs.DEVELOPMENT_VARIABLE, "yes")
    assert docs.development_version("0.1.0a3") is False


def test_the_site_configuration_asks_whether_it_builds_a_development_version() -> None:
    conf = (ROOT / "docs" / "site" / "conf.py").read_text(encoding="utf-8")
    assert "development = docs.development_version(neolith.__version__)\n" in conf


# The build (needs the docs group)

_needs_sphinx = pytest.mark.skipif(
    importlib.util.find_spec("sphinx") is None,
    reason="the docs group is not installed (it needs Python 3.13 or newer)",
)


def _run(call: str, setup: str = "") -> list[str]:
    """Run ``call`` on ``scripts.docs`` in a fresh interpreter; return its list.

    A warning raised in the build's own code is an error. Sphinx and
    MyST-Parser raise deprecation warnings of their own in Sphinx 9.2
    (sphinx-doc/sphinx#14270 for napoleon), so the run does not make every
    warning an error, as the test suite does.
    """
    paths = [str(ROOT), str(ROOT / "docs" / "site" / "_ext")]
    code = (
        f"import sys; sys.path[:0] = {paths!r}\n"
        "from pathlib import Path\nfrom scripts import docs, citations\n"
        "import neolith_docs\n"
        f"{setup}\n"
        f"print(repr({call}))\n"
    )
    strict = [
        arg
        for module in ("scripts.docs", "scripts.citations", "neolith_docs")
        for arg in ("-W", f"error::Warning:{module}")
    ]
    done = subprocess.run(  # noqa: S603
        [sys.executable, *strict, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    assert done.returncode == 0, done.stderr
    result = ast.literal_eval(done.stdout.strip().splitlines()[-1])
    assert isinstance(result, list)
    return [str(line) for line in result]


@_needs_sphinx
def test_the_site_builds_and_passes_every_check(tmp_path: Path) -> None:
    extra = {
        "extra/code.md": "---\norphan: true\n---\n\n# Code\n\n"
        "The text `CON-1` stays code.\n\n## About CON-1\n\nThis heading stays one.\n"
    }
    assert _run(f"docs.check(docs.ROOT, Path({str(tmp_path)!r}), {extra!r})") == []
    built = tmp_path / "html"
    code = (built / "extra" / "code" / "index.html").read_text(encoding="utf-8")
    (span,) = re.findall(r"<code[^>]*>.*?</code>", code, re.DOTALL)
    assert "CON-1" in span
    assert "<a" not in span, span
    (heading,) = re.findall(r"<h2[^>]*>.*?</h2>", code, re.DOTALL)
    assert "CON-1" in heading
    assert "spec/01-contracts" not in heading, heading

    def read(path: str) -> str:
        return (built / path / "index.html").read_text(encoding="utf-8")

    spec = read("spec/01-contracts")
    assert re.search(
        r'<strong class="neolith-requirement" id="con-1">CON-1\.</strong>', spec
    )
    assert 'href="../../spec/01-contracts/#con-16"' in read(
        "adr/0019-wire-encodings-of-the-portable-forms"
    )
    forms = read("reference/api/forms")
    assert "alias of" not in forms
    assert re.search(r"Specified by.*spec 01", forms, re.DOTALL)
    assert re.search(r"Decided in.*ADR 0020</a> \(Accepted\)", forms, re.DOTALL)
    contributing = read("project/contributing")
    assert "github.com" not in contributing
    assert "<span>code of conduct</span>" in contributing
    note = "Only records with the status Accepted bind."
    assert note in read("adr/0005-small-required-core")
    assert note in read("adr")
    assert note not in read("adr/0001-record-architecture-decisions")
    record = json.loads((tmp_path / docs.PLANNED_RECORD).read_text("utf-8"))
    (ident,) = record["concepts/index.html"]
    assert f'class="admonition planned" id="{ident}"' in read("concepts")
    assert 'href="../spec/00-overview/#ovr-5"' in read("concepts")
    for repo_path, site_path in docs.mirror_files(docs.ROOT).items():
        assert (built / site_path).read_bytes() == (ROOT / repo_path).read_bytes()


# Each entry: a page, or a patch run before the build, and what must be said.
_BAD_PAGES = {
    "bad/withdrawn.md": (
        "As CON-21 says.",
        "3: WARNING: cites withdrawn requirement CON-21",
    ),
    "bad/requirement.md": (
        "As CON-9999 says.",
        "3: WARNING: cites unknown requirement CON-9999",
    ),
    "bad/record.md": ("As ADR 9999 says.", "3: WARNING: cites unknown ADR 9999"),
    "bad/document.md": (
        "As spec 99 says.",
        "3: WARNING: cites unknown spec document 99",
    ),
    "bad/step.md": (
        "```{planned} 99\nIt will ship.\n```",
        "WARNING: {planned} names roadmap step 99",
    ),
    "bad/heading.md": (
        "## About CON-9998\n\nText.",
        "3: WARNING: cites unknown requirement CON-9998",
    ),
    "bad/link-words.md": (
        "See [CON-9997](../index.md).",
        "3: WARNING: cites unknown requirement CON-9997",
    ),
    "bad/link-anchor.md": (
        "See [the rule](../spec/01-contracts.md#con-21).",
        "WARNING: links to withdrawn requirement CON-21",
    ),
    "bad/class.md": (
        "```{note}\n:class: planned\nIt ships.\n```",
        'WARNING: the class "planned" belongs to {planned} blocks only',
    ),
    "bad/raw.md": (
        '<div DATA-NEOLITH-PLANNED="5">It will ship.</div>',
        "WARNING: raw HTML is not allowed in a page the site writes",
    ),
    "bad/install.md": (
        "Install it with pip install django-neolith, as shown here.",
        "/index.html: shows an install command before a release",
    ),
    "bad/email.md": (
        "Write to someone@example.org about it.",
        ": an email address on line 3; the page is left out",
    ),
}
_BAD_PATCH = """
# Checked as a development build whatever __version__ says, so the rules that
# hold only before a release (bad/install.md) are checked too.
docs.development = lambda root: True
site_init = neolith_docs._Site.__init__
def patched(self, root):
    site_init(self, root)
    self.status["0005"] = "Maybe"
    self.records.requirement_files["CON-2"] = "02-transports.md"
neolith_docs._Site.__init__ = patched
neolith_docs._alias_comment = lambda name, found: None
"""
_BAD_PATCHED = [
    # A block naming an unknown step is not recorded, so its words count.
    "bad/step/index.html: describes planned behaviour outside a {planned} block",
    # Raw HTML is refused, and its words count too.
    "bad/raw/index.html: describes planned behaviour outside a {planned} block",
    "record 0005: status 'Maybe' is not one the template admits",
    "requirement anchors",
    "neolith.Binary: no #: comment where it is defined",
]


@_needs_sphinx
def test_one_check_reports_every_problem_of_every_step(tmp_path: Path) -> None:
    pages = {path: f"# Bad\n\n{text}\n" for path, (text, _) in _BAD_PAGES.items()}
    said = "\n".join(
        _run(f"docs.check(docs.ROOT, Path({str(tmp_path)!r}), {pages!r})", _BAD_PATCH)
    )
    for path, (_, warning) in _BAD_PAGES.items():
        # Each is reported where it is found: Sphinx's warnings name the
        # staged file, and the HTML rules the built page.
        stem = path.removesuffix(".md")
        assert re.search(
            rf"^{re.escape(stem)}(?:\.md|/index\.html)?:?.*{re.escape(warning)}",
            said,
            re.MULTILINE,
        ), (path, said)
    for expected in _BAD_PATCHED:
        assert expected in said, (expected, said)


@_needs_sphinx
def test_the_published_site_is_a_checked_development_build(tmp_path: Path) -> None:
    assert _run(f"docs.publish(docs.ROOT, Path({str(tmp_path)!r})).problems") == []
    site = tmp_path / "site"
    home = (site / "dev" / "index.html").read_text(encoding="utf-8")
    assert '<meta name="robots" content="noindex, nofollow">' in home
    assert docs.BANNER in home
    assert not (site / "dev" / ".buildinfo").exists()
    for name, text in docs.root_files(ROOT).items():
        assert (site / name).read_text(encoding="utf-8") == text
