"""Check requirement IDs in spec/ and the coverage tests report (ADR 0018).

The script reads the specification and ``tests/spec_trace.toml``. Coverage
comes from the report that ``scripts/spec_trace_plugin.py`` writes when pytest
runs with ``--spec-report=PATH``: only a test that ran and passed covers the
requirements it cites.

The specification is parsed with markdown-it-py, a CommonMark parser, with
GFM tables and strikethrough enabled. The checker walks its token stream:
paragraphs, list items, headings, table rows and HTML blocks are prose; fenced
and indented code are sketches and are skipped. A wrapped paragraph or list
item is one statement. markdown-it-py is a development dependency only.

A normative lead-in is the last sentence or clause of a paragraph (after the
last ".", "!", "?" or ";") when it uses a normative word and either ends in
":" or is followed by a list, with only HTML blocks between. Each item of that
list is a requirement with its own ID, and the lead-in carries none. A list
that CommonMark starts right after a governed list (a changed bullet or
delimiter, or a split by an HTML block) is governed by the same lead-in. So
is a list introduced right after it by a paragraph that ends in ":" and has no
normative word or ID, such as "And also:" (any other paragraph makes its list
description, unless it ends in a normative lead-in or the list is an ordered
list that resumes the numbering), and an ordered list that resumes its numbering
before the next heading.

It always reports a problem, and exits 1, when:

- the text uses an RFC 2119 word that spec/README.md does not list, such as
  SHALL;
- normative text carries no requirement ID of its own: a statement, table
  row or HTML block without one, text before the first ID in a paragraph, text
  after a withdrawal, a heading, or an item in the list of a normative lead-in;
- a heading carries an ID;
- a normative lead-in carries an ID, or ends in ":" with no list after it;
- a code fence is not closed by its own closing line (CommonMark then ends it
  at the end of its container, such as a list item);
- an ID is malformed, uses another document's area or is defined twice;
- an ID marks no normative word, or more than one;
- a withdrawn requirement does not say "Withdrawn in spec <version>: <reason>";
- spec/ holds a Markdown file that is not a top-level numbered document, or a
  document whose number or area is missing from or repeated in the table in
  spec/README.md;
- ``tests/spec_trace.toml`` cannot be read, has an unknown key, enforces a
  document that does not exist, has a ``[not-tested]`` entry that is not a
  live, must-level requirement with a reason, or a ``[not-implemented]``
  entry that is not a live SHOULD- or MAY-level requirement with a reason;
- spec 06 has more than one heading *Requirements that bind others*, or one
  without a table under it (before the next heading) with an ``ID`` and a
  ``Binds`` column, or a row whose ``ID`` cell is not one live must-level
  requirement ID (bare or in backticks), is repeated, or has an empty
  ``Binds`` cell. Without the heading the list is empty;
- ``spec/conformance/cases/`` holds anything but case files named
  ``<part>.json`` or ``<part>-<topic>.json``, where ``<part>`` is
  ``contract``, ``wire``, ``routing`` or ``lifecycle`` and ``<topic>`` is
  ``[a-z][a-z0-9]*``;
- a case file is not UTF-8 JSON holding an array of objects, each with
  exactly the members ``name`` (``[a-z][a-z0-9_]*``), ``part`` (the file's
  ``<part>``), ``state``, ``procedure``, ``expected`` (strings) and
  ``requirements`` (a non-empty list of distinct live requirement IDs), each
  given once. Messages count the cases of a file from 1;
- a case name is listed twice, in one case file or in two (ADR 0032, the
  admitted set and Decision 7).

With ``--report``, it also reports a test that cites an unknown or withdrawn
ID, a ``[not-tested]`` or ``[not-implemented]`` entry for a requirement a
passing test covers, a test whose ``conformance_case`` names a case no case
file lists or that does not cite every ID its case lists, and a listed case
that no test in the report runs. For the enforced documents (``enforce`` in
the settings, plus ``--enforce``), every must-level requirement (MUST, MUST
NOT, REQUIRED) needs a passing test that cites it, or a ``[not-tested]``
entry. Enforcing without ``--report`` is a problem, so the gate cannot be
skipped by leaving the report out. The report must be format 2, as the plugin
writes it; any other member or value, or a member given twice, is a
problem, and a report with an entry
the checker cannot read counts no test at all, since that test may have
failed. Whatever the checker refuses takes no effect: a refused
``[not-tested]`` or ``[not-implemented]`` entry gives no status, and a refused
row of spec 06's table binds no one.

The conformance run
-------------------

With ``--report`` and ``--conformance PATH``, it writes the conformance run
(ADR 0032, Decision 9) to PATH, a JSON object with exactly these members:

- ``format``: ``1``;
- ``spec``: the specification version in the *Status* line of
  ``spec/README.md``, such as ``"0.1"``;
- ``suite``: ``{"cases", "reference", "tests"}``, each a SHA-256 in hex.
  ``reference`` is the hash of ``spec/conformance/reference.v1.openapi.json``,
  or ``null`` while it does not exist. ``cases`` is over every file under
  ``spec/conformance/cases/``, and ``tests`` over every file under ``tests/``
  apart from ``__pycache__`` directories, following links (a directory
  reached by two names is read under both, and a link back to a directory
  that holds it is left out): for each file, in the byte order of its path
  from the repository root, written with ``/``, the hash takes the path's
  bytes as the file system gives them (UTF-8 for a name that is UTF-8), a
  NUL byte, its size in bytes in decimal, a NUL byte and its bytes. A hash
  that cannot be computed, as when a file or directory cannot be read, is
  ``null`` and a problem;
- ``implementation``: ``{"name": "django-neolith", "version"}``, and
  ``environment``, both from the report;
- ``requirements``: for each live requirement ID of every level, in the
  specification's order, ``{"document", "level", "status", "tests"}``, with a
  ``reason`` for the statuses ``not-tested`` and ``not-implemented``.
  ``document`` is its document's number, ``level`` ``MUST``, ``SHOULD`` or
  ``MAY``, and ``tests`` the tests that cite it, ``{"test", "outcome"}`` and
  their ``transport_outcomes`` when the report has them. ``status`` is the
  first of these that applies: ``failed`` (a citing test failed or errored),
  ``passed`` (a passing test cites it), ``not-applicable`` (spec 06 lists it
  as binding others, whatever ``[not-tested]`` holds for it), ``not-tested``,
  ``not-implemented``, ``uncovered``;
- ``conforms``: true exactly when no requirement is ``failed``, and every
  must-level requirement of every document that the table in
  ``spec/README.md`` marks ``yes`` in its ``In <version>`` column is
  ``passed`` or ``not-applicable``. Every cell of that column must be
  ``yes``, ``no`` or ``outline only``.

The command prints why a run does not conform: each requirement that keeps
``conforms`` false, with its status. It refuses ``--conformance`` without
``--report``, a value with no file name, and a path it cannot write; it
empties PATH before reading anything, and writes the run only once the report
has been read, so a run that stops leaves no earlier run behind.

Usage::

    python -m pytest --spec-report=spec-report.json
    python scripts/spec_trace.py --report spec-report.json
    python scripts/spec_trace.py --report spec-report.json --conformance run.json
    python scripts/spec_trace.py              # the specification only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.token import Token

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = "tests/spec_trace.toml"
REPORT_FORMAT = 2
RUN_FORMAT = 1
# What the plugin records for a test, and the members of its report.
_OUTCOMES = ("passed", "skipped", "xfailed", "xpassed", "failed", "error", "not run")
_FAILED = ("failed", "error")
_REPORT_MEMBERS = {"format", "environment", "tests"}
_ENVIRONMENT_MEMBERS = {"python", "architecture", "packages"}
_PACKAGES = {"django-neolith", "Django"}
_ENTRY_MEMBERS = {"ids", "outcome", "case", "transport_outcomes"}
# The conformance suite's files under spec/ (ADR 0032).
CASES = "conformance/cases"
REFERENCE = "conformance/reference.v1.openapi.json"
_PARTS = ("contract", "wire", "routing", "lifecycle")
_CASE_FILE = re.compile(rf"({'|'.join(_PARTS)})(?:-[a-z][a-z0-9]*)?\.json")
_CASE_NAME = re.compile(r"[a-z][a-z0-9_]*")
_CASE_MEMBERS = ("name", "part", "state", "procedure", "expected", "requirements")
_BINDS_OTHERS = "Requirements that bind others"
_IN_VERSION = {"yes": True, "no": False, "outline only": False}
_STATUS_LINE = re.compile(r"^\s*-\s*\*\*Status:\*\*.*$", re.MULTILINE)
_VERSION = re.compile(r"\b(\d+\.\d+)\b")

# RFC 2119 keywords. Longer phrases come first so "MUST NOT" is one match.
_NORMATIVE = re.compile(
    r"\b(MUST NOT|MUST|REQUIRED|SHALL NOT|SHALL|SHOULD NOT|SHOULD|NOT RECOMMENDED"
    r"|RECOMMENDED|MAY|OPTIONAL)\b"
)
_LEVELS = {
    "MUST NOT": "MUST",
    "MUST": "MUST",
    "REQUIRED": "MUST",
    "SHALL NOT": "MUST",
    "SHALL": "MUST",
    "SHOULD NOT": "SHOULD",
    "SHOULD": "SHOULD",
    "NOT RECOMMENDED": "SHOULD",
    "RECOMMENDED": "SHOULD",
    "MAY": "MAY",
    "OPTIONAL": "MAY",
}
_STRENGTH = ["MUST", "SHOULD", "MAY"]
# The normative words spec/README.md lists. Other RFC 2119 words are reported.
_USED = ("MUST", "MUST NOT", "REQUIRED", "SHOULD", "MAY")

# "**XYZ-12.**". A "~~" right before it strikes the requirement through.
_ID = re.compile(r"(~~)?\*\*([A-Z]+-\d+)\.\*\*")
_WELL_FORMED = re.compile(r"([A-Z]{2,5})-([1-9]\d*)")
_WITHDRAWN = re.compile(r"Withdrawn in spec \d+\.\d+: \S")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?;])\s+")
_DOCUMENT = re.compile(r"(\d{2})-[a-z0-9-]+\.md")
_AREA_ROW = re.compile(r"\|\s*(\d{2})\s*\|.*\|\s*`([A-Z]{2,5})`\s*\|\s*$")
_SETTINGS_KEYS = {"enforce", "not-tested", "not-implemented"}
# CommonMark, plus the GFM tables and strikethrough the specification uses.
_MARKDOWN = MarkdownIt("commonmark").enable(["table", "strikethrough"])


@dataclass(frozen=True)
class Requirement:
    rid: str
    document: str
    line: int
    level: str | None
    withdrawn: bool


@dataclass(frozen=True)
class Citation:
    rid: str
    test: str
    outcome: str


@dataclass(frozen=True)
class Ran:
    """One test's entry in the report."""

    ids: tuple[str, ...]
    outcome: str
    case: str | None = None
    transport_outcomes: dict[str, str] | None = None


