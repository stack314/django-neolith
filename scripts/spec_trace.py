"""Check requirement IDs in spec/ and the tests that cite them (ADR 0018).

The script reads the specification, the test sources and
``tests/spec_trace.toml``. It imports neither the package nor the tests, and
it needs only the standard library.

It always reports a problem, and exits 1, when:

- normative text carries no requirement ID: a statement without one, text
  before the first ID on a line, or an item in a list introduced by a
  normative lead-in such as "A transport MUST:";
- a normative lead-in is not followed by a list;
- an ID is malformed, uses another document's area or is defined twice;
- an ID marks no normative word, or more than one;
- a withdrawn requirement does not say "Withdrawn in spec <version>: <reason>";
- a test cites an ID that does not exist or has been withdrawn;
- a ``spec`` marker has an argument that is not a string literal;
- an entry under ``[not-tested]`` is not a live, must-level, uncited requirement
  with a reason.

For the documents listed in ``enforce`` (or given with ``--enforce``), every
must-level requirement (MUST, MUST NOT, REQUIRED, SHALL) also needs a citing
test or an entry under ``[not-tested]``.

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
SETTINGS = Path("tests") / "spec_trace.toml"

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

# "**XYZ-12.**". A "~~" right before it strikes the requirement through.
_ID = re.compile(r"(~~)?\*\*([A-Z]+-\d+)\.\*\*")
_WELL_FORMED = re.compile(r"([A-Z]{2,5})-([1-9]\d*)")
_WITHDRAWN = re.compile(r"Withdrawn in spec \d+\.\d+: \S")
_LIST_ITEM = re.compile(r"\s*(?:[-*+]|\d+\.)\s")
_DOCUMENT = re.compile(r"(\d{2})-[a-z0-9-]+\.md")
_AREA_ROW = re.compile(r"\|\s*(\d{2})\s*\|.*\|\s*`([A-Z]{2,5})`\s*\|\s*$")


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


@dataclass
class Trace:
    requirements: dict[str, Requirement] = field(default_factory=dict)
    citations: list[Citation] = field(default_factory=list)
    not_tested: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def cited(self, rid: str) -> list[Citation]:
        return [c for c in self.citations if c.rid == rid]


def read_areas(readme: Path) -> dict[str, str]:
    """Map document numbers to ID areas, from the table in spec/README.md."""
    areas: dict[str, str] = {}
    for line in readme.read_text(encoding="utf-8").splitlines():
        match = _AREA_ROW.match(line)
        if match:
            areas[match.group(1)] = match.group(2)
    return areas


def _prose_lines(text: str) -> Iterator[tuple[int, str]]:
    """Yield numbered, non-blank lines outside fenced code blocks and headings."""
    fenced = False
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced or line.startswith("#") or not line.strip():
            continue
        yield number, line


class _DocumentReader:
    """Collects the requirements of one specification document."""

    def __init__(self, name: str, area: str, trace: Trace) -> None:
        self.name = name
        self.area = area
        self.trace = trace
        # The level of a "... MUST:" line, while its list lasts.
        self.lead_in: str | None = None
        # The line number of a lead-in whose list has not started yet.
        self.awaiting_list: int | None = None

    def problem(self, number: int, text: str) -> None:
        self.trace.problems.append(f"{self.name}:{number}: {text}")

    def read(self, text: str) -> None:
        for number, line in _prose_lines(text):
            self.read_line(number, line)
        if self.awaiting_list is not None:
            self.problem(self.awaiting_list, "normative lead-in has no list after it")

    def read_line(self, number: int, line: str) -> None:
        is_item = bool(_LIST_ITEM.match(line))
        if self.awaiting_list is not None and not is_item:
            self.problem(self.awaiting_list, "normative lead-in has no list after it")
        self.awaiting_list = None
        if not is_item:
            self.lead_in = None
        matches = list(_ID.finditer(line))
        normative = [m.group(1) for m in _NORMATIVE.finditer(line)]
        if not matches:
            if normative and not is_item and line.rstrip().endswith(":"):
                self.lead_in = min(
                    (_LEVELS[word] for word in normative), key=_STRENGTH.index
                )
                self.awaiting_list = number
            elif normative:
                self.problem(number, "normative statement has no requirement ID")
            elif is_item and self.lead_in is not None:
                self.problem(number, "item under a normative lead-in has no ID")
            return
        if _NORMATIVE.search(line[: matches[0].start()]):
            self.problem(number, "normative text before the first requirement ID")
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else None
            self.read_requirement(number, match, line[match.end() : end], is_item)

    def read_requirement(
        self, number: int, match: re.Match[str], statement: str, is_item: bool
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
            elif is_item and self.lead_in is not None:
                level = self.lead_in
            else:
                self.problem(number, f"{rid} marks no normative statement")
        self.trace.requirements[rid] = Requirement(
            rid, self.name, number, level, withdrawn
        )


def read_spec(spec_dir: Path, trace: Trace) -> None:
    """Collect requirements from every numbered document in ``spec_dir``."""
    areas = read_areas(spec_dir / "README.md")
    for path in sorted(spec_dir.glob("*.md")):
        match = _DOCUMENT.fullmatch(path.name)
        if not match:
            continue
        area = areas.get(match.group(1))
        if area is None:
            trace.problems.append(
                f"{path.name}: no ID area in the table in spec/README.md"
            )
            continue
        _DocumentReader(path.name, area, trace).read(path.read_text(encoding="utf-8"))


def _is_spec_marker(node: ast.expr) -> bool:
    """True for ``pytest.mark.spec`` (any module alias) and a bare ``mark.spec``."""
    if not isinstance(node, ast.Attribute) or node.attr != "spec":
        return False
    owner = node.value
    return (isinstance(owner, ast.Attribute) and owner.attr == "mark") or (
        isinstance(owner, ast.Name) and owner.id == "mark"
    )


class _CitationFinder(ast.NodeVisitor):
    def __init__(self, path: str, trace: Trace) -> None:
        self.path = path
        self.trace = trace
        self.owners: list[str] = []

    def _visit_owner(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    ) -> None:
        # Decorators belong to the definition they decorate.
        self.owners.append(node.name)
        self.generic_visit(node)
        self.owners.pop()

    visit_FunctionDef = _visit_owner
    visit_AsyncFunctionDef = _visit_owner
    visit_ClassDef = _visit_owner

    def visit_Call(self, node: ast.Call) -> None:
        if _is_spec_marker(node.func):
            owner = self.owners[-1] if self.owners else "<module>"
            where = f"{self.path}:{node.lineno}"
            if not node.args or node.keywords:
                self.trace.problems.append(
                    f"{where}: the spec marker takes one or more requirement IDs"
                )
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    self.trace.citations.append(
                        Citation(arg.value, self.path, node.lineno, owner)
                    )
                else:
                    self.trace.problems.append(
                        f"{where}: spec marker arguments must be string literals"
                    )
        self.generic_visit(node)


def read_citations(tests_dir: Path, root: Path, trace: Trace) -> None:
    """Collect every ``spec`` marker in the test sources."""
    for path in sorted(tests_dir.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        _CitationFinder(relative, trace).visit(tree)


def read_settings(path: Path, trace: Trace) -> list[str]:
    """Read ``tests/spec_trace.toml``: the enforced documents and [not-tested]."""
    if not path.is_file():
        return []
    settings = tomllib.loads(path.read_text(encoding="utf-8"))
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
    documents = {f"{n}-" for n in [*read_settings(root / SETTINGS, trace), *enforce]}
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
            requirement.document[:3] in documents
            and requirement.level == "MUST"
            and not requirement.withdrawn
            and requirement.rid not in trace.not_tested
            and not trace.cited(requirement.rid)
        ):
            trace.problems.append(
                f"{requirement.document}:{requirement.line}: {requirement.rid} "
                "is must-level and no test cites it"
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
