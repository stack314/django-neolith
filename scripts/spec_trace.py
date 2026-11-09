"""Check requirement IDs in spec/ and the coverage tests report (ADR 0018).

The script reads the specification and ``tests/spec_trace.toml``. Coverage
comes from the report that ``scripts/spec_trace_plugin.py`` writes when pytest
runs with ``--spec-report PATH``: only a test that ran and passed covers the
requirements it cites.

The specification is parsed with markdown-it-py, a CommonMark parser, with
GFM tables and strikethrough enabled. The checker walks its token stream:
paragraphs, list items, headings, table rows and HTML blocks are prose; fenced
and indented code are sketches and are skipped. A wrapped paragraph or list
item is one statement. markdown-it-py is a development dependency only.

A normative lead-in is the last sentence or clause of a paragraph (after the
last ".", "!", "?" or ";") when it uses a normative word and either ends in
":" or is followed directly by a list. Each item of that list is a
requirement with its own ID, and the lead-in carries none.

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
  document that does not exist, or has a ``[not-tested]`` entry that is not a
  live, must-level requirement with a reason.

With ``--report``, it also reports a test that cites an unknown or withdrawn
ID, and a ``[not-tested]`` entry for a requirement a passing test covers. For
the enforced documents (``enforce`` in the settings, plus ``--enforce``),
every must-level requirement (MUST, MUST NOT, REQUIRED) needs a passing test
that cites it, or a ``[not-tested]`` entry. Enforcing without ``--report`` is
a problem, so the gate cannot be skipped by leaving the report out.

Usage::

    python -m pytest --spec-report spec-report.json
    python scripts/spec_trace.py --report spec-report.json
    python scripts/spec_trace.py              # the specification only
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from markdown_it import MarkdownIt
from markdown_it.token import Token

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = "tests/spec_trace.toml"
REPORT_FORMAT = 1

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
_SETTINGS_KEYS = {"enforce", "not-tested"}
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


@dataclass
class Trace:
    requirements: dict[str, Requirement] = field(default_factory=dict)
    citations: list[Citation] = field(default_factory=list)
    not_tested: dict[str, str] = field(default_factory=dict)
    documents: set[str] = field(default_factory=set)
    measured: bool = False
    problems: list[str] = field(default_factory=list)

    def covering(self, rid: str) -> list[Citation]:
        """Passing tests that cite ``rid``."""
        return [c for c in self.citations if c.rid == rid and c.outcome == "passed"]


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
    # True for a paragraph followed directly by a list.
    before_list: bool = False


@dataclass
class _List:
    lead: str | None  # the level of the lead-in that introduced it, if any


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
        lead: str | None = None  # from a lead-in, for the list right after it
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token.type in _LIST_OPEN:
                stack.append(_List(lead))
                lead = None
            elif token.type in _LIST_CLOSE:
                stack.pop()
            elif token.type == "list_item_open":
                parent = stack[-1]
                if isinstance(parent, _List):
                    stack.append(_Item(_line(token), parent))
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
                after = tokens[index + 3] if index + 3 < len(tokens) else None
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
                    after is not None and after.type in _LIST_OPEN,
                )
                lead = self.read_unit(unit)
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
        _DocumentReader(name, area, trace).read(path.read_text(encoding="utf-8"))


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
    return enforce


def read_report(path: Path, trace: Trace) -> None:
    """Read the report the pytest plugin wrote."""
    where = f"report {path.name}"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        trace.problems.append(f"{where}: cannot be read: {error}")
        return
    if not isinstance(report, dict) or report.get("format") != REPORT_FORMAT:
        trace.problems.append(f"{where}: not a format {REPORT_FORMAT} spec report")
        return
    tests = report.get("tests")
    if not isinstance(tests, dict):
        trace.problems.append(f"{where}: not a format {REPORT_FORMAT} spec report")
        return
    for test, entry in tests.items():
        ids = entry.get("ids") if isinstance(entry, dict) else None
        outcome = entry.get("outcome") if isinstance(entry, dict) else None
        if not isinstance(ids, list) or not isinstance(outcome, str):
            trace.problems.append(f"{where}: malformed entry for {test}")
            continue
        trace.citations.extend(Citation(str(rid), test, outcome) for rid in ids)
    trace.measured = True


def check(root: Path, enforce: Sequence[str] = (), report: Path | None = None) -> Trace:
    """Check spec/ and the settings under ``root``, and coverage from ``report``."""
    trace = Trace()
    read_spec(root / "spec", trace)
    enforced: set[str] = set()
    for number in [*read_settings(root / SETTINGS, trace), *enforce]:
        if number in trace.documents:
            enforced.add(number)
        else:
            trace.problems.append(
                f"enforce: {number!r} is not the two-digit number of a document "
                "in spec/"
            )
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
    return trace


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
    lines.extend(f"problem: {problem}" for problem in trace.problems)
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--report",
        type=Path,
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
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    trace = check(args.root, args.enforce, args.report)
    print(render(trace))
    return 1 if trace.problems else 0


if __name__ == "__main__":
    sys.exit(main())