@dataclass(frozen=True)
class Case:
    """One case of a case file."""

    name: str
    file: str
    requirements: tuple[str, ...]


@dataclass(frozen=True)
class _Row:
    """A row of spec 06's table of requirements that bind others."""

    document: str
    line: int
    rid: str


@dataclass
class Trace:
    requirements: dict[str, Requirement] = field(default_factory=dict)
    citations: list[Citation] = field(default_factory=list)
    not_tested: dict[str, str] = field(default_factory=dict)
    not_implemented: dict[str, str] = field(default_factory=dict)
    documents: set[str] = field(default_factory=set)
    measured: bool = False
    problems: list[str] = field(default_factory=list)
    tests: dict[str, Ran] = field(default_factory=dict)
    environment: dict[str, Any] | None = None
    cases: dict[str, Case] = field(default_factory=dict)
    binds_others: set[str] = field(default_factory=set)
    rows: list[_Row] = field(default_factory=list)
    run: dict[str, Any] | None = None
    # Why the run does not conform: "<ID> <status>[: <reason>]".
    nonconforming: list[str] = field(default_factory=list)

    def covering(self, rid: str) -> list[Citation]:
        """Passing tests that cite ``rid``."""
        return [c for c in self.citations if c.rid == rid and c.outcome == "passed"]


