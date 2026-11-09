"""Check requirement IDs in spec/ and the tests that cite them (ADR 0018).

The script reads the specification and the test sources. It imports neither
the package nor the tests, and it needs only the standard library.

It always reports a problem, and exits 1, when:

- a line that uses a normative word carries no requirement ID;
- an ID is malformed, uses another document's area, is defined twice, or marks
  a statement without a normative word;
- a test cites an ID that does not exist or has been withdrawn;
- a ``spec`` marker has an argument that is not a string literal.

With ``--enforce NN [NN ...]``, every must-level requirement (MUST, MUST NOT,
REQUIRED, SHALL) in the named documents also needs at least one citing test.

Usage::

    python scripts/spec_trace.py
    python scripts/spec_trace.py --enforce 01 02 03 05
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# RFC 2119 keywords, strongest first within each level.
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
_RANK = {"MUST": 0, "SHOULD": 1, "MAY": 2}

# A bold "**XYZ-12.**", optionally struck through as "~~**XYZ-12.**~~".
_ID = re.compile(r"(~~)?\*\*([A-Z]+-\d+)\.\*\*(~~)?")
_WELL_FORMED = re.compile(r"([A-Z]{2,5})-([1-9]\d*)")
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
    problems: list[str] = field(default_factory=list)

    def cited(self, rid: str) -> list[Citation]:
        return [c for c in self.citations if c.rid == rid]


def level_of(text: str) -> str | None:
    """The strongest requirement level named in ``text``, or None."""
    levels = {_LEVELS[m.group(1)] for m in _NORMATIVE.finditer(text)}
    return min(levels, key=_RANK.__getitem__) if levels else None


def read_areas(readme: Path) -> dict[str, str]:
    """Map document numbers to ID areas, from the table in spec/README.md."""
    areas: dict[str, str] = {}
    for line in readme.read_text(encoding="utf-8").splitlines():
        match = _AREA_ROW.match(line)
        if match:
            areas[match.group(1)] = match.group(2)
    return areas


def _prose_lines(text: str) -> Iterator[tuple[int, str]]:
    """Yield numbered lines outside fenced code blocks and headings."""
    fenced = False
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced or line.startswith("#"):
            continue
        yield number, line


def read_document(path: Path, area: str, trace: Trace) -> None:
    """Collect the requirements of one specification document into ``trace``."""
    name = path.name
    lead_in: str | None = None  # level of a "... MUST:" line introducing a list
    for number, line in _prose_lines(path.read_text(encoding="utf-8")):
        if not line.strip():
            continue
        is_item = bool(_LIST_ITEM.match(line))
        if not is_item:
            lead_in = None
        matches = list(_ID.finditer(line))
        if not matches:
            if _NORMATIVE.search(line) and line.rstrip().endswith(":"):
                lead_in = level_of(line)
            elif _NORMATIVE.search(line):
                trace.problems.append(
                    f"{name}:{number}: normative statement has no requirement ID"
                )
            continue
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else None
            statement = line[match.end() : end]
            rid = match.group(2)
            withdrawn = bool(match.group(1) and match.group(3))
            where = f"{name}:{number}: {rid}"
            well_formed = _WELL_FORMED.fullmatch(rid)
            if not well_formed:
                trace.problems.append(f"{where} is malformed; use {area}-<n>")
                continue
            if well_formed.group(1) != area:
                trace.problems.append(f"{where} uses another area; use {area}-<n>")
                continue
            if rid in trace.requirements:
                first = trace.requirements[rid]
                trace.problems.append(
                    f"{where} is already defined at {first.document}:{first.line}"
                )
                continue
            level = level_of(statement) or (lead_in if is_item else None)
            if level is None and not withdrawn:
                trace.problems.append(f"{where} marks no normative statement")
            trace.requirements[rid] = Requirement(rid, name, number, level, withdrawn)


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
        read_document(path, area, trace)


def _is_spec_marker(node: ast.expr) -> bool:
    """True for ``pytest.mark.spec`` and ``mark.spec``."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "spec"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "mark"
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


def check(root: Path, enforce: Sequence[str] = ()) -> Trace:
    """Read spec/ and tests/ under ``root`` and cross-check them."""
    trace = Trace()
    read_spec(root / "spec", trace)
    read_citations(root / "tests", root, trace)
    for citation in trace.citations:
        requirement = trace.requirements.get(citation.rid)
        where = f"{citation.path}:{citation.line}"
        if requirement is None:
            trace.problems.append(f"{where}: cites unknown requirement {citation.rid}")
        elif requirement.withdrawn:
            trace.problems.append(
                f"{where}: cites withdrawn requirement {citation.rid}"
            )
    documents = {f"{number}-" for number in enforce}
    for requirement in trace.requirements.values():
        if (
            requirement.document[:3] in documents
            and requirement.level == "MUST"
            and not requirement.withdrawn
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
        lines.append(
            f"{requirement.rid:<8} {level or '-':<9} {count:>3}  "
            f"{requirement.document}:{requirement.line}"
        )
    live = [r for r in trace.requirements.values() if not r.withdrawn]
    covered = sum(1 for r in live if trace.cited(r.rid))
    lines.append(
        f"{len(live)} requirements, {covered} cited by tests, "
        f"{len(trace.problems)} problems"
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
        help="document numbers whose must-level requirements need a citing test",
    )
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    trace = check(args.root, args.enforce)
    print(render(trace))
    return 1 if trace.problems else 0


if __name__ == "__main__":
    sys.exit(main())
