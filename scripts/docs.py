"""Build and check the documentation site (ADR 0024).

Usage::

    python scripts/docs.py stage   # write the source tree under build/docs/src
    python scripts/docs.py build   # stage, then build the HTML with Sphinx
    python scripts/docs.py check   # build, then check the HTML against the site rules
    python scripts/docs.py serve   # build, then serve it on http://127.0.0.1:8000/

Building needs the ``docs`` dependency group, which needs Python 3.13 or newer.
Staging and the checks need only the ``test`` group.

Every step runs and reports, whatever an earlier one found, and a failure in
one file or one step is reported, not raised, so one run lists every
problem: staging's, then Sphinx's, then the HTML rules'. The exit status is 1
exactly when a problem is found. ``--out`` may not be the repository or hold
it, and inside the repository must sit under ``build/``.

**Stage** writes a throwaway source tree:

- the pages under ``docs/site/``, written for the site;
- the repository documents in ``mirror_table``, word for word. Only link
  targets change: a link to a mirrored file points at its page, and a link to
  any other file of the repository points at it on GitHub at the built
  commit. markdown-it-py parses the source and the staged copy, and every
  token but a link's target must be equal, or the file is left out. A link
  to a path that does not exist is a problem;
- the API reference, one page per group of ``API_GROUPS``, which must name
  each name of ``neolith.__all__`` but ``__version__`` exactly once;
- the index of requirement IDs, read with ``scripts/spec_trace.py``, whose
  problems are reported too.

A staged page that holds an email address is a problem, and is left out.

**Build** runs Sphinx with ``-W -n --keep-going`` and reports each warning.
The extension ``docs/site/_ext/neolith_docs.py`` anchors requirements, links
and checks citations, refuses raw HTML in pages the site writes, and records
each ``{planned}`` block in ``planned.json``.

**Check** reads every built file as a reader gets it (``_Page.view``): the
whole page's text with character references decoded, joined across inline
elements such as highlighted code, the attributes a reader sees (``title``,
``alt``, ``aria-label``), and the decoded link targets, as well as the raw
file. On every page it refuses: no footer in the ``<footer>``, no
``noindex`` or no banner in the announcement on a development build, an
email address, something shaped like a secret (known token forms in any text
file; a high-entropy run on pages the site writes), a URL to another origin
in any attribute that loads (every attribute but a link's ``href``) or in
CSS, a link or anchor that leads nowhere, and a ``<main>`` without a heading
or text (the search page is exempt). On pages the site writes or generates,
it also refuses ``neolith._``, the word "official", the names and GitHub
handles of the people the repository records, words about how the code is
written, a command that installs the package or a requirement on it on a
development build, a citation of an unknown or withdrawn requirement, record
or document, and words about planned behaviour outside a ``{planned}``
block, which exempts its text from that rule only. The word rules state
forms, read case-insensitively, and are a backstop for review, not a proof.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import inspect
import io
import json
import math
import posixpath
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.parse
import urllib.request
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from markdown_it import MarkdownIt
from markdown_it.common.utils import unescapeAll
from markdown_it.token import Token

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "docs" / "site"
OUT = ROOT / "build" / "docs"
CACHE = ROOT / "build" / "docs-cache"

#: The API reference: one page per group, with its title, a line of
#: introduction and its names in order. Every public name is in one group.
API_GROUPS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "contracts": (
        "Contracts",
        "Declaring a contract and its actions, and implementing it (spec 01).",
        ("contract", "action", "implements", "Contract", "Action"),
    ),
    "apps": (
        "Apps and hosts",
        "Declaring an app, and connecting to a contract (spec 05).",
        ("NeolithAppConfig", "Provided", "connect", "Connector"),
    ),
    "call-context": (
        "Call context",
        "What an action knows about the call it serves (ADR 0017).",
        ("CallContext", "Principal"),
    ),
    "forms": (
        "Portable forms",
        "Types whose wire form differs from Pydantic's default (ADR 0020).",
        ("Binary", "Decimal128", "Duration"),
    ),
    "errors": (
        "Errors",
        "The exceptions neolith raises.",
        (
            "ContractError",
            "ContractDefinitionError",
            "DeclarationError",
            "UsageError",
        ),
    ),
}

#: Inventories for intersphinx: the docs each one belongs to, and its file.
INVENTORIES = {
    "python": ("https://docs.python.org/3/", "objects.inv"),
    "django": ("https://docs.djangoproject.com/en/5.2/", "_objects/"),
    "pydantic": ("https://docs.pydantic.dev/2.12/", "objects.inv"),
}

#: The footer every page carries.
FOOTER = "Neolith is not affiliated with or endorsed by the Django Software Foundation."
#: The banner every page of a development build carries.
BANNER = "Development version. Not released."

_MARKDOWN = MarkdownIt("commonmark").enable(["table", "strikethrough"])
_EMAIL = re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)+")
# An inline link or image destination, after "](" and optional blanks: in
# angle brackets, or a run without blanks whose parentheses are balanced one
# level deep, as CommonMark reads it.
_INLINE = re.compile(r"\]\([ \t]*(?:<([^<>\n]*)>|((?:[^\s()<>\\]|\\.|\([^\s()]*\))+))")
# A link reference definition: "[label]: destination".
_DEFINITION = re.compile(
    r"^ {0,3}\[(?:[^\]\\]|\\.)+\]:[ \t]*(?:\n[ \t]*)?(?:<([^<>\n]*)>|(\S+))",
    re.MULTILINE,
)
_BACKTICKS = re.compile(r"(?<![`\\])`+")
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
_SECRETS = re.compile(
    r"AKIA[0-9A-Z]{16}"  # AWS access key
    r"|gh[pousr]_[A-Za-z0-9]{36,}|github_pat_\w{22,}"  # GitHub tokens
    r"|xox[abposr]-[A-Za-z0-9-]{10,}"  # Slack tokens
    r"|sk-[A-Za-z0-9_-]{20,}"  # API keys in the sk- form
    r"|pypi-[A-Za-z0-9_-]{50,}"  # PyPI upload tokens
    r"|AIza[0-9A-Za-z_-]{35}"  # Google API keys
    r"|\b[MNO][A-Za-z0-9_-]{23,27}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}"  # Discord
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|https://hooks\.slack\.com/services/\S+"
    r"|https://discord(?:app)?\.com/api/webhooks/\S+"
    r"|https://api\.cloudflare\.com/client/v4/workers/builds/deploy_hooks/\S+"
    # A value given to a key, secret, token or password.
    r"""|(?i:\b(?:api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*['"]?"""
    r"[A-Za-z0-9_\-/+=.]{16,})"
)
# A run that may be a credential: judged by its mix of characters and entropy.
_TOKEN_RUN = re.compile(r"[A-Za-z0-9+/_=-]{32,}")
# The word rules are a backstop for review, not a proof: each states a form,
# not a list of spellings, and reads case-insensitively.
_OFFICIAL = re.compile(r"\bofficial(?:ly)?\b", re.IGNORECASE)
_PRIVATE = re.compile(r"\bneolith\._", re.IGNORECASE)
_HOW_WRITTEN = re.compile(
    r"\b(?:AI|LLMs?|Claude|ChatGPT|Copilot|Gemini|GPT(?:-\d+)?|vibe[- ]cod\w*"
    r"|hand-?written|machine-generated|AI-generated"
    r"|(?:written|generated|drafted) (?:by|with) (?:an? )?(?:model|agent|assistant))\b",
    re.IGNORECASE,
)
# Any command that installs this package, whatever the tool, and any
# requirement on a version of it.
_INSTALL = re.compile(
    r"(?:install|add)\b[^\n]{0,80}?\bdjango[-_.]?neolith\b"
    r"|\bdjango[-_.]?neolith\s*(?:===?|[<>!~]=|[<>]|@\s*\S)",
    re.IGNORECASE,
)
_PLANNED = re.compile(
    r"\b(?:will|shall|planned|plans? to|going to|upcoming|coming (?:soon|in|with)"
    r"|not (?:yet )?(?:implemented|supported|available|built|done|ready)"
    r"|(?:in|for|with) an? (?:future|later|coming|next) (?:release|version|step|update)"
    r"|scheduled (?:for|in)|yet to (?:be|come)|eventually|TBD"
    r"|(?:roadmap )?step \d+ (?:adds|brings|delivers|introduces|will))\b",
    re.IGNORECASE,
)


class DocsError(Exception):
    """The site cannot be staged, built or checked; the message lists why."""


# Mirroring


def mirror_table(root: Path) -> dict[str, str]:
    """Map each mirrored repository file to its page in the staged tree.

    Args:
        root: The repository root.

    Returns:
        Repository paths (POSIX, relative to ``root``) to staged paths.
    """
    table = {
        "spec/README.md": "spec/index.md",
        "docs/adr/README.md": "adr/index.md",
        "VISION.md": "project/vision.md",
        "docs/principles.md": "project/principles.md",
        "docs/roadmap.md": "project/roadmap.md",
        "docs/open-questions.md": "project/open-questions.md",
        "CONTRIBUTING.md": "project/contributing.md",
        "CHANGELOG.md": "project/changelog.md",
    }
    for path in sorted((root / "spec").glob("[0-9][0-9]-*.md")):
        table[f"spec/{path.name}"] = f"spec/{path.name}"
    for path in sorted((root / "docs" / "adr").glob("[0-9][0-9][0-9][0-9]-*.md")):
        table[f"docs/adr/{path.name}"] = f"adr/{path.name}"
    return table


@dataclass
class Links:
    """Where the links of one mirrored file lead, and what is wrong with them.

    Attributes:
        root: The repository root.
        table: The mirror table.
        repo_url: The repository on GitHub.
        ref: The commit the GitHub links name.
        problems: What is wrong, one line each.
    """

    root: Path
    table: Mapping[str, str]
    repo_url: str
    ref: str
    problems: list[str] = field(default_factory=list)

    def target(self, destination: str, source: str) -> str:
        """The staged destination of a link written in ``source``."""
        if not destination or destination.startswith("#"):
            return destination
        if _SCHEME.match(destination):
            return destination
        path, hash_, fragment = destination.partition("#")
        path = urllib.parse.unquote(path)
        if path.startswith("/"):
            self.problems.append(f"{source}: links to {destination}, an absolute path")
            return destination
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), path))
        if resolved == ".." or resolved.startswith("../"):
            self.problems.append(
                f"{source}: links to {destination}, outside the repository"
            )
            return destination
        on_disk = self.root / resolved
        if resolved not in self.table and on_disk.is_dir():
            readme = posixpath.join(resolved, "README.md")
            if readme in self.table:
                resolved = readme
        if resolved in self.table:
            here = posixpath.dirname(self.table[source])
            staged = posixpath.relpath(self.table[resolved], here or ".")
            return staged + hash_ + fragment
        if not on_disk.exists():
            self.problems.append(
                f"{source}: links to {destination}, which does not exist"
            )
            return destination
        kind = "tree" if on_disk.is_dir() else "blob"
        return f"{self.repo_url}/{kind}/{self.ref}/{resolved}" + hash_ + fragment


