"""Check requirement IDs in spec/ and the tests that cite them (ADR 0018).

The script reads the specification, the test sources and
``tests/spec_trace.toml``. It imports neither the package nor the tests, and
it needs only the standard library.

The specification is read as Markdown blocks, not physical lines: a wrapped
paragraph or list item is one statement, and an indented paragraph belongs to
the list item above it. Fenced code blocks are sketches and are skipped.

It always reports a problem, and exits 1, when:

- the text uses an RFC 2119 word that spec/README.md does not list, such as
  SHALL;
- normative text carries no requirement ID of its own: a statement or table
  row without one, text before the first ID in a block, a heading, or an item
  in a list introduced by a normative lead-in such as "A transport MUST:";
- a normative lead-in carries an ID (its items carry them), or is not
  followed by a list;
- an ID is malformed, uses another document's area or is defined twice;
- an ID marks no normative word, or more than one;
- a withdrawn requirement does not say "Withdrawn in spec <version>: <reason>";
- spec/ holds a document that is not numbered, or whose number or area is
  missing from or repeated in the table in spec/README.md;
- a test cites an ID that does not exist or has been withdrawn;
- the ``spec`` marker is used other than as a direct call with string literals;
- ``tests/spec_trace.toml`` cannot be read, has an unknown key, enforces a
  document that does not exist, or has a ``[not-tested]`` entry that is not a
  live, must-level, uncited requirement with a reason.

For the enforced documents (``enforce`` in the settings, plus ``--enforce``),
every must-level requirement (MUST, MUST NOT, REQUIRED) also needs a
citing test or a ``[not-tested]`` entry. A test marked with ``skip``,
``skipif`` or ``xfail`` (on the test, its class or the module's ``pytestmark``)
does not count. A skip called inside a test body cannot be seen statically.

Usage::

    python scripts/spec_trace.py
    python scripts/spec_trace.py --enforce 01 02
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = "tests/spec_trace.toml"

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
_LIST_ITEM = re.compile(r"\s*(?:[-*+]|\d+\.)\s")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?;])\s+")
_DOCUMENT = re.compile(r"(\d{2})-[a-z0-9-]+\.md")
_AREA_ROW = re.compile(r"\|\s*(\d{2})\s*\|.*\|\s*`([A-Z]{2,5})`\s*\|\s*$")
_SKIPPING_MARKS = {"skip", "skipif", "xfail"}
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
    path: str
    line: int
    owner: str
    skipped: bool = False


@dataclass
class Trace:
    requirements: dict[str, Requirement] = field(default_factory=dict)
    citations: list[Citation] = field(default_factory=list)
    not_tested: dict[str, str] = field(default_factory=dict)
    documents: set[str] = field(default_factory=set)
    problems: list[str] = field(default_factory=list)

    def cited(self, rid: str) -> list[Citation]:
        """Citations of ``rid`` by tests that run."""
        return [c for c in self.citations if c.rid == rid and not c.skipped]


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
    kind: str  # "item", "continuation", "paragraph", "row" or "heading"


def _blocks(text: str) -> Iterator[_Block]:
    """Split Markdown into the blocks a statement can span.

    A list item runs until the next item or blank line, so wrapped lines join
    it. A paragraph indented under a list item is a continuation of that item.
    Table rows and headings are blocks of their own. Fenced code is skipped.
    """
    fenced = False
    current: list[str] = []
    start = 0
    kind = "paragraph"
    in_list = False
    after_blank = True

    def flush() -> Iterator[_Block]:
        if current:
            yield _Block(start, " ".join(s.strip() for s in current), kind)
            current.clear()

    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            yield from flush()
            fenced = not fenced
            after_blank = True
            continue
        if fenced:
            continue
        if not line.strip():
            yield from flush()
            after_blank = True
            continue
        if line.startswith("#") or line.lstrip().startswith("|"):
            yield from flush()
            is_heading = line.startswith("#")
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


def _lead_in_start(text: str) -> int | None:
    """Where a normative lead-in sentence ending in ":" starts, if there is one."""
    if not text.rstrip().endswith(":"):
        return None
    breaks = list(_SENTENCE_BREAK.finditer(text))
    start = breaks[-1].end() if breaks else 0
    return start if _NORMATIVE.search(text[start:]) else None


class _DocumentReader:
    """Collects the requirements of one specification document."""

    def __init__(self, name: str, area: str, trace: Trace) -> None:
        self.name = name
        self.area = area
        self.trace = trace
        # The level of the current normative lead-in, while its list lasts.
        self.lead_in: str | None = None
        # The line of a lead-in whose list has not started yet.
        self.awaiting_list: int | None = None

    def problem(self, number: int, text: str) -> None:
        self.trace.problems.append(f"{self.name}:{number}: {text}")

    def read(self, text: str) -> None:
        for block in _blocks(text):
            self.read_block(block)
        if self.awaiting_list is not None:
            self.problem(self.awaiting_list, "normative lead-in has no list after it")

    def read_block(self, block: _Block) -> None:
        number, text, kind = block.line, block.text, block.kind
        for word in dict.fromkeys(m.group(1) for m in _NORMATIVE.finditer(text)):
            if word not in _USED:
                self.problem(
                    number,
                    f"{word} is not one of the normative words in spec/README.md; "
                    f"use {', '.join(_USED)}",
                )
        if self.awaiting_list is not None and kind != "item":
            self.problem(self.awaiting_list, "normative lead-in has no list after it")
        self.awaiting_list = None
        if kind in ("paragraph", "heading"):
            self.lead_in = None
        if kind == "heading":
            if _NORMATIVE.search(text):
                self.problem(number, "heading uses a normative word")
            return
        under_lead_in = kind == "item" and self.lead_in is not None

        lead = _lead_in_start(text)
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
            self.awaiting_list = number

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
    for path in sorted(spec_dir.glob("*.md")):
        if path.name == "README.md":
            continue
        match = _DOCUMENT.fullmatch(path.name)
        if not match:
            trace.problems.append(f"{path.name}: not named NN-lowercase-words.md")
            continue
        number = match.group(1)
        if number in trace.documents:
            trace.problems.append(f"{path.name}: another document has number {number}")
            continue
        trace.documents.add(number)
        area = areas.get(number)
        if area is None:
            trace.problems.append(
                f"{path.name}: no ID area in the table in spec/README.md"
            )
            continue
        _DocumentReader(path.name, area, trace).read(path.read_text(encoding="utf-8"))


# Reading the tests


def _mark_name(node: ast.expr) -> str | None:
    """``x`` for ``pytest.mark.x``, ``<alias>.mark.x`` or a bare ``mark.x``."""
    if not isinstance(node, ast.Attribute):
        return None
    owner = node.value
    if (isinstance(owner, ast.Attribute) and owner.attr == "mark") or (
        isinstance(owner, ast.Name) and owner.id == "mark"
    ):
        return node.attr
    return None


def _skips(decorators: Sequence[ast.expr]) -> bool:
    """True if a decorator marks the test to skip or to fail expectedly."""
    for decorator in decorators:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if _mark_name(target) in _SKIPPING_MARKS:
            return True
    return False


def _module_skips(tree: ast.Module) -> bool:
    """True if ``pytestmark`` marks the whole module to skip or fail expectedly."""
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            targets, value = statement.targets, statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            targets, value = [statement.target], statement.value
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in targets):
            marks = value.elts if isinstance(value, ast.List | ast.Tuple) else [value]
            if _skips(marks):
                return True
    return False


class _CitationFinder(ast.NodeVisitor):
    def __init__(self, path: str, trace: Trace, module_skipped: bool) -> None:
        self.path = path
        self.trace = trace
        # The innermost test, class or module, and whether it is skipped.
        self.owners: list[tuple[str, bool]] = [("<module>", module_skipped)]
        self.calls: set[int] = set()

    def _visit_owner(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    ) -> None:
        # Decorators belong to the definition they decorate.
        skipped = _skips(node.decorator_list) or self.owners[-1][1]
        self.owners.append((node.name, skipped))
        self.generic_visit(node)
        self.owners.pop()

    visit_FunctionDef = _visit_owner
    visit_AsyncFunctionDef = _visit_owner
    visit_ClassDef = _visit_owner

    def visit_Call(self, node: ast.Call) -> None:
        if _mark_name(node.func) == "spec":
            self.calls.add(id(node.func))
            owner, skipped = self.owners[-1]
            where = f"{self.path}:{node.lineno}"
            if not node.args or node.keywords:
                self.trace.problems.append(
                    f"{where}: the spec marker takes one or more requirement IDs"
                )
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    self.trace.citations.append(
                        Citation(arg.value, self.path, node.lineno, owner, skipped)
                    )
                else:
                    self.trace.problems.append(
                        f"{where}: spec marker arguments must be string literals"
                    )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if _mark_name(node) == "spec" and id(node) not in self.calls:
            self.trace.problems.append(
                f"{self.path}:{node.lineno}: use the spec marker as a direct call "
                "with literal IDs, so the trace can read it"
            )
        self.generic_visit(node)


def read_citations(tests_dir: Path, root: Path, trace: Trace) -> None:
    """Collect every ``spec`` marker in the test sources."""
    for path in sorted(tests_dir.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        _CitationFinder(relative, trace, _module_skips(tree)).visit(tree)


# Settings and cross-checks


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


def check(root: Path, enforce: Sequence[str] = ()) -> Trace:
    """Read spec/, tests/ and the settings under ``root`` and cross-check them."""
    trace = Trace()
    read_spec(root / "spec", trace)
    read_citations(root / "tests", root, trace)
    enforced: set[str] = set()
    for number in [*read_settings(root / SETTINGS, trace), *enforce]:
        if number in trace.documents:
            enforced.add(number)
        else:
            trace.problems.append(
                f"enforce: {number!r} is not the two-digit number of a document "
                "in spec/"
            )
    for citation in trace.citations:
        requirement = trace.requirements.get(citation.rid)
        where = f"{citation.path}:{citation.line}"
        if requirement is None:
            trace.problems.append(f"{where}: cites unknown requirement {citation.rid}")
        elif requirement.withdrawn:
            trace.problems.append(
                f"{where}: cites withdrawn requirement {citation.rid}"
            )
    for rid in trace.not_tested:
        requirement = trace.requirements.get(rid)
        if requirement is None or requirement.withdrawn:
            reason = "is not a live requirement"
        elif requirement.level != "MUST":
            reason = "is not must-level, so it needs no entry"
        elif trace.cited(rid):
            reason = "is cited by a test; remove the entry"
        else:
            continue
        trace.problems.append(f"{SETTINGS}: [not-tested] {rid} {reason}")
    for requirement in trace.requirements.values():
        if (
            requirement.document[:2] in enforced
            and requirement.level == "MUST"
            and not requirement.withdrawn
            and requirement.rid not in trace.not_tested
            and not trace.cited(requirement.rid)
        ):
            trace.problems.append(
                f"{requirement.document}:{requirement.line}: {requirement.rid} "
                "is must-level and no running test cites it"
            )
    return trace


def render(trace: Trace) -> str:
    """The report printed by the command line."""
    lines = []
    for requirement in trace.requirements.values():
        level = "withdrawn" if requirement.withdrawn else requirement.level
        count = len(trace.cited(requirement.rid))
        note = "  not tested" if requirement.rid in trace.not_tested else ""
        lines.append(
            f"{requirement.rid:<8} {level or '-':<9} {count:>3}  "
            f"{requirement.document}:{requirement.line}{note}"
        )
    live = [r for r in trace.requirements.values() if not r.withdrawn]
    covered = sum(1 for r in live if trace.cited(r.rid))
    lines.append(
        f"{len(live)} requirements, {covered} cited by tests, "
        f"{len(trace.not_tested)} marked not tested, {len(trace.problems)} problems"
    )
    lines.extend(f"problem: {problem}" for problem in trace.problems)
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--enforce",
        nargs="+",
        default=[],
        metavar="NN",
        help="also enforce coverage for these document numbers",
    )
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    trace = check(args.root, args.enforce)
    print(render(trace))
    return 1 if trace.problems else 0


if __name__ == "__main__":
    sys.exit(main())
