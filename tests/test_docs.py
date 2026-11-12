"""The documentation site's staging, reference groups and HTML rules (ADR 0024).

Everything here but the build tests runs without Sphinx, on every supported
Python. The build tests need the ``docs`` dependency group (Python 3.13 or
newer) and are skipped where it is not installed.
"""

import importlib.util
import os
import re
import subprocess
import sys
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
    assert docs.group_problems(docs.public_names(neolith)) == []
    grouped = [name for _, _, names in docs.API_GROUPS.values() for name in names]
    assert sorted(grouped) == sorted(set(neolith.__all__) - {"__version__"})


@pytest.mark.parametrize(
    ("public", "problem"),
    [
        pytest.param(
            [*docs.public_names(neolith), "NewThing"],
            "NewThing is public and in no group",
            id="ungrouped",
        ),
        pytest.param(
            [n for n in docs.public_names(neolith) if n != "connect"],
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
    assert docs.group_problems(docs.public_names(neolith)) == [
        "contract is in groups contracts and errors"
    ]


def test_each_group_page_documents_its_names_by_their_public_path() -> None:
    pages = docs.api_pages(neolith)
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
    page = docs.requirements_page(ROOT / "spec", docs.mirror_table(ROOT))
    listed = re.findall(
        r"^\| \[([A-Z]+-\d+)\]\(\.\./spec/([\w-]+)\.md#([a-z]+-\d+)\)",
        page,
        re.MULTILINE,
    )
    assert {rid for rid, _, _ in listed} == records.live | records.withdrawn
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
    assert docs.same_text(text, staged) is None
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


def test_staging_refuses_a_page_with_an_email_address(tmp_path: Path) -> None:
    with pytest.raises(docs.DocsError, match=r"bad\.md: an email address on line 3"):
        docs.stage(ROOT, tmp_path, {"bad.md": "# Bad\n\nmail someone@example.org\n"})


def test_staging_writes_every_mirrored_and_generated_page(tmp_path: Path) -> None:
    source = docs.stage(ROOT, tmp_path)
    for staged in docs.mirror_table(ROOT).values():
        assert (source / staged).is_file(), staged
    for slug in docs.API_GROUPS:
        assert (source / "reference" / "api" / f"{slug}.md").is_file()
    assert (source / "reference" / "requirements.md").is_file()
    assert (source / "index.md").is_file()
    assert not (source / "conf.py").exists()


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


def _page(
    main: str = _GOOD_TEXT,
    *,
    head: str = '<meta name="robots" content="noindex">',
    banner: str = docs.BANNER,
    footer: str = docs.FOOTER,
    h1: str = "<h1>Title</h1>",
) -> str:
    return (
        f"<html><head>{head}</head><body><div>{banner}</div>"
        f"<main>{h1}<p>{main}</p></main><footer>{footer}</footer></body></html>"
    )


def _site(tmp_path: Path, pages: dict[str, str]) -> Path:
    html = tmp_path / "html"
    for path, text in {"index.html": _page(), **pages}.items():
        (html / path).parent.mkdir(parents=True, exist_ok=True)
        (html / path).write_text(text, encoding="utf-8")
    return html


def test_a_good_page_passes(tmp_path: Path) -> None:
    html = _site(
        tmp_path, {"concepts/index.html": _page(_GOOD_TEXT + '<a href="../">home</a>')}
    )
    assert docs.check_html(html, ROOT) == []


def _person() -> str:
    people = docs._people(ROOT)
    assert people
    return people[0]


@pytest.mark.parametrize(
    ("page", "problem"),
    [
        (_page(footer=""), "no footer"),
        (_page(head=""), "no noindex on a development build"),
        (_page(banner=""), "no development banner"),
        (_page("write to someone@example.org"), "holds an email address"),
        (_page("token ghp_" + "a" * 36), "holds something shaped like a secret"),
        (
            _page(
                head='<meta name="robots" content="noindex">'
                '<script src="https://cdn.example.com/x.js"></script>'
            ),
            "loads https://cdn.example.com/x.js from another origin",
        ),
        (
            _page('<a href="../missing/">x</a>'),
            "links to ../missing/, which is not a page",
        ),
        (
            _page('<a href="../#nowhere">x</a>'),
            "links to ../#nowhere, an anchor the page lacks",
        ),
        (_page('<img src="pic.png">'), "pic.png does not exist"),
        (_page("", h1=""), "<main> has no heading and text without JavaScript"),
        (
            _page("See neolith._forms for the helpers of each form."),
            "names a private module",
        ),
        (_page("The official way to declare a contract is shown."), "says official"),
        (
            _page("This page was written with Claude for the reader."),
            "says how the code is written",
        ),
        (
            _page("Install it with pip install django-neolith today."),
            "shows an install command",
        ),
        (
            _page("The HTTP transport will carry calls to a detached app."),
            "describes planned behaviour",
        ),
    ],
)
def test_each_rule_refuses_its_page(tmp_path: Path, page: str, problem: str) -> None:
    html = _site(tmp_path, {"concepts/index.html": page})
    found = docs.check_html(html, ROOT)
    assert any(problem in said for said in found), found


def test_a_person_named_on_a_site_page_is_refused(tmp_path: Path) -> None:
    html = _site(
        tmp_path,
        {"concepts/index.html": _page(f"Thanks to {_person()} for the design.")},
    )
    assert any("names a person" in said for said in docs.check_html(html, ROOT))


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(_GOOD_TEXT + " See neolith._compat.", id="private-module"),
        pytest.param(_GOOD_TEXT + " python -m pip install -e .", id="install"),
        pytest.param(_GOOD_TEXT + " The step will add it.", id="future"),
    ],
)
def test_a_mirrored_page_is_word_for_word_and_exempt_from_the_site_text_rules(
    tmp_path: Path, text: str
) -> None:
    html = _site(tmp_path, {"project/contributing/index.html": _page(text)})
    assert docs.check_html(html, ROOT) == []


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
    ],
)
def test_the_nearest_legitimate_text_passes(tmp_path: Path, text: str) -> None:
    html = _site(tmp_path, {"concepts/index.html": _page(text)})
    assert docs.check_html(html, ROOT) == []


