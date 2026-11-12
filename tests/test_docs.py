"""The documentation site's staging, reference groups and HTML rules (ADR 0024).

Everything here but the build tests runs without Sphinx, on every supported
Python. The build tests need the ``docs`` dependency group (Python 3.13 or
newer) and are skipped where it is not installed.
"""

import ast
import importlib.util
import os
import re
import shutil
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
        "tests",
    ):
        if (ROOT / name).is_dir():
            shutil.copytree(ROOT / name, root / name, ignore=skip)
        else:
            shutil.copy(ROOT / name, root / name)
    return root


def test_staging_reports_every_problem_and_leaves_out_only_the_bad_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _copy_root(tmp_path)
    with (root / "VISION.md").open("a", encoding="utf-8") as file:
        file.write("\nSee [this](NOWHERE.md).\n")
    with (root / "spec" / "01-contracts.md").open("a", encoding="utf-8") as file:
        file.write("\n**CON-1.** A second thing MUST be.\n")
    with (root / "docs" / "site" / "concepts" / "index.md").open(
        "a", encoding="utf-8"
    ) as file:
        file.write("\nWrite to someone@example.org.\n")
    title, intro, names = docs.API_GROUPS["errors"]
    monkeypatch.setitem(docs.API_GROUPS, "errors", (title, intro, (*names, "Bogus")))
    rewrite = docs.rewrite_links

    def changed(text: str, source: str, links: docs.Links) -> str:
        staged = rewrite(text, source, links)
        return staged + "\nAdded.\n" if source == "CHANGELOG.md" else staged

    monkeypatch.setattr(docs, "rewrite_links", changed)
    result = docs.stage(root, tmp_path / "out")
    said = "\n".join(result.problems)
    for expected in (
        "VISION.md: links to NOWHERE.md, which does not exist",
        "spec/01-contracts.md:",
        "CON-1 is already defined",
        "concepts/index.md: an email address on line",
        "group errors names Bogus, which is not public",
        "CHANGELOG.md: staging changed more than links",
    ):
        assert expected in said, (expected, said)
    assert not (result.path / "concepts" / "index.md").exists()
    assert not (result.path / "project" / "changelog.md").exists()
    assert (result.path / "project" / "vision.md").is_file()
    assert (result.path / "reference" / "requirements.md").is_file()
    errors = (result.path / "reference" / "api" / "errors.md").read_text("utf-8")
    assert "Bogus" not in errors
    assert "neolith.UsageError" in errors


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


def _found(tmp_path: Path, page: str, path: str = "concepts/index.html") -> list[str]:
    return docs.check_html(_site(tmp_path, {path: page}), ROOT)


def test_a_good_page_passes(tmp_path: Path) -> None:
    page = _page(_GOOD_TEXT + '<a href="../">home</a> <a href="#x" id="x">here</a>')
    assert _found(tmp_path, page) == []


def _person() -> str:
    people = docs._people(ROOT)
    assert people
    return people[0]


_PLANNED = '<div class="admonition planned" data-neolith-planned="5">{}</div>'

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
    ("mailto", _page('<a href="mailto:x">write</a>' + _GOOD_TEXT), "links to mailto:x"),
    ("secret-github", _page("token ghp_" + "a" * 36), "shaped like a secret"),
    ("secret-aws", _page("key AKIA" + "A" * 16), "shaped like a secret"),
    (
        "secret-private-key",
        _page("-----BEGIN RSA " + "PRIVATE KEY-----"),
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
        "secret-entity",
        _page("token gh&#112;_" + "a" * 36),
        "shaped like a secret",
    ),
    (
        "foreign-script",
        _page(head=_ROBOTS + '<script src="https://cdn.example.com/x.js"></script>'),
        "loads https://cdn.example.com/x.js from another origin",
    ),
    (
        "foreign-style-import",
        _page(
            head=_ROBOTS + '<style>@import url("https://x.example.com/a.css");</style>'
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
        _page('<a href="../missing/">x</a>' + _GOOD_TEXT),
        "../missing/, which does not exist",
    ),
    (
        "missing-html",
        _page('<a href="nope.html">x</a>' + _GOOD_TEXT),
        "nope.html, which does not exist",
    ),
    (
        "missing-file",
        _page('<img src="pic.png">' + _GOOD_TEXT),
        "pic.png, which does not exist",
    ),
    (
        "missing-anchor",
        _page('<a href="../#nowhere">x</a>' + _GOOD_TEXT),
        "an anchor the page lacks",
    ),
    (
        "absolute-link",
        _page('<a href="/concepts/">x</a>' + _GOOD_TEXT),
        "an absolute path",
    ),
    (
        "outside-link",
        _page('<a href="../../x/">x</a>' + _GOOD_TEXT),
        "outside the site",
    ),
    ("no-heading", _page(h1=""), "<main> has no heading"),
    ("no-text", _page(""), "<main> has no text"),
    # Pages the site writes.
    (
        "private",
        _page("See neolith._forms for the helpers of each form."),
        "names a private module",
    ),
    (
        "private-outside-main",
        _page(body='<a title="neolith._host.Connector" href="#">x</a>'),
        "names a private module",
    ),
    (
        "official",
        _page("The official way to declare a contract is shown."),
        "says official",
    ),
    (
        "how-written",
        _page("This page was written with Claude for the reader."),
        "says how the code is written",
    ),
    (
        "install-pip",
        _page("Install it with pip install django-neolith today."),
        "shows an install command",
    ),
    (
        "install-pipenv",
        _page("Install it with pipenv install django-neolith today."),
        "shows an install command",
    ),
    (
        "install-hatch",
        _page("Add it with hatch add django-neolith to the project."),
        "shows an install command",
    ),
    (
        "install-pin",
        _page("Require django-neolith==0.1 in the requirements file."),
        "shows an install command",
    ),
    (
        "planned",
        _page("The HTTP transport will carry calls to a detached app."),
        "describes planned behaviour",
    ),
    (
        "planned-lookalike",
        _page(_GOOD_TEXT + '<div class="admonition planned">It will ship.</div>'),
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
]


@pytest.mark.parametrize(
    ("page", "problem"), [p[1:] for p in _REFUSED], ids=[p[0] for p in _REFUSED]
)
def test_each_rule_refuses_its_page(tmp_path: Path, page: str, problem: str) -> None:
    found = _found(tmp_path, page)
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
    ],
)
def test_a_planned_block_exempts_only_the_planned_words_rule(
    tmp_path: Path, words: str, problem: str
) -> None:
    block = _PLANNED.format(
        "<p>It will carry calls. " + words.format(person=_person()) + "</p>"
    )
    found = _found(tmp_path, _page(_GOOD_TEXT + block))
    assert any(problem in said for said in found), found
    assert not any("planned behaviour" in said for said in found), found