def _code_spans(text: str, tokens: Sequence[Token]) -> list[tuple[int, int]]:
    """Where ``text`` holds code: code blocks, HTML blocks and code spans."""
    starts = [0]
    for line in text.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    spans = [
        (starts[token.map[0]], starts[min(token.map[1], len(starts) - 1)])
        for token in tokens
        if token.type in ("fence", "code_block", "html_block") and token.map
    ]
    at = 0
    while (opening := _BACKTICKS.search(text, at)) is not None:
        closing = re.compile(rf"(?<![`]){opening[0]}(?![`])").search(
            text, opening.end()
        )
        if closing is None:
            at = opening.end()
            continue
        spans.append((opening.start(), closing.end()))
        at = closing.end()
    return spans


def rewrite_links(text: str, source: str, links: Links) -> str:
    """``text`` with each link destination replaced by its staged target.

    A destination is replaced where markdown-it-py reads a link to it, outside
    code. ``same_text`` then proves that nothing else changed.

    Args:
        text: The Markdown of a mirrored file.
        source: The file's repository path.
        links: Where links lead.

    Returns:
        The text, changed only in link destinations.
    """
    tokens = _MARKDOWN.parse(text)
    real = set(_destinations(tokens))
    code = _code_spans(text, tokens)
    edits = []
    for pattern in (_INLINE, _DEFINITION):
        for match in pattern.finditer(text):
            if any(start <= match.start() < end for start, end in code):
                continue
            group = 1 if match[1] is not None else 2
            old = unescapeAll(match[group])
            if _MARKDOWN.normalizeLink(old) not in real:
                continue
            new = links.target(old, source)
            if new == old:
                continue
            start, end = match.span(group)
            if group == 2 and re.search(r"[\s()<>\\]", new):
                new = "<" + new + ">"
            edits.append((start, end, new))
    for start, end, new in sorted(edits, reverse=True):
        text = text[:start] + new + text[end:]
    return text