def test_planned_text_inside_a_planned_block_passes(tmp_path: Path) -> None:
    block = (
        '<div class="admonition planned"><p class="admonition-title">Planned</p>'
        "<p>The HTTP transport will carry calls to a detached app.</p></div>"
    )
    html = _site(tmp_path, {"concepts/index.html": _page(_GOOD_TEXT + block)})
    assert docs.check_html(html, ROOT) == []


def test_a_release_build_needs_no_banner_and_refuses_no_install(tmp_path: Path) -> None:
    page = _page(_GOOD_TEXT + " pip install django-neolith", head="", banner="")
    html = _site(tmp_path, {"index.html": page})
    assert docs.check_html(html, ROOT, development=False) == []


# The build (needs the docs group)

_needs_sphinx = pytest.mark.skipif(
    importlib.util.find_spec("sphinx") is None,
    reason="the docs group is not installed (it needs Python 3.13 or newer)",
)


def _run(call: str) -> subprocess.CompletedProcess[str]:
    """Run ``call`` on ``scripts.docs`` in a fresh interpreter.

    A warning raised in the build's own code is an error. Sphinx and
    MyST-Parser raise deprecation warnings of their own in Sphinx 9.2
    (sphinx-doc/sphinx#14270 for napoleon), so the run does not make every
    warning an error, as the test suite does.
    """
    code = (
        f"import sys; sys.path.insert(0, {str(ROOT)!r}); "
        "from pathlib import Path; from scripts import docs; "
        f"result = {call}; print(repr(result))"
    )
    strict = [
        arg
        for module in ("scripts.docs", "scripts.citations", "neolith_docs")
        for arg in ("-W", f"error::Warning:{module}")
    ]
    return subprocess.run(  # noqa: S603
        [sys.executable, *strict, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )


@_needs_sphinx
def test_the_site_builds_and_passes_every_check(tmp_path: Path) -> None:
    done = _run(f"docs.check(docs.ROOT, Path({str(tmp_path)!r}))")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == "[]", done.stdout
    built = tmp_path / "html"
    spec = (built / "spec" / "01-contracts" / "index.html").read_text(encoding="utf-8")
    assert re.search(
        r'<strong class="neolith-requirement" id="con-1">CON-1\.</strong>', spec
    )
    record = (
        built / "adr" / "0019-wire-encodings-of-the-portable-forms" / "index.html"
    ).read_text(encoding="utf-8")
    assert 'href="../../spec/01-contracts/#con-16"' in record
    forms = (built / "reference" / "api" / "forms" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "alias of" not in forms
    assert "Decided in" in forms
    contributing = (built / "project" / "contributing" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "github.com" not in contributing
    assert "<span>code of conduct</span>" in contributing
    proposed = (built / "adr" / "0005-small-required-core" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "Only records with the status Accepted bind." in proposed


_BAD = {
    "bad/withdrawn.md": ("As CON-21 says.", "cites withdrawn requirement CON-21"),
    "bad/requirement.md": ("As CON-9999 says.", "cites unknown requirement CON-9999"),
    "bad/record.md": ("As ADR 9999 says.", "cites unknown ADR 9999"),
    "bad/document.md": ("As spec 99 says.", "cites unknown spec document 99"),
    "bad/step.md": ("```{planned} 99\nLater.\n```", "names roadmap step 99"),
}


@_needs_sphinx
def test_the_build_refuses_each_bad_citation_and_step(tmp_path: Path) -> None:
    pages = {path: f"# Bad\n\n{text}\n" for path, (text, _) in _BAD.items()}
    done = _run(f"docs.build(docs.ROOT, Path({str(tmp_path)!r}), {pages!r})")
    assert done.returncode != 0
    for path, (_, warning) in _BAD.items():
        pattern = rf"{re.escape(path)}:\d+: WARNING: .*{re.escape(warning)}"
        assert re.search(pattern, done.stderr), (path, done.stderr)