def _shown(value: object) -> str:
    """A value from an input file, as JSON and cut to a readable length."""
    try:
        text = json.dumps(value, ensure_ascii=True)
    except (TypeError, ValueError, RecursionError):
        text = type(value).__name__
    return text if len(text) <= 40 else text[:37] + "..."


# Reading the specification


def read_areas(readme: Path, trace: Trace | None = None) -> dict[str, str]:
    """Map document numbers to ID areas, from the table in spec/README.md."""
    areas: dict[str, str] = {}
    for line in readme.read_text(encoding="utf-8").splitlines():
        match = _AREA_ROW.match(line)
        if not match:
            continue
        number, area = match.groups()
        repeated = (
            f"document {number}" if number in areas
            else f"area {area}" if area in areas.values()
            else None
        )  # fmt: skip
        if repeated is None:
            areas[number] = area
        elif trace is not None:
            trace.problems.append(f"README.md: {repeated} is listed twice")
    return areas


_LIST_OPEN = ("bullet_list_open", "ordered_list_open")
_LIST_CLOSE = ("bullet_list_close", "ordered_list_close")


@dataclass(frozen=True)
class _Unit:
    """One piece of prose that can hold requirements."""

    line: int  # first physical line, 1-based
    text: str  # the inline Markdown, wrapped lines joined by spaces
    # "item" (first paragraph of a list item), "continuation" (a later one),
    # "paragraph", "row" (a table row), "heading" or "html"
    kind: str
    # The level of the lead-in that governs this list item, if any.
    governed: str | None = None
    # True for a paragraph followed by a list, with only HTML blocks between.
    before_list: bool = False


@dataclass
class _List:
    lead: str | None  # the level of the lead-in that introduced it, if any
    last_number: int | None = None  # of the last item, in an ordered list


@dataclass
class _Item:
    line: int
    parent: _List
    paragraphs: int = 0


def _line(token: Token) -> int:
    return token.map[0] + 1 if token.map else 0


def _fence_closed(token: Token, lines: list[str]) -> bool:
    """True if a fence ends on its own closing line, not at its container's end."""
    if token.map is None:
        return False
    start, end = token.map
    if end - start < 2:
        return False
    last = re.sub(r"^[\s>]*", "", lines[end - 1]).rstrip()
    character, length = token.markup[0], len(token.markup)
    return re.fullmatch(rf"{re.escape(character)}{{{length},}}", last) is not None


def _last_clause(text: str) -> int:
    """Where the last sentence or clause of ``text`` starts."""
    breaks = list(_SENTENCE_BREAK.finditer(text.rstrip()))
    return breaks[-1].end() if breaks else 0