def _destinations(tokens: Sequence[Token]) -> Iterator[str]:
    for token in tokens:
        if token.type == "link_open":
            yield str(token.attrs.get("href", ""))
        elif token.type == "image":
            yield str(token.attrs.get("src", ""))
        yield from _destinations(token.children or [])


def _tokens_differ(
    old: Sequence[Token], new: Sequence[Token], where: str
) -> str | None:
    if len(old) != len(new):
        return f"{where}: {len(old)} tokens became {len(new)}"
    for index, (a, b) in enumerate(zip(old, new, strict=True)):
        place = f"{where}[{index}] ({a.type})"
        for name in ("type", "tag", "nesting", "markup", "info", "block", "hidden"):
            if getattr(a, name) != getattr(b, name):
                return f"{place}: {name} changed"
        if a.map != b.map:
            return f"{place}: lines changed"
        # An inline token's content is its raw source, links included; its
        # children hold the text.
        if a.type != "inline" and a.content != b.content:
            return f"{place}: text changed"
        target = {"link_open": "href", "image": "src"}.get(a.type)
        attrs_a = {k: v for k, v in a.attrs.items() if k != target}
        attrs_b = {k: v for k, v in b.attrs.items() if k != target}
        if attrs_a != attrs_b:
            return f"{place}: attributes changed"
        changed = _tokens_differ(a.children or [], b.children or [], place)
        if changed:
            return changed
    return None


def same_text(original: str, staged: str) -> str | None:
    """Say how ``staged`` differs from ``original`` beyond link targets, if it does."""
    old_env: dict[str, object] = {}
    new_env: dict[str, object] = {}
    old = _MARKDOWN.parse(original, old_env)
    new = _MARKDOWN.parse(staged, new_env)
    if old_env.keys() != new_env.keys():
        return "link definitions changed"
    return _tokens_differ(old, new, "token")


def email_lines(text: str) -> list[int]:
    """The lines of ``text`` that hold something shaped like an email address."""
    return [
        number
        for number, line in enumerate(text.splitlines(), start=1)
        if _EMAIL.search(line)
    ]


# Generated pages


def group_problems(public: Collection[str]) -> list[str]:
    """Say where ``API_GROUPS`` and the public names disagree.

    Args:
        public: The names the reference must document.

    Returns:
        One line per name in no group, in two groups, or not public.
    """
    problems = []
    seen: dict[str, str] = {}
    for slug, (_, _, names) in API_GROUPS.items():
        for name in names:
            if name in seen:
                problems.append(f"{name} is in groups {seen[name]} and {slug}")
            seen[name] = slug
            if name not in public:
                problems.append(f"group {slug} names {name}, which is not public")
    problems.extend(
        f"{name} is public and in no group of API_GROUPS"
        for name in sorted(set(public) - set(seen))
    )
    return problems


def public_names(package: object) -> tuple[list[str], list[str]]:
    """The names the reference documents: ``__all__`` but ``__version__``.

    Returns:
        The names, and a problem if ``__all__`` is not a list of strings.
    """
    names = getattr(package, "__all__", None)
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        return [], ["neolith.__all__ is not a list of names"]
    return [name for name in names if name != "__version__"], []


def _directive(package: object, name: str) -> str:
    found = getattr(package, name)
    if isinstance(found, type):
        kind = "autoexception" if issubclass(found, BaseException) else "autoclass"
        return f".. {kind}:: neolith.{name}\n   :members:\n"
    if inspect.isfunction(found):
        return f".. autofunction:: neolith.{name}\n"
    return f".. autodata:: neolith.{name}\n   :no-value:\n"