def test_planned_words_inside_a_planned_block_pass(tmp_path: Path) -> None:
    block = _PLANNED.format(
        "<p>The HTTP transport will carry calls to a detached app.</p>"
    )
    assert _found(tmp_path, _page(_GOOD_TEXT + block)) == []


def test_every_match_of_a_rule_is_reported(tmp_path: Path) -> None:
    page = _page("The official way, officially. It will be, and is planned.")
    found = _found(tmp_path, page)
    assert sum("says official" in said for said in found) == 2, found
    assert sum("planned behaviour" in said for said in found) == 2, found


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
    ],
)
def test_a_mirrored_page_is_word_for_word_and_exempt_from_the_site_text_rules(
    tmp_path: Path, text: str
) -> None:
    assert _found(tmp_path, _page(text), "project/contributing/index.html") == []


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
    ],
)
def test_the_nearest_legitimate_text_passes(tmp_path: Path, text: str) -> None:
    assert _found(tmp_path, _page(text)) == []


def test_a_release_build_needs_no_banner_and_refuses_no_install(tmp_path: Path) -> None:
    page = _page(_GOOD_TEXT + " pip install django-neolith", head="", banner="")
    html = _site(tmp_path, {"index.html": page})
    assert docs.check_html(html, ROOT, development=False) == []


def test_the_people_are_the_authors_and_the_copyright_holder(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nauthors = [{ name = "Ada Lovelace and project contributors" }]\n',
        encoding="utf-8",
    )
    (tmp_path / "LICENSE").write_text(
        "Copyright (c) 2026, Grace Hopper\n", encoding="utf-8"
    )
    assert docs._people(tmp_path) == ["Ada", "Grace", "Hopper", "Lovelace"]


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
    assert _run(f"docs.check(docs.ROOT, Path({str(tmp_path)!r}))") == []
    built = tmp_path / "html"

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
    assert 'data-neolith-planned="5"' in read("concepts")


# Each entry: a page, or a patch run before the build, and what must be said.
_BAD_PAGES = {
    "bad/withdrawn.md": ("As CON-21 says.", "cites withdrawn requirement CON-21"),
    "bad/requirement.md": ("As CON-9999 says.", "cites unknown requirement CON-9999"),
    "bad/record.md": ("As ADR 9999 says.", "cites unknown ADR 9999"),
    "bad/document.md": ("As spec 99 says.", "cites unknown spec document 99"),
    "bad/step.md": ("```{planned} 99\nIt will ship.\n```", "names roadmap step 99"),
    "bad/heading.md": (
        "## About CON-9998\n\nText.",
        "cites unknown requirement CON-9998",
    ),
    "bad/link-words.md": (
        "See [CON-9997](../index.md).",
        "cites unknown requirement CON-9997",
    ),
    "bad/link-anchor.md": (
        "See [the rule](../spec/01-contracts.md#con-21).",
        "links to withdrawn requirement CON-21",
    ),
    "bad/class.md": (
        "```{note}\n:class: planned\nIt ships.\n```",
        'the class "planned" belongs to {planned} blocks only',
    ),
    "bad/raw.md": (
        '<div data-neolith-planned="5">It ships.</div>',
        "raw HTML may not imitate a {planned} block",
    ),
    "bad/install.md": (
        "Install it with pip install django-neolith, as shown here.",
        "bad/install/index.html: shows an install command before a release",
    ),
    "bad/email.md": (
        "Write to someone@example.org about it.",
        "bad/email.md: an email address on line 3; the page is left out",
    ),
}
_BAD_PATCH = """
site_init = neolith_docs._Site.__init__
def patched(self, root):
    site_init(self, root)
    self.status["0005"] = "Maybe"
    self.records.requirement_files["CON-2"] = "02-transports.md"
neolith_docs._Site.__init__ = patched
neolith_docs._alias_comment = lambda name, found: None
"""
_BAD_PATCHED = [
    # A block naming an unknown step carries no mark, so its words count.
    "bad/step/index.html: describes planned behaviour outside a {planned} block",
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
    for expected in [warning for _, warning in _BAD_PAGES.values()] + _BAD_PATCHED:
        assert expected in said, (expected, said)