class _DocumentReader:
    """Collects the requirements of one specification document."""

    def __init__(self, name: str, area: str, trace: Trace) -> None:
        self.name = name
        self.area = area
        self.trace = trace

    def problem(self, number: int, text: str) -> None:
        self.trace.problems.append(f"{self.name}:{number}: {text}")

    def read(self, text: str) -> None:
        """Walk the CommonMark token stream of one document."""
        tokens = _MARKDOWN.parse(text)
        lines = text.splitlines()
        stack: list[_List | _Item] = []
        # The level of a lead-in, for the list that opens next. It survives
        # HTML blocks, and a governed list hands it on: CommonMark starts a new
        # list when the bullet or delimiter changes, and an HTML comment is the
        # usual way to split a list, yet a reader sees one list. Any other
        # token clears it.
        lead: str | None = None
        # A governed ordered list, as (lead, next number), until the next
        # heading: an ordered list that resumes its numbering after a note is
        # read as the same list.
        numbered: tuple[str, int] | None = None
        index = 0
        while index < len(tokens):
            token = tokens[index]
            carried = lead
            if token.type not in (*_LIST_OPEN, *_LIST_CLOSE, "html_block"):
                lead = None
            if token.type in _LIST_OPEN:
                start = int(token.attrGet("start") or 1)
                if lead is None and numbered is not None and start == numbered[1]:
                    lead = numbered[0]
                stack.append(_List(lead))
                lead = None
            elif token.type in _LIST_CLOSE:
                closed = stack.pop()
                lead = closed.lead if isinstance(closed, _List) else None
                if (
                    isinstance(closed, _List)
                    and closed.lead is not None
                    and closed.last_number is not None
                ):
                    numbered = (closed.lead, closed.last_number + 1)
            elif token.type == "list_item_open":
                parent = stack[-1]
                if isinstance(parent, _List):
                    stack.append(_Item(_line(token), parent))
                    if token.info.isdigit():
                        parent.last_number = int(token.info)
            elif token.type == "list_item_close":
                item = stack.pop()
                if (
                    isinstance(item, _Item)
                    and item.paragraphs == 0
                    and item.parent.lead is not None
                ):
                    self.problem(item.line, "item under a normative lead-in has no ID")
            elif token.type == "fence":
                if not _fence_closed(token, lines):
                    self.problem(_line(token), "code fence is never closed")
            elif token.type == "heading_open":
                numbered = None
                inline = tokens[index + 1]
                self.read_unit(_Unit(_line(token), inline.content, "heading"))
            elif token.type == "tr_open":
                cells = []
                row = index
                while tokens[index].type != "tr_close":
                    if tokens[index].type == "inline":
                        cells.append(tokens[index].content)
                    index += 1
                self.read_unit(_Unit(_line(tokens[row]), " | ".join(cells), "row"))
            elif token.type == "html_block":
                self.read_unit(
                    _Unit(_line(token), token.content.replace("\n", " "), "html")
                )
            elif token.type == "paragraph_open":
                inline = tokens[index + 1]
                after = index + 3  # the first token after paragraph_close
                while after < len(tokens) and tokens[after].type == "html_block":
                    after += 1
                kind, governed = "paragraph", None
                container = stack[-1] if stack else None
                if isinstance(container, _Item):
                    kind = "item" if container.paragraphs == 0 else "continuation"
                    governed = container.parent.lead if kind == "item" else None
                    container.paragraphs += 1
                unit = _Unit(
                    _line(token),
                    inline.content.replace("\n", " "),
                    kind,
                    governed,
                    after < len(tokens) and tokens[after].type in _LIST_OPEN,
                )
                lead = self.read_unit(unit)
                if (
                    lead is None
                    and carried is not None
                    and kind == "paragraph"
                    and unit.before_list
                    and unit.text.rstrip().endswith(":")
                    and not _NORMATIVE.search(unit.text)
                    and not _ID.search(unit.text)
                ):
                    # "And also:" right after a governed list introduces more
                    # of the same list.
                    lead = carried
                index += 2  # the inline and paragraph_close tokens
            index += 1

    def read_unit(self, unit: _Unit) -> str | None:
        """Check one unit. Return the level of its lead-in if a list follows."""
        number, text, kind = unit.line, unit.text, unit.kind
        for word in dict.fromkeys(m.group(1) for m in _NORMATIVE.finditer(text)):
            if word not in _USED:
                self.problem(
                    number,
                    f"{word} is not one of the normative words in spec/README.md; "
                    f"use {', '.join(_USED)}",
                )
        if kind == "heading":
            if _NORMATIVE.search(text):
                self.problem(number, "heading uses a normative word")
            for match in _ID.finditer(text):
                self.problem(number, f"{match.group(2)} is in a heading")
            return None

        lead_level: str | None = None
        start: int | None = None
        if kind in ("paragraph", "item", "continuation"):
            start = _last_clause(text)
            words = [m.group(1) for m in _NORMATIVE.finditer(text[start:])]
            colon = text.rstrip().endswith(":")
            if words and (colon or unit.before_list):
                lead_level = min((_LEVELS[w] for w in words), key=_STRENGTH.index)
                if not unit.before_list:
                    self.problem(number, "normative lead-in has no list after it")
                for match in _ID.finditer(text[start:]):
                    self.problem(
                        number,
                        f"{match.group(2)} is on a normative lead-in; "
                        "give the IDs to its list items",
                    )
            else:
                start = None
        body = text if start is None else text[:start]

        matches = list(_ID.finditer(body))
        if not matches:
            if _NORMATIVE.search(body):
                self.problem(number, "normative statement has no requirement ID")
            elif unit.governed is not None and lead_level is None:
                self.problem(number, "item under a normative lead-in has no ID")
        else:
            if _NORMATIVE.search(body[: matches[0].start()]):
                self.problem(number, "normative text before the first requirement ID")
            for index, match in enumerate(matches):
                end = matches[index + 1].start() if index + 1 < len(matches) else None
                self.read_requirement(
                    number, match, body[match.end() : end], unit.governed
                )
        return lead_level if unit.before_list else None

    def read_requirement(
        self, number: int, match: re.Match[str], statement: str, governed: str | None
    ) -> None:
        rid = match.group(2)
        withdrawn = match.group(1) is not None
        well_formed = _WELL_FORMED.fullmatch(rid)
        if not well_formed:
            self.problem(number, f"{rid} is malformed; use {self.area}-<n>")
            return
        if well_formed.group(1) != self.area:
            self.problem(number, f"{rid} uses another area; use {self.area}-<n>")
            return
        if rid in self.trace.requirements:
            first = self.trace.requirements[rid]
            self.problem(
                number, f"{rid} is already defined at {first.document}:{first.line}"
            )
            return
        level: str | None = None
        if withdrawn:
            # Text after the struck part is live text, not part of the withdrawal.
            tail = statement.split("~~", 1)[1] if "~~" in statement else ""
            if _NORMATIVE.search(tail):
                self.problem(
                    number,
                    f"normative text after withdrawn {rid} has no requirement ID",
                )
            if not _WITHDRAWN.search(statement):
                self.problem(
                    number,
                    f"{rid} is struck through but does not say "
                    '"Withdrawn in spec <version>: <reason>"',
                )
        else:
            words = [m.group(1) for m in _NORMATIVE.finditer(statement)]
            if len(words) > 1:
                self.problem(
                    number,
                    f"{rid} holds {len(words)} normative words; "
                    "give each requirement its own ID",
                )
            if words:
                level = _LEVELS[words[0]]
            elif governed is not None:
                level = governed
            else:
                self.problem(number, f"{rid} marks no normative statement")
        self.trace.requirements[rid] = Requirement(
            rid, self.name, number, level, withdrawn
        )


def read_spec(spec_dir: Path, trace: Trace) -> None:
    """Collect requirements from every numbered document in ``spec_dir``."""
    areas = read_areas(spec_dir / "README.md", trace)
    for path in sorted(spec_dir.rglob("*")):
        if path.suffix.lower() not in (".md", ".markdown") or not path.is_file():
            continue
        name = path.relative_to(spec_dir).as_posix()
        if name == "README.md":
            continue
        match = _DOCUMENT.fullmatch(name)
        if not match:
            trace.problems.append(
                f"{name}: not a top-level document named NN-lowercase-words.md"
            )
            continue
        number = match.group(1)
        if number in trace.documents:
            trace.problems.append(f"{name}: another document has number {number}")
            continue
        trace.documents.add(number)
        area = areas.get(number)
        if area is None:
            trace.problems.append(f"{name}: no ID area in the table in spec/README.md")
            continue
        text = path.read_text(encoding="utf-8")
        _DocumentReader(name, area, trace).read(text)
        if number == "06":
            _read_binds_others(name, text, trace)


def _cells(tokens: list[Token], start: int, close: str) -> tuple[list[str], int]:
    """The inline text of each cell from ``start`` up to the ``close`` token."""
    cells: list[str] = []
    index = start
    while tokens[index].type != close:
        if tokens[index].type == "inline":
            cells.append(tokens[index].content.strip())
        index += 1
    return cells, index


def _table(
    tokens: list[Token], start: int
) -> tuple[list[str], list[tuple[int, list[str]]]]:
    """The header cells and the (line, cells) of each body row of the table
    that opens at ``start``.
    """
    header: list[str] = []
    rows: list[tuple[int, list[str]]] = []
    index = start
    while tokens[index].type != "table_close":
        token = tokens[index]
        if token.type == "thead_open":
            header, index = _cells(tokens, index, "thead_close")
        elif token.type == "tr_open":
            cells, index = _cells(tokens, index, "tr_close")
            rows.append((_line(token), cells))
        index += 1
    return header, rows