def api_pages(package: object) -> tuple[dict[str, str], list[str]]:
    """The reference pages: one per group, then the index of groups.

    A group page documents only the names that are public and that the
    package defines, so a problem with one name is reported and the rest of
    the reference still builds.

    Args:
        package: The ``neolith`` package.

    Returns:
        Staged paths to page text, and one line per problem.
    """
    public, problems = public_names(package)
    pages = {}
    for slug, (title, intro, names) in API_GROUPS.items():
        body = ""
        for name in names:
            if name not in public:
                continue
            try:
                body += "\n" + _directive(package, name)
            except Exception as error:  # reported, and the rest built
                problems.append(f"neolith.{name}: cannot be documented: {error!r}")
        pages[f"reference/api/{slug}.md"] = (
            f"# {title}\n\n{intro}\n\n```{{eval-rst}}{body}```\n"
        )
    entries = "\n".join(f"{slug}" for slug in API_GROUPS)
    pages["reference/api/index.md"] = (
        "# API\n\nEvery name the `neolith` package exports, by group.\n\n"
        f"```{{toctree}}\n:maxdepth: 1\n\n{entries}\n```\n"
    )
    return pages, problems + group_problems(public)


def requirements_page(
    spec_dir: Path, table: Mapping[str, str]
) -> tuple[str, list[str]]:
    """The index of requirement IDs, with each one's document and level.

    Returns:
        The page, and each problem ``spec_trace`` reports in the specification.
    """
    from scripts import spec_trace

    trace = spec_trace.Trace()
    spec_trace.read_spec(spec_dir, trace)
    rows = []
    for rid, found in trace.requirements.items():
        page = posixpath.relpath(table[f"spec/{found.document}"], "reference")
        state = "Withdrawn" if found.withdrawn else (found.level or "")
        rows.append(
            f"| [{rid}]({page}#{rid.lower()}) | spec {found.document[:2]} | {state} |"
        )
    page = (
        "# Requirements\n\nEvery requirement ID in the specification, with its "
        "document and its level. A withdrawn ID keeps its place and its "
        "anchor.\n\n| ID | Document | Level |\n|---|---|---|\n" + "\n".join(rows) + "\n"
    )
    return page, [f"spec/{problem}" for problem in trace.problems]


# Staging


def repo_url(root: Path) -> str:
    """The repository on GitHub, from ``project.urls.Source``."""
    with (root / "pyproject.toml").open("rb") as file:
        url = tomllib.load(file)["project"]["urls"]["Source"]
    if not isinstance(url, str):
        raise DocsError("pyproject.toml: project.urls.Source is not a string")
    return url.rstrip("/")


def commit(root: Path) -> str:
    """The commit being built, or ``main`` outside a git checkout."""
    try:
        done = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "main"
    return done.stdout.strip()


def development(root: Path) -> bool:
    """Whether the source is a development version, as ``__version__`` says.

    Raises:
        DocsError: ``src/neolith/__init__.py`` cannot be read or has no
            ``__version__``.
    """
    try:
        text = (root / "src" / "neolith" / "__init__.py").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise DocsError(f"src/neolith/__init__.py: {error}") from error
    found = re.search(r'^__version__ = "([^"]+)"$', text, re.MULTILINE)
    if found is None:
        raise DocsError("src/neolith/__init__.py: no __version__")
    return ".dev" in found[1]


def inventory_file(name: str, cache: Path = CACHE) -> Path:
    """Where the inventory ``name`` is cached: one file per URL it comes from."""
    base, path = INVENTORIES[name]
    digest = hashlib.sha256((base + path).encode()).hexdigest()[:16]
    return cache / f"{name}-{digest}.inv"


def fetch_inventories(cache: Path = CACHE) -> list[str]:
    """Download each intersphinx inventory into ``cache`` unless it is there.

    The file is named by its URL, so a changed URL fetches anew. A cached
    inventory is a snapshot: it is not refreshed while it exists.

    Returns:
        One line per inventory that is neither cached nor downloadable.
    """
    problems = []
    cache.mkdir(parents=True, exist_ok=True)
    for name, (base, path) in INVENTORIES.items():
        target = inventory_file(name, cache)
        if target.exists():
            continue
        url = base + path
        try:
            # Some hosts refuse urllib's default User-Agent.
            request = urllib.request.Request(  # noqa: S310
                url, headers={"User-Agent": "docs-build"}
            )
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                target.write_bytes(response.read())
        except OSError as error:
            problems.append(f"cannot fetch the {name} inventory from {url}: {error}")
    return problems


@dataclass
class Result:
    """What a step produced, and every problem it found.

    Attributes:
        path: The staged source or the HTML directory.
        problems: One line per problem.
    """

    path: Path
    problems: list[str] = field(default_factory=list)


#: The file that marks a directory as one this script builds into.
MARKER = ".neolith-docs-build"


def prepare_out(root: Path, out: Path) -> Path:
    """Check that ``out`` is safe to build into, mark it, and return it.

    The build deletes ``out/src`` and ``out/html``, so ``out`` may not be the
    repository or hold it. Inside the repository it must sit under
    ``build/``, which git ignores; elsewhere it must be empty or already
    marked as a build directory.

    Raises:
        DocsError: ``out`` is not a safe place to build.
    """
    out = out.resolve()
    root = root.resolve()
    if out == root or out in root.parents:
        raise DocsError(f"{out}: holds the repository; build somewhere else")
    inside = root in out.parents
    if inside and (root / "build") not in out.parents:
        raise DocsError(f"{out}: inside the repository but not under build/")
    if (
        not inside
        and out.exists()
        and any(out.iterdir())
        and not (out / MARKER).exists()
    ):
        raise DocsError(f"{out}: not empty and not a docs build directory")
    out.mkdir(parents=True, exist_ok=True)
    (out / MARKER).touch()
    return out


