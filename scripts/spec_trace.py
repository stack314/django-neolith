"""Check requirement IDs in spec/ and the coverage tests report (ADR 0018).

The script reads the specification and ``tests/spec_trace.toml`` statically.
Coverage comes from the report that ``scripts/spec_trace_plugin.py`` writes
when pytest runs with ``--spec-report PATH``: only a test that ran and passed
covers the requirements it cites. The script needs only the standard library.

The specification is read as CommonMark blocks, not physical lines: a wrapped
paragraph or list item is one statement, and an indented paragraph belongs to
the list item above it. Fenced code (``` or ~~~, closed by a fence of the same
character at least as long) is a sketch and is skipped.

A normative lead-in is the last sentence or clause of a block (after the last
".", "!", "?" or ";") when it uses a normative word and either ends in ":" or
is a paragraph followed directly by a list. Each item of its list is a
requirement with its own ID, and the lead-in carries none.

It always reports a problem, and exits 1, when:

- the text uses an RFC 2119 word that spec/README.md does not list, such as
  SHALL;
- normative text carries no requirement ID of its own: a statement or table
  row without one, text before the first ID in a block, a heading, or an item
  in the list of a normative lead-in;
- a heading carries an ID;
- a normative lead-in carries an ID, or ends in ":" with no list after it;
- a code fence is never closed;
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
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

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
_LIST_ITEM = re.compile(r"\s*(?:[-*+]|\d{1,9}[.)])(?:\s|$)")
_HEADING = re.compile(r" {0,3}#{1,6}(?:\s|$)")
_FENCE_OPEN = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
_FENCE_CLOSE = re.compile(r" {0,3}(`{3,}|~{3,})\s*")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?;])\s+")
_DOCUMENT = re.compile(r"(\d{2})-[a-z0-9-]+\.md")
_AREA_ROW = re.compile(r"\|\s*(\d{2})\s*\|.*\|\s*`([A-Z]{2,5})`\s*\|\s*$")
_SETTINGS_KEYS = {"enforce", "not-tested"}


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


@dataclass(frozen=True)
class _Block:
    line: int  # first physical line
    text: str  # physical lines joined by spaces
    # "item", "continuation", "paragraph", "row", "heading" or "unclosed fence"
    kind: str
    indent: int = 0  # leading spaces of the first line


def _blocks(text: str) -> Iterator[_Block]:
    """Split Markdown into the blocks a statement can span.

    A list item runs until the next item or blank line, so wrapped lines join
    it. A paragraph indented under a list item is a continuation of that item.
    Table rows and headings are blocks of their own. Fenced code is skipped,
    and a fence still open at the end is yielded as "unclosed fence".
    """
    fence: tuple[str, int, int] | None = None  # character, length, line
    current: list[str] = []
    start = 0
    kind = "paragraph"
    in_list = False
    after_blank = True

    def flush() -> Iterator[_Block]:
        if current:
            first = current[0].expandtabs(4)
            indent = len(first) - len(first.lstrip())
            yield _Block(start, " ".join(s.strip() for s in current), kind, indent)
            current.clear()

    for number, line in enumerate(text.splitlines(), start=1):
        if fence is not None:
            closing = _FENCE_CLOSE.fullmatch(line)
            if (
                closing
                and closing.group(1)[0] == fence[0]
                and len(closing.group(1)) >= fence[1]
            ):
                fence = None
                after_blank = True
            continue
        opening = _FENCE_OPEN.fullmatch(line)
        if opening and not (opening.group(1)[0] == "`" and "`" in opening.group(2)):
            yield from flush()
            fence = (opening.group(1)[0], len(opening.group(1)), number)
            continue
        if not line.strip():
            yield from flush()
            after_blank = True
            continue
        if _HEADING.match(line) or line.lstrip().startswith("|"):
            yield from flush()
            is_heading = bool(_HEADING.match(line))
            yield _Block(number, line.strip(), "heading" if is_heading else "row")
            in_list = in_list and not is_heading
            after_blank = False
            continue
        if _LIST_ITEM.match(line):
            yield from flush()
            start, kind, in_list = number, "item", True
        elif current and not after_blank:
            pass  # a wrapped line of the current block
        else:
            yield from flush()
            indented = line[:1].isspace()
            start = number
            kind = "continuation" if indented and in_list else "paragraph"
            in_list = in_list and indented
        current.append(line)
        after_blank = False
    yield from flush()
    if fence is not None:
        yield _Block(fence[2], "", "unclosed fence")


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
        # The level of the current normative lead-in, while its list lasts,
        # and the indent its items must exceed (-1 for a paragraph lead-in).
        self.lead_in: str | None = None
        self.lead_in_indent = -1

    def problem(self, number: int, text: str) -> None:
        self.trace.problems.append(f"{self.name}:{number}: {text}")

    def read(self, text: str) -> None:
        blocks = list(_blocks(text))
        for index, block in enumerate(blocks):
            following = blocks[index + 1].kind if index + 1 < len(blocks) else None
            self.read_block(block, following)

    def lead_in_start(self, block: _Block, following: str | None) -> int | None:
        """Where the block's normative lead-in starts, if it has one."""
        if block.kind not in ("paragraph", "continuation", "item"):
            return None
        start = _last_clause(block.text)
        if not _NORMATIVE.search(block.text[start:]):
            return None
        colon = block.text.rstrip().endswith(":")
        before_list = block.kind == "paragraph" and following == "item"
        if not (colon or before_list):
            return None
        if colon and following != "item":
            self.problem(block.line, "normative lead-in has no list after it")
        return start

    def read_block(self, block: _Block, following: str | None) -> None:
        number, text, kind = block.line, block.text, block.kind
        if kind == "unclosed fence":
            self.problem(number, "code fence is never closed")
            return
        for word in dict.fromkeys(m.group(1) for m in _NORMATIVE.finditer(text)):
            if word not in _USED:
                self.problem(
                    number,
                    f"{word} is not one of the normative words in spec/README.md; "
                    f"use {', '.join(_USED)}",
                )
        if kind in ("paragraph", "heading"):
            self.lead_in = None
        if kind == "heading":
            if _NORMATIVE.search(text):
                self.problem(number, "heading uses a normative word")
            for match in _ID.finditer(text):
                self.problem(number, f"{match.group(2)} is in a heading")
            return
        if kind == "item" and block.indent <= self.lead_in_indent:
            self.lead_in = None  # a sibling of the item that led in
        under_lead_in = kind == "item" and self.lead_in is not None

        lead = self.lead_in_start(block, following)
        body = text if lead is None else text[:lead]
        if lead is not None:
            for match in _ID.finditer(text[lead:]):
                self.problem(
                    number,
                    f"{match.group(2)} is on a normative lead-in; "
                    "give the IDs to its list items",
                )
            words = [m.group(1) for m in _NORMATIVE.finditer(text[lead:])]
            self.lead_in = min((_LEVELS[w] for w in words), key=_STRENGTH.index)
            self.lead_in_indent = block.indent if kind == "item" else -1

        matches = list(_ID.finditer(body))
        if not matches:
            if _NORMATIVE.search(body):
                self.problem(number, "normative statement has no requirement ID")
            elif under_lead_in and lead is None:
                self.problem(number, "item under a normative lead-in has no ID")
            return
        if _NORMATIVE.search(body[: matches[0].start()]):
            self.problem(number, "normative text before the first requirement ID")
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else None
            self.read_requirement(number, match, body[match.end() : end], under_lead_in)

    def read_requirement(
        self, number: int, match: re.Match[str], statement: str, under_lead_in: bool
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
            elif under_lead_in:
                level = self.lead_in
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