def _read_binds_others(name: str, text: str, trace: Trace) -> None:
    """Collect the rows of spec 06's table of requirements that bind others.

    Whether each ID is a live must-level requirement is checked once every
    document has been read.
    """
    tokens = _MARKDOWN.parse(text)
    headings = [
        index
        for index, token in enumerate(tokens)
        if token.type == "heading_open"
        and tokens[index + 1].content.strip() == _BINDS_OTHERS
    ]
    where = f'under "{_BINDS_OTHERS}"'
    for extra in headings[1:]:
        trace.problems.append(
            f'{name}:{_line(tokens[extra])}: a second heading "{_BINDS_OTHERS}"'
        )
    if not headings:
        return
    heading = headings[0]
    line = _line(tokens[heading])
    index = heading + 3  # past the heading's inline and close tokens
    while index < len(tokens) and tokens[index].type not in (
        "table_open",
        "heading_open",
    ):
        index += 1
    if index == len(tokens) or tokens[index].type != "table_open":
        trace.problems.append(f"{name}:{line}: no table {where}")
        return
    header, rows = _table(tokens, index)
    if "ID" not in header or "Binds" not in header:
        trace.problems.append(
            f"{name}:{_line(tokens[index])}: the table {where} needs the columns "
            "ID and Binds"
        )
        return
    id_column, binds_column = header.index("ID"), header.index("Binds")
    seen: set[str] = set()
    for row_line, cells in rows:
        cells += [""] * (len(header) - len(cells))
        cell = cells[id_column]
        rid = cell[1:-1] if len(cell) > 2 and cell[0] == cell[-1] == "`" else cell
        if not _WELL_FORMED.fullmatch(rid):
            trace.problems.append(
                f"{name}:{row_line}: {_shown(cell)} {where} is not a requirement ID"
            )
            continue
        if rid in seen:
            trace.problems.append(f"{name}:{row_line}: {rid} is listed twice {where}")
            continue
        seen.add(rid)
        if not cells[binds_column]:
            trace.problems.append(
                f"{name}:{row_line}: {rid} {where} does not say whom it binds"
            )
            continue
        trace.rows.append(_Row(name, row_line, rid))


def read_cases(spec_dir: Path, trace: Trace) -> None:
    """Read and check the case files in ``spec/conformance/cases/``."""
    folder = spec_dir / CASES
    if not folder.is_dir():
        if folder.exists() or folder.is_symlink():
            trace.problems.append(f"{CASES}: is not a directory")
        return
    try:
        paths = sorted(folder.iterdir())
    except OSError as error:
        trace.problems.append(f"{CASES}: cannot be read: {error.strerror or error}")
        return
    for path in paths:
        label = f"{CASES}/{path.name}"
        match = _CASE_FILE.fullmatch(path.name)
        if match is None or not path.is_file():
            trace.problems.append(
                f"{label}: a case file is named <part>.json or <part>-<topic>.json, "
                f"where <part> is {', '.join(_PARTS[:-1])} or {_PARTS[-1]}"
            )
            continue
        _read_case_file(label, match.group(1), path, trace)


class _Members(dict[str, Any]):
    """A JSON object that remembers the members it was given twice."""

    repeated: list[str]


def _members(pairs: list[tuple[str, Any]]) -> _Members:
    members = _Members()
    members.repeated = []
    for key, value in pairs:
        if key in members:
            members.repeated.append(key)
        members[key] = value
    return members