def _mirror(
    root: Path, table: Mapping[str, str], problems: list[str]
) -> dict[str, str]:
    links = Links(root, table, repo_url(root), commit(root))
    pages: dict[str, str] = {}
    for repo_path, staged_path in table.items():
        try:
            original = (root / repo_path).read_text(encoding="utf-8")
            staged = rewrite_links(original, repo_path, links)
            differs = same_text(original, staged)
        except Exception as error:  # reported, and the rest staged
            problems.append(f"{repo_path}: cannot be staged: {error!r}")
            continue
        if differs:
            problems.append(f"{repo_path}: staging changed more than links: {differs}")
        else:
            pages[staged_path] = staged
    problems.extend(links.problems)
    return pages


def _generated(
    root: Path, table: Mapping[str, str], problems: list[str]
) -> dict[str, str]:
    pages: dict[str, str] = {}
    sys.path.insert(0, str(root / "src"))
    try:
        import neolith

        generated, said = api_pages(neolith)
        pages.update(generated)
        problems.extend(said)
    except Exception as error:  # reported, and the rest staged
        problems.append(f"the API reference cannot be generated: {error!r}")
    finally:
        sys.path.remove(str(root / "src"))
    try:
        requirements, said = requirements_page(root / "spec", table)
        pages["reference/requirements.md"] = requirements
        problems.extend(said)
    except Exception as error:  # reported, and the rest staged
        problems.append(f"the requirement index cannot be generated: {error!r}")
    return pages


def stage(
    root: Path = ROOT, out: Path = OUT, extra: Mapping[str, str] | None = None
) -> Result:
    """Write the site's source tree under ``out/src``, and collect its problems.

    Every check runs, whatever the others found, and a failure in one file or
    one step is reported as a problem, not raised. A mirrored file whose
    staged copy differs beyond its links, and a page that holds an email
    address, are left out of the tree; everything else is written.

    Args:
        root: The repository root.
        out: Where the build writes; ``prepare_out`` checks it.
        extra: More pages, by staged path, as site-written pages.

    Returns:
        The staged source directory, with every problem found.

    Raises:
        DocsError: ``out`` is not a safe place to build.
    """
    out = prepare_out(root, out)
    problems: list[str] = []
    source = out / "src"
    if source.exists():
        shutil.rmtree(source)
    shutil.copytree(
        root / "docs" / "site",
        source,
        ignore=shutil.ignore_patterns(
            "conf.py", "_ext", "_templates", "_static", "__pycache__"
        ),
    )
    table = mirror_table(root)
    pages = _mirror(root, table, problems)
    pages.update(_generated(root, table, problems))
    pages.update(extra or {})
    for staged_path, text in pages.items():
        try:
            target = source / staged_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        except Exception as error:  # reported, and the rest staged
            problems.append(f"{staged_path}: cannot be written: {error!r}")
    for page in sorted(source.rglob("*.md")):
        name = page.relative_to(source).as_posix()
        try:
            lines = email_lines(page.read_text(encoding="utf-8"))
        except Exception as error:  # reported, and the page left out
            problems.append(f"{name}: cannot be read: {error!r}; the page is left out")
            page.unlink()
            continue
        if lines:
            where = ", ".join(map(str, lines))
            problems.append(
                f"{name}: an email address on line {where}; the page is left out"
            )
            page.unlink()
    return Result(source, problems)


# Building


def _warnings(path: Path, source: Path) -> list[str]:
    """The warnings Sphinx wrote, one line each, relative to the staged tree."""
    if not path.exists():
        return []
    found: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        if re.search(r"\b(?:WARNING|ERROR|CRITICAL|SEVERE):", line) or not found:
            found.append(line.replace(str(source) + "/", ""))
        else:
            found[-1] += " " + line.strip()
    return found


def build(
    root: Path = ROOT, out: Path = OUT, extra: Mapping[str, str] | None = None
) -> Result:
    """Stage, then build the HTML with Sphinx, collecting every problem.

    Sphinx runs with ``-W -n --keep-going`` whatever staging found, so its
    warnings are reported with staging's. The extension records each
    ``{planned}`` block it writes in ``out/planned.json``.

    Returns:
        The HTML directory, with the problems of staging and every warning.

    Raises:
        DocsError: Sphinx is not installed, or ``out`` is not a safe place.
    """
    try:
        from sphinx.cmd.build import build_main
    except ImportError as error:
        raise DocsError(
            "building needs the docs dependency group, which needs Python 3.13 "
            "or newer: python -m pip install -e . --group docs"
        ) from error
    staged = stage(root, out, extra)
    out = staged.path.parent
    problems = staged.problems
    try:
        problems += fetch_inventories()
    except Exception as error:  # reported, and the build goes on
        problems.append(f"the inventories cannot be fetched: {error!r}")
    html = out / "html"
    if html.exists():
        shutil.rmtree(html)
    warnings = out / "warnings.txt"
    warnings.unlink(missing_ok=True)
    (out / PLANNED_RECORD).unlink(missing_ok=True)
    # Sphinx's own output is kept, and shown only if it failed without a
    # warning; each warning comes from the file -w writes.
    output = io.StringIO()
    status: object = 0
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            status = build_main(
                [
                    "-b",
                    "dirhtml",
                    "-c",
                    str(root / "docs" / "site"),
                    "-d",
                    str(out / "doctrees"),
                    "-E",
                    "-W",
                    "-n",
                    "--keep-going",
                    "-q",
                    "-w",
                    str(warnings),
                    str(staged.path),
                    str(html),
                ]
            )
    except Exception as error:  # reported with what came before
        problems.append(f"Sphinx raised {error!r}")
    said = _warnings(warnings, staged.path)
    problems.extend(said)
    if status and not said:
        problems.append(f"Sphinx failed with status {status}:\n{output.getvalue()}")
    return Result(html, problems)