def _once(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """A JSON object in which no member is given twice."""
    members = dict(pairs)
    if len(members) < len(pairs):
        repeated = sorted(k for k in members if [p[0] for p in pairs].count(k) > 1)
        raise ValueError(f"{_shown(repeated[0])} is given twice in one object")
    return members


def _read_case_file(label: str, part: str, path: Path, trace: Trace) -> None:
    try:
        cases = json.loads(
            path.read_bytes().decode("utf-8"), object_pairs_hook=_members
        )
    except (OSError, ValueError, RecursionError) as error:
        trace.problems.append(f"{label}: cannot be read: {error}")
        return
    if not isinstance(cases, list):
        trace.problems.append(f"{label}: is not a JSON array of cases")
        return
    for number, case in enumerate(cases, start=1):
        _read_case(_CaseProblems(trace, label, number), part, case)


@dataclass(frozen=True)
class _CaseProblems:
    trace: Trace
    file: str
    number: int  # counted from 1

    @property
    def where(self) -> str:
        return f"{self.file}: case {self.number}"

    def __call__(self, member: str, reason: str) -> None:
        self.trace.problems.append(f"{self.where}: {member}: {reason}")


def _read_case(problem: _CaseProblems, part: str, case: object) -> None:
    """Check one case of a case file, and list it under its name."""
    trace = problem.trace
    if not isinstance(case, _Members):
        trace.problems.append(f"{problem.where}: is not an object")
        return
    for member in dict.fromkeys(case.repeated):
        problem(member if member in _CASE_MEMBERS else _shown(member), "is given twice")
    for member in sorted(case.keys() - set(_CASE_MEMBERS)):
        problem(_shown(member), "is not a member of a case")
    for member in _CASE_MEMBERS:
        if member not in case:
            problem(member, "is missing")
    name = case.get("name")
    if "name" in case and not (isinstance(name, str) and _CASE_NAME.fullmatch(name)):
        problem("name", f"{_shown(name)} is not [a-z][a-z0-9_]*")
        name = None
    if "part" in case and case["part"] != part:
        problem("part", f"{_shown(case['part'])} is not {part}, the file's part")
    for member in ("state", "procedure", "expected"):
        if member in case and not isinstance(case[member], str):
            problem(member, "is not a string")
    requirements = (
        _case_requirements(case["requirements"], problem)
        if "requirements" in case
        else ()
    )
    if not isinstance(name, str):
        return
    label = problem.file
    first = trace.cases.get(name)
    if first is None:
        trace.cases[name] = Case(name, label, requirements)
    elif first.file == label:
        trace.problems.append(f"{name}: listed twice in {label}")
    else:
        trace.problems.append(f"{name}: listed in {first.file} and {label}")


def _case_requirements(value: object, problem: _CaseProblems) -> tuple[str, ...]:
    """The live IDs a case lists; a problem for anything else."""
    if not isinstance(value, list) or not value:
        problem("requirements", "is not a non-empty list of requirement IDs")
        return ()
    ids: list[str] = []
    for rid in value:
        requirement = (
            problem.trace.requirements.get(rid) if isinstance(rid, str) else None
        )
        if requirement is None or requirement.withdrawn:
            problem("requirements", f"{_shown(rid)} is not a live requirement")
        elif rid in ids:
            problem("requirements", f"{rid} is listed twice")
        else:
            ids.append(rid)
    return tuple(ids)


# Settings, the test report and cross-checks


def read_settings(path: Path, trace: Trace) -> list[str]:
    """Read ``tests/spec_trace.toml``: the enforced documents and [not-tested]."""
    if not path.is_file():
        return []
    try:
        settings = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        trace.problems.append(f"{SETTINGS}: {error}")
        return []
    for key in sorted(settings.keys() - _SETTINGS_KEYS):
        trace.problems.append(f"{SETTINGS}: unknown key {key!r}")
    enforce = settings.get("enforce", [])
    if not isinstance(enforce, list) or not all(isinstance(n, str) for n in enforce):
        trace.problems.append(f"{SETTINGS}: enforce must be a list of strings")
        enforce = []
    not_tested = settings.get("not-tested", {})
    if not isinstance(not_tested, dict):
        trace.problems.append(f"{SETTINGS}: [not-tested] must be a table")
        not_tested = {}
    for rid, reason in not_tested.items():
        if not isinstance(reason, str) or not reason.strip():
            trace.problems.append(f"{SETTINGS}: [not-tested] {rid} needs a reason")
        else:
            trace.not_tested[rid] = reason
    not_implemented = settings.get("not-implemented", {})
    if not isinstance(not_implemented, dict):
        trace.problems.append(f"{SETTINGS}: [not-implemented] must be a table")
        not_implemented = {}
    for rid, reason in not_implemented.items():
        if not isinstance(reason, str) or not reason.strip():
            trace.problems.append(
                f"{SETTINGS}: [not-implemented] {rid}: needs a reason"
            )
        else:
            trace.not_implemented[rid] = reason
    return enforce


def _environment(value: object) -> dict[str, Any] | None:
    """The report's ``environment``, or None if it is not as the plugin writes it."""
    if not isinstance(value, dict) or value.keys() != _ENVIRONMENT_MEMBERS:
        return None
    packages = value["packages"]
    if (
        not isinstance(value["python"], str)
        or not isinstance(value["architecture"], str)
        or not isinstance(packages, dict)
        or packages.keys() != _PACKAGES
        or not all(v is None or isinstance(v, str) for v in packages.values())
    ):
        return None
    return value


def _ran(entry: object) -> Ran | None:
    """A test's entry, or None if it is not as the plugin writes it."""
    if not isinstance(entry, dict) or not (
        {"ids", "outcome"} <= entry.keys() <= _ENTRY_MEMBERS
    ):
        return None
    ids, outcome = entry["ids"], entry["outcome"]
    case = entry.get("case")
    outcomes = entry.get("transport_outcomes")
    if (
        not isinstance(ids, list)
        or not all(isinstance(rid, str) for rid in ids)
        or outcome not in _OUTCOMES
        or ("case" in entry and not isinstance(case, str))
        or (
            "transport_outcomes" in entry
            and not (
                isinstance(outcomes, dict)
                and outcomes
                and all(isinstance(v, str) for v in outcomes.values())
            )
        )
    ):
        return None
    return Ran(tuple(ids), outcome, case, outcomes)


def read_report(path: Path, trace: Trace) -> None:
    """Read the report the pytest plugin wrote."""
    where = f"report {path.name}"
    try:
        report = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_once)
    except (OSError, ValueError, RecursionError) as error:
        trace.problems.append(f"{where}: cannot be read: {error}")
        return
    if not isinstance(report, dict):
        trace.problems.append(f"{where}: not a format {REPORT_FORMAT} spec report")
        return
    number = report.get("format")
    if type(number) is not int or number != REPORT_FORMAT:
        trace.problems.append(
            f"{path}: report format {_shown(number)}; "
            f"this checker reads {REPORT_FORMAT}"
        )
        return
    environment = _environment(report.get("environment"))
    tests = report.get("tests")
    if (
        report.keys() != _REPORT_MEMBERS
        or environment is None
        or not isinstance(tests, dict)
    ):
        trace.problems.append(f"{where}: not a format {REPORT_FORMAT} spec report")
        return
    read: dict[str, Ran] = {}
    for test, entry in tests.items():
        ran = _ran(entry)
        if ran is None:
            trace.problems.append(f"{where}: malformed entry for {test}")
        else:
            read[test] = ran
    if len(read) < len(tests):
        # A test the checker cannot read may have failed: nothing is counted.
        return
    trace.environment = environment
    trace.tests = read
    for test, ran in read.items():
        trace.citations.extend(Citation(rid, test, ran.outcome) for rid in ran.ids)
    trace.measured = True