# Checking the HTML

#: Where the extension records each ``{planned}`` block: page to element ids.
PLANNED_RECORD = "planned.json"
# A URL to another origin.
_FOREIGN_URL = re.compile(r"(?:https?:)?//[^\s\"'<>),]+", re.IGNORECASE)
# A URL that CSS loads, in url(), @import, image-set() or a list of them.
_FOREIGN_CSS = re.compile(
    r"""(?:url\(|@import|image-set\(|,)\s*(?:url\(\s*)?['"]?\s*(?:https?:)?//""",
    re.IGNORECASE,
)
_TEXT_FILES = (".html", ".js", ".css", ".txt", ".json", ".xml", ".svg", ".map")
_VOID = frozenset(
    [
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    ]
)
_BLOCK = frozenset(
    [
        "address",
        "article",
        "aside",
        "blockquote",
        "body",
        "br",
        "dd",
        "details",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "head",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "summary",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "title",
        "tr",
        "ul",
    ]
)
# Attributes whose text a reader sees, or a screen reader reads.
_SEEN_ATTRIBUTES = frozenset(
    {"title", "alt", "aria-label", "aria-description", "placeholder"}
)
# Attributes that hold a URL the page loads or links to.
_URL_ATTRIBUTES = frozenset({"href", "src", "data", "poster", "action", "srcset"})


class _Page(HTMLParser):
    """One page as a reader gets it.

    ``view`` is its whole text with character references decoded, joined
    across inline elements (as in highlighted code) and split at blocks, with
    the attributes a reader sees and the decoded link targets. The text of a
    ``{planned}`` block, known by the element ids the extension recorded, is
    also kept apart.

    Args:
        planned: The ids of this page's ``{planned}`` blocks.
    """

    def __init__(self, planned: Collection[str] = ()) -> None:
        super().__init__(convert_charrefs=True)
        self.planned_ids = set(planned)
        self.raw = ""
        self.ids: set[str] = set()
        self.links: list[str] = []
        self.hrefs: list[str] = []
        self.foreign: list[str] = []
        self.robots: list[str] = []
        self.seen: list[str] = []
        self.main_text: list[str] = []
        self.planned_text: list[str] = []
        self.footer_text: list[str] = []
        self.banner_text: list[str] = []
        self.styles: list[str] = []
        self.has_h1 = False
        self._open = dict.fromkeys(
            ("main", "planned", "footer", "banner", "skip", "style"), 0
        )
        self._stack: list[tuple[str, tuple[str, ...]]] = []

    def _attributes(self, tag: str, values: dict[str, str]) -> None:
        for name, value in values.items():
            if name in _SEEN_ATTRIBUTES or (tag == "meta" and name == "content"):
                self.seen.append(f" {value} ")
            if name == "style":
                self.styles.append(value)
            if name.startswith("xmlns"):
                continue  # a namespace name, not a load
            navigates = (tag in ("a", "area") and name == "href") or (
                tag == "link" and values.get("rel", "") in ("canonical", "alternate")
            )
            urls = (
                [part.split()[0] for part in value.split(",") if part.strip()]
                if name == "srcset"
                else [value]
            )
            if name in _URL_ATTRIBUTES:
                for url in urls:
                    self.hrefs.append(urllib.parse.unquote(url))
                    if not navigates and re.match(r"(?:https?:)?//", url, re.I):
                        self.foreign.append(url)
                    else:
                        self.links.append(url)
            elif not navigates and tag != "meta":
                self.foreign.extend(_FOREIGN_URL.findall(value))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if values.get("id"):
            self.ids.add(values["id"])
        if values.get("name") and tag == "a":
            self.ids.add(values["name"])
        if tag == "meta" and values.get("name", "").lower() == "robots":
            self.robots.append(values.get("content", ""))
        self._attributes(tag, values)
        if tag in _BLOCK:
            self._text(" ")
        if tag in _VOID:
            return
        classes = values.get("class", "").split()
        opened = tuple(
            kind
            for kind, here in (
                ("main", tag == "main"),
                ("planned", values.get("id", "") in self.planned_ids),
                ("footer", tag == "footer"),
                (
                    "banner",
                    any(c.startswith("bd-header-announcement") for c in classes),
                ),
                ("skip", tag in ("script", "style", "template")),
                ("style", tag == "style"),
            )
            if here
        )
        self._stack.append((tag, opened))
        for kind in opened:
            self._open[kind] += 1
        if tag == "h1" and self._open["main"]:
            self.has_h1 = True

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK:
            self._text(" ")
        if not any(name == tag for name, _ in self._stack):
            return
        while self._stack:
            name, opened = self._stack.pop()
            for kind in opened:
                self._open[kind] -= 1
            if name == tag:
                break

    def handle_data(self, data: str) -> None:
        if self._open["style"]:
            self.styles.append(data)
        if self._open["skip"]:
            return
        self._text(data)

    def _text(self, data: str) -> None:
        if self._open["planned"]:
            self.planned_text.append(data)
        else:
            self.seen.append(data)
        if self._open["footer"]:
            self.footer_text.append(data)
        if self._open["banner"]:
            self.banner_text.append(data)
        if self._open["main"]:
            self.main_text.append(data)

    def view(self, *, planned: bool = True) -> str:
        """What a reader gets, with or without the ``{planned}`` blocks."""
        parts = [*self.seen, " ", *(self.planned_text if planned else []), " "]
        return _joined([*parts, " ".join(self.hrefs)])