def check(
    root: Path,
    enforce: Sequence[str] = (),
    report: Path | None = None,
    *,
    conformance: bool = False,
) -> Trace:
    """Check spec/ and the settings under ``root``, and coverage from ``report``.

    With ``conformance``, also build the conformance run into ``trace.run``
    once the report has been read.
    """
    trace = Trace()
    read_spec(root / "spec", trace)
    read_cases(root / "spec", trace)
    enforced: set[str] = set()
    for number in [*read_settings(root / SETTINGS, trace), *enforce]:
        if number in trace.documents:
            enforced.add(number)
        else:
            trace.problems.append(
                f"enforce: {number!r} is not the two-digit number of a document "
                "in spec/"
            )
    for row in trace.rows:
        requirement = trace.requirements.get(row.rid)
        if requirement is None or requirement.withdrawn or requirement.level != "MUST":
            trace.problems.append(
                f'{row.document}:{row.line}: {row.rid} under "{_BINDS_OTHERS}" is '
                "not a live must-level requirement"
            )
        else:
            trace.binds_others.add(row.rid)
    if report is not None:
        read_report(report, trace)
    elif enforced:
        trace.problems.append(
            "enforce: coverage cannot be checked without --report; run pytest with "
            "--spec-report first"
        )
    for citation in trace.citations:
        requirement = trace.requirements.get(citation.rid)
        if requirement is None:
            trace.problems.append(
                f"{citation.test}: cites unknown requirement {citation.rid}"
            )
        elif requirement.withdrawn:
            trace.problems.append(
                f"{citation.test}: cites withdrawn requirement {citation.rid}"
            )
    refused: list[tuple[dict[str, str], str]] = []
    for rid in trace.not_tested:
        requirement = trace.requirements.get(rid)
        if requirement is None or requirement.withdrawn:
            reason = "is not a live requirement"
        elif requirement.level != "MUST":
            reason = "is not must-level, so it needs no entry"
        elif trace.covering(rid):
            reason = "is covered by a passing test; remove the entry"
        else:
            continue
        trace.problems.append(f"{SETTINGS}: [not-tested] {rid} {reason}")
        refused.append((trace.not_tested, rid))
    for rid in trace.not_implemented:
        requirement = trace.requirements.get(rid)
        if requirement is None or requirement.withdrawn:
            reason = "is not a live requirement"
        elif requirement.level == "MUST":
            reason = (
                "is a MUST; only SHOULD and MAY requirements may be left unimplemented"
            )
        elif trace.covering(rid):
            reason = "is covered by a passing test; remove the entry"
        else:
            continue
        trace.problems.append(f"{SETTINGS}: [not-implemented] {rid}: {reason}")
        refused.append((trace.not_implemented, rid))
    # A refused entry gives no status in the run; the other table's entry for
    # the same ID stands on its own.
    for table, rid in refused:
        del table[rid]
    if not trace.measured:
        return trace
    for requirement in trace.requirements.values():
        if (
            requirement.document[:2] in enforced
            and requirement.level == "MUST"
            and not requirement.withdrawn
            and requirement.rid not in trace.not_tested
            and not trace.covering(requirement.rid)
        ):
            trace.problems.append(
                f"{requirement.document}:{requirement.line}: {requirement.rid} "
                "is must-level and no passing test cites it"
            )
    _check_markers(trace)
    if conformance:
        trace.run = conformance_run(root, trace)
    return trace


def _check_markers(trace: Trace) -> None:
    """Hold the ``conformance_case`` markers and the case files in step."""
    run: set[str] = set()
    for test, ran in trace.tests.items():
        if ran.case is None:
            continue
        case = trace.cases.get(ran.case)
        if case is None:
            name = ran.case if _CASE_NAME.fullmatch(ran.case) else _shown(ran.case)
            trace.problems.append(f"{test}: case {name} is not listed")
            continue
        run.add(case.name)
        for rid in case.requirements:
            if rid not in ran.ids:
                trace.problems.append(
                    f"{test}: case {case.name} lists {rid}, which the test does "
                    "not cite"
                )
    for name in trace.cases:
        if name not in run:
            trace.problems.append(f"case {name}: no test runs it")


# The conformance run


def _digest(root: Path, folder: Path, problems: list[str]) -> str | None:
    """SHA-256 over each file under ``folder``: its path from ``root``, its
    size and its bytes.

    None, with a problem, if a file or a directory cannot be read.
    """
    files = _files(root, folder, problems)
    if files is None:
        return None
    digest = hashlib.sha256()
    named = sorted(
        (os.fsencode(path.relative_to(root).as_posix()), path) for path in files
    )
    for name, path in named:
        try:
            data = path.read_bytes()
        except OSError as error:
            problems.append(_unreadable(name, error))
            return None
        digest.update(name + b"\0" + str(len(data)).encode() + b"\0")
        digest.update(data)
    return digest.hexdigest()


def _unreadable(name: bytes, error: OSError) -> str:
    shown = name.decode("utf-8", "backslashreplace")
    return f"{shown}: cannot be read for the suite's hash: {error.strerror or error}"


def _files(root: Path, folder: Path, problems: list[str]) -> list[Path] | None:
    """Every file under ``folder``, apart from ``__pycache__`` directories;
    None, with a problem, if a directory cannot be read.

    Links are followed, so a directory reached by two names is read under
    both. A link back to a directory that holds it would never end, so it is
    left out, whatever order the directories are listed in.
    """
    found: list[Path] = []

    def visit(directory: Path, ancestors: frozenset[str]) -> bool:
        try:
            with os.scandir(directory) as listing:
                entries = list(listing)
        except OSError as error:
            name = os.fsencode(directory.relative_to(root).as_posix())
            problems.append(_unreadable(name, error))
            return False
        for entry in entries:
            path = Path(entry.path)
            if entry.is_dir():
                real = os.path.realpath(path)
                if entry.name == "__pycache__" or real in ancestors:
                    continue
                if not visit(path, ancestors | {real}):
                    return False
            elif entry.is_file():
                found.append(path)
        return True

    if not folder.is_dir():
        return []
    return found if visit(folder, frozenset({os.path.realpath(folder)})) else None


def suite(root: Path, problems: list[str] | None = None) -> dict[str, str | None]:
    """The hashes that name the suite a run passed.

    A hash that cannot be computed is None, with a problem.
    """
    problems = [] if problems is None else problems
    path = root / "spec" / REFERENCE
    reference: str | None = None
    if path.is_file():
        try:
            reference = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as error:
            problems.append(
                f"spec/{REFERENCE}: cannot be read for the suite's hash: "
                f"{error.strerror or error}"
            )
    return {
        "cases": _digest(root, root / "spec" / CASES, problems),
        "reference": reference,
        "tests": _digest(root, root / "tests", problems),
    }


def read_version(readme: Path, trace: Trace) -> tuple[str | None, set[str] | None]:
    """The specification version in the README's *Status* line, and the
    documents its table marks as part of that version; None for what cannot
    be read, which is a problem.
    """
    text = readme.read_text(encoding="utf-8")
    status = _STATUS_LINE.search(text)
    version = _VERSION.search(status.group(0)) if status else None
    if version is None:
        trace.problems.append(
            "README.md: the Status line names no specification version"
        )
        return None, None
    column = f"In {version.group(1)}"
    tokens = _MARKDOWN.parse(text)
    for index, token in enumerate(tokens):
        if token.type != "table_open":
            continue
        header, rows = _table(tokens, index)
        if "#" not in header or column not in header:
            continue
        number, marked = header.index("#"), header.index(column)
        documents: set[str] | None = set()
        for line, cells in rows:
            cells += [""] * (len(header) - len(cells))
            value = cells[marked]
            if value not in _IN_VERSION:
                trace.problems.append(
                    f"README.md:{line}: {_shown(value)} in the {column} column; "
                    "write yes, no or outline only"
                )
                documents = None
            elif _IN_VERSION[value] and documents is not None:
                documents.add(cells[number])
        return version.group(1), documents
    trace.problems.append(f'README.md: no table of documents with an "{column}" column')
    return version.group(1), None


def status(trace: Trace, rid: str) -> tuple[str, str | None]:
    """A requirement's status in the run, and the reason it carries, if any."""
    outcomes = {c.outcome for c in trace.citations if c.rid == rid}
    if outcomes & set(_FAILED):
        return "failed", None
    if "passed" in outcomes:
        return "passed", None
    if rid in trace.binds_others:
        return "not-applicable", None
    if rid in trace.not_tested:
        return "not-tested", trace.not_tested[rid]
    if rid in trace.not_implemented:
        return "not-implemented", trace.not_implemented[rid]
    return "uncovered", None


def conformance_run(root: Path, trace: Trace) -> dict[str, Any]:
    """The conformance run of a measured trace (ADR 0032, Decision 9)."""
    version, in_version = read_version(root / "spec" / "README.md", trace)
    environment = trace.environment or {}
    citing: dict[str, list[dict[str, Any]]] = {}
    for test, ran in sorted(trace.tests.items()):
        for rid in dict.fromkeys(ran.ids):
            entry: dict[str, Any] = {"test": test, "outcome": ran.outcome}
            if ran.transport_outcomes is not None:
                entry["transport_outcomes"] = ran.transport_outcomes
            citing.setdefault(rid, []).append(entry)
    requirements: dict[str, dict[str, Any]] = {}
    if in_version is None:
        trace.nonconforming.append("the specification's version or documents")
    for requirement in trace.requirements.values():
        if requirement.withdrawn:
            continue
        state, reason = status(trace, requirement.rid)
        number = requirement.document[:2]
        record: dict[str, Any] = {
            "document": number,
            "level": requirement.level,
            "status": state,
        }
        if reason is not None:
            record["reason"] = reason
        record["tests"] = citing.get(requirement.rid, [])
        requirements[requirement.rid] = record
        if state == "failed" or (
            requirement.level == "MUST"
            and (in_version is None or number in in_version)
            and state not in ("passed", "not-applicable")
        ):
            note = f": {reason}" if reason is not None else ""
            trace.nonconforming.append(f"{requirement.rid} {state}{note}")
    return {
        "format": RUN_FORMAT,
        "spec": version,
        "suite": suite(root, trace.problems),
        "implementation": {
            "name": "django-neolith",
            "version": environment.get("packages", {}).get("django-neolith"),
        },
        "environment": environment,
        "requirements": requirements,
        "conforms": not trace.nonconforming,
    }


def render(trace: Trace) -> str:
    """The report printed by the command line."""
    lines = []
    for requirement in trace.requirements.values():
        level = "withdrawn" if requirement.withdrawn else requirement.level
        count = str(len(trace.covering(requirement.rid))) if trace.measured else "?"
        note = "  not tested" if requirement.rid in trace.not_tested else ""
        lines.append(
            f"{requirement.rid:<8} {level or '-':<9} {count:>3}  "
            f"{requirement.document}:{requirement.line}{note}"
        )
    live = [r for r in trace.requirements.values() if not r.withdrawn]
    covered = (
        f"{sum(1 for r in live if trace.covering(r.rid))} covered by passing tests"
        if trace.measured
        else "coverage not measured (no --report)"
    )
    lines.append(
        f"{len(live)} requirements, {covered}, "
        f"{len(trace.not_tested)} marked not tested, {len(trace.problems)} problems"
    )
    if trace.run is not None:
        lines.append(f"conformance run: conforms {str(trace.run['conforms']).lower()}")
        lines.extend(f"does not conform: {why}" for why in trace.nonconforming)
    lines.extend(f"problem: {problem}" for problem in trace.problems)
    return "\n".join(lines)


def _expanded(value: str) -> Path:
    """A path with ``~`` and environment variables expanded, as the plugin does."""
    return Path(os.path.expanduser(os.path.expandvars(value)))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--report",
        type=_expanded,
        metavar="PATH",
        help="the report written by pytest --spec-report",
    )
    parser.add_argument(
        "--enforce",
        nargs="+",
        default=[],
        metavar="NN",
        help="also enforce coverage for these document numbers",
    )
    parser.add_argument(
        "--conformance",
        metavar="PATH",
        help="write the conformance run here (needs --report)",
    )
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    run_path: Path | None = None
    if args.conformance is not None:
        if args.report is None:
            parser.error("--conformance needs --report")
        run_path = _emptied(parser, args.conformance)
    trace = check(
        args.root, args.enforce, args.report, conformance=run_path is not None
    )
    if run_path is not None and trace.run is not None:
        try:
            run_path.write_text(
                json.dumps(trace.run, indent=1) + "\n", encoding="utf-8"
            )
        except OSError as error:
            parser.error(
                f"--conformance cannot write {run_path}: {error.strerror or error}"
            )
    # Test ids and file names come from outside; a lone surrogate in one is
    # printed escaped.
    print(render(trace).encode("utf-8", "backslashreplace").decode("utf-8"))
    return 1 if trace.problems else 0


def _emptied(parser: argparse.ArgumentParser, value: str) -> Path:
    """The run's absolute path, emptied, as the plugin empties its report."""
    expanded = os.path.expanduser(os.path.expandvars(value))
    if os.path.basename(expanded) in ("", ".", ".."):
        parser.error(f"--conformance needs a file path, not {value!r}")
    path = Path(expanded).absolute()
    try:
        path.write_text("", encoding="utf-8")
    except OSError as error:
        parser.error(f"--conformance cannot write {path}: {error.strerror or error}")
    return path


if __name__ == "__main__":
    sys.exit(main())