def people(root: Path) -> list[str]:
    """The people the repository records: authors, copyright holder, committers.

    Returns:
        Each word of three letters or more of their names, and the handle of
        each committer's GitHub address.
    """
    names: list[str] = []
    try:
        with (root / "pyproject.toml").open("rb") as file:
            authors = tomllib.load(file)["project"].get("authors", [])
    except (OSError, tomllib.TOMLDecodeError, KeyError):
        authors = []
    for author in authors:
        for part in re.split(r"\s+and\s+", str(author.get("name", ""))):
            if "contributors" not in part and part.strip():
                names.append(part.strip())
    try:
        licence = (root / "LICENSE").read_text(encoding="utf-8")
    except OSError:
        licence = ""
    for match in re.finditer(r"Copyright \(c\) [\d, -]+(.+)", licence):
        holder = match[1].strip().rstrip(".")
        if "contributors" not in holder:
            names.append(holder)
    try:
        log = subprocess.run(
            ["git", "log", "--format=%an%x00%ae"],  # noqa: S607
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        log = ""
    for line in set(log.splitlines()):
        name, _, address = line.partition("\0")
        names.append(name)
        handle = re.fullmatch(
            r"(?:\d+\+)?([\w.-]+)@users\.noreply\.github\.com", address
        )
        if handle:
            names.append(handle[1])
    words = {
        word for name in names for word in re.split(r"[\s,]+", name) if len(word) > 2
    }
    return sorted(words, key=str.lower)


def _mirrored_files(root: Path) -> set[str]:
    """The built files of the mirrored pages, relative to the site root."""
    files = set()
    for staged in mirror_table(root).values():
        stem = staged[: -len(".md")]
        files.add(stem + ".html" if stem.endswith("index") else stem + "/index.html")
    return files


def _joined(parts: list[str]) -> str:
    return " ".join("".join(parts).split())


def _matches(pattern: re.Pattern[str], text: str) -> list[str]:
    return sorted({found[0] for found in pattern.finditer(text)})


def _entropy(run: str) -> float:
    counts = {char: run.count(char) for char in set(run)}
    return -sum(n / len(run) * math.log2(n / len(run)) for n in counts.values())


def _secrets(texts: Iterable[str], *, seen: bool) -> set[str]:
    found = {match[0] for text in texts for match in _SECRETS.finditer(text)}
    if seen:
        for text in texts:
            for run in _TOKEN_RUN.findall(text):
                mixed = all(re.search(cls, run) for cls in ("[a-z]", "[A-Z]", "[0-9]"))
                if mixed and _entropy(run) >= 4.2:
                    found.add(run)
    return found


def _planned_record(html: Path) -> dict[str, list[str]]:
    record = html.parent / PLANNED_RECORD
    if not record.exists():
        return {}
    loaded = json.loads(record.read_text(encoding="utf-8"))
    return {str(k): [str(i) for i in v] for k, v in loaded.items()}


def check_html(
    html: Path,
    root: Path = ROOT,
    development: bool = True,
    planned: Mapping[str, Collection[str]] | None = None,
) -> list[str]:
    """Check every built file and page against the site rules.

    Every rule reads what a reader gets (``_Page.view``): the whole page, not
    only ``<main>``. A file that cannot be read is a problem, and the others
    are still checked.

    Args:
        html: The built site.
        root: The repository root.
        development: Whether this is a development build, which carries
            ``noindex`` and the banner and shows no install command.
        planned: The ids of the ``{planned}`` blocks on each page; by default,
            what the extension recorded beside ``html``.

    Returns:
        One line per problem.
    """
    from scripts import citations

    problems: list[str] = []
    try:
        records: citations.Records | None = citations.Records(root)
    except Exception as error:  # reported, and the rest checked
        problems.append(f"the citations cannot be read: {error!r}")
        records = None
    if planned is None:
        try:
            planned = _planned_record(html)
        except Exception as error:  # reported, and the rest checked
            problems.append(f"{PLANNED_RECORD} cannot be read: {error!r}")
            planned = {}
    mirrored = _mirrored_files(root)
    names = people(root)
    named = (
        re.compile(r"(?<![\w@])@?(?:" + "|".join(map(re.escape, names)) + r")\b", re.I)
        if names
        else None
    )
    pages: dict[str, _Page] = {}
    for path in sorted(html.rglob("*")):
        if not path.is_file() or path.suffix not in _TEXT_FILES:
            continue
        rel = path.relative_to(html).as_posix()
        try:
            problems.extend(
                _file_problems(path, rel, development, planned, pages, mirrored)
            )
            page = pages.get(rel)
            if page is not None and rel not in mirrored:
                problems.extend(
                    f"{rel}: {said}"
                    for said in _site_problems(page, rel, development, named, records)
                )
        except Exception as error:  # reported, and the rest checked
            problems.append(f"{rel}: cannot be checked: {error!r}")
    for rel, page in pages.items():
        problems.extend(_link_problems(html, rel, page.links, pages))
    return problems


def _file_problems(
    path: Path,
    rel: str,
    development: bool,
    planned: Mapping[str, Collection[str]],
    pages: dict[str, _Page],
    mirrored: Collection[str],
) -> Iterator[str]:
    raw = path.read_text(encoding="utf-8")
    page = _Page(planned.get(rel, ()))
    if path.suffix == ".html":
        page.raw = raw
        page.feed(raw)
        page.close()
    seen = page.view()
    # A run judged by entropy alone counts on pages the site writes; the
    # mirrored specification holds example tokens as test vectors.
    guessed = path.suffix == ".html" and rel not in mirrored
    count = len(_secrets([raw, seen], seen=False) | _secrets([seen], seen=guessed))
    if count:
        yield f"{rel}: holds {count} thing(s) shaped like a secret"
    css = raw if path.suffix == ".css" else "\n".join(page.styles)
    if _FOREIGN_CSS.search(css):
        yield f"{rel}: its CSS loads from another origin"
    if path.suffix != ".html" or rel.startswith("_static/"):
        return
    pages[rel] = page
    addresses = {found[0] for found in _EMAIL.finditer(raw)}
    addresses |= {found[0] for found in _EMAIL.finditer(seen)}
    yield from (f"{rel}: holds the email address {a}" for a in sorted(addresses))
    yield from (
        f"{rel}: links to {h}" for h in page.hrefs if h.lower().startswith("mailto:")
    )
    yield from (f"{rel}: {said}" for said in _page_problems(page, rel, development))


def _site_problems(
    page: _Page,
    rel: str,
    development: bool,
    named: re.Pattern[str] | None,
    records: object,
) -> Iterator[str]:
    """The rules for pages the site writes or generates, on the reader's view."""
    from scripts import citations

    everything = page.view()
    rules = [
        (_PRIVATE, everything + " " + page.raw, "names a private module"),
        (_OFFICIAL, everything, "says official"),
        (_HOW_WRITTEN, everything, "says how the code is written"),
        (
            _PLANNED,
            page.view(planned=False),
            "describes planned behaviour outside a {planned} block",
        ),
    ]
    if development:
        rules.append(
            (_INSTALL, everything, "shows an install command before a release")
        )
    if named is not None:
        rules.append((named, everything, "names a person"))
    for pattern, text, what in rules:
        yield from (f"{what} ({word!r})" for word in _matches(pattern, text))
    if isinstance(records, citations.Records):
        history = rel == "reference/requirements/index.html"
        for found in citations.find(everything):
            if found.kind == citations.REQUIREMENT and found.key in citations.NOT_IDS:
                continue
            if history and found.key in records.withdrawn:
                continue
            said = records.problem(found)
            if said is not None:
                yield said
        if not history:
            for href in page.hrefs:
                anchor = re.fullmatch(r"[a-z]+-\d+", href.partition("#")[2])
                if anchor and anchor[0].upper() in records.withdrawn:
                    yield f"links to withdrawn requirement {anchor[0].upper()}"


def _page_problems(page: _Page, rel: str, development: bool) -> Iterator[str]:
    if FOOTER not in _joined(page.footer_text):
        yield "no footer"
    if development:
        if not any("noindex" in robots.lower() for robots in page.robots):
            yield "no noindex on a development build"
        if BANNER not in _joined(page.banner_text):
            yield "no development banner"
    yield from (
        f"loads {link} from another origin" for link in sorted(set(page.foreign))
    )
    if rel != "search/index.html":
        if not page.has_h1:
            yield "<main> has no heading without JavaScript"
        if len(_joined(page.main_text)) < 40:
            yield "<main> has no text without JavaScript"


def _link_problems(
    html: Path, rel: str, links: list[str], pages: Mapping[str, _Page]
) -> Iterator[str]:
    here = posixpath.dirname(rel)
    for link in links:
        if not link or _SCHEME.match(link) or link.startswith("//"):
            continue
        path, _, fragment = link.partition("#")
        path = urllib.parse.unquote(path.partition("?")[0])
        if path.startswith("/"):
            yield f"{rel}: links to {link}, an absolute path"
            continue
        if not path:
            target = rel
        else:
            joined = posixpath.normpath(posixpath.join(here or ".", path))
            if joined == ".." or joined.startswith("../"):
                yield f"{rel}: links to {link}, outside the site"
                continue
            if path.endswith("/") or not posixpath.splitext(joined)[1]:
                joined = posixpath.join("" if joined == "." else joined, "index.html")
            target = joined
        if not (html / target).is_file():
            yield f"{rel}: links to {link}, which does not exist"
            continue
        page = pages.get(target)
        if (
            fragment
            and page is not None
            and urllib.parse.unquote(fragment) not in page.ids
        ):
            yield f"{rel}: links to {link}, an anchor the page lacks"


def check(
    root: Path = ROOT, out: Path = OUT, extra: Mapping[str, str] | None = None
) -> list[str]:
    """Build the site and check it, reporting every problem of every step.

    Returns:
        One line per problem: staging's, Sphinx's, then the HTML rules'.
    """
    built = build(root, out, extra)
    problems = list(built.problems)
    try:
        is_development = development(root)
    except DocsError as error:
        problems.append(f"{error}; checked as a development build")
        is_development = True
    if built.path.is_dir():
        problems.extend(check_html(built.path, root, is_development))
    else:
        problems.append(f"{built.path}: no HTML was built")
    return problems


def serve(html: Path, port: int) -> None:
    """Serve ``html`` on localhost until interrupted."""
    import functools
    import http.server

    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(html)
    )
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as server:
        print(f"Serving on http://127.0.0.1:{port}/")
        server.serve_forever()


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command; return the exit status: 0 with no problem, else 1."""
    parser = argparse.ArgumentParser(description="Build and check the docs site.")
    parser.add_argument("command", choices=["stage", "build", "check", "serve"])
    parser.add_argument("--out", type=Path, default=OUT, help="where to build")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    try:
        if args.command == "stage":
            problems = stage(ROOT, args.out).problems
        elif args.command == "build":
            problems = build(ROOT, args.out).problems
        elif args.command == "check":
            problems = check(ROOT, args.out)
        else:
            result = build(ROOT, args.out)
            for problem in result.problems:
                print(problem, file=sys.stderr)
            serve(result.path, args.port)
            return 0
    except DocsError as error:
        print(error, file=sys.stderr)
        return 1
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(f"docs: {len(problems)} problem(s)", file=sys.stderr)
        return 1
    print(f"docs: every check passed ({args.command})")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
