"""Citations of requirement IDs, decision records and spec documents in prose.

Docstrings, site pages and the mirrored repository documents cite
requirements as ``CON-30``, decision records as ``ADR 0017`` and spec
documents as ``spec 01``. This module holds the one grammar for those
citations, read by ``tests/test_api_docs.py`` and by the docs build
(``scripts/docs.py`` and its Sphinx extension), so that a citation the tests
accept is one the site links, and the reverse.

The grammar admits a stated set and refuses the rest: a word shaped like a
requirement ID that is not a live requirement is a problem, unless
``NOT_IDS`` lists it as the name of something else.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from scripts import spec_trace

# The one set of joins between cited numbers, in a list or a range: each
# pattern, with the spellings of it the tests write. Every citation rule reads
# JOIN, and every variant the tests try is drawn from these spellings.
JOINS = {
    r",\s*": (", ", ","),
    r",?\s+and\s+": (" and ", ", and "),
    r",?\s+or\s+": (" or ", ", or "),
    r"\s+(?:to|through)\s+": (" to ", " through "),
    r"\s*[-\u2013\u2014]\s*": ("-", "\u2013", "\u2014", " \u2013 ", " - "),
}
JOIN = "(?:" + "|".join(JOINS) + ")"
JOIN_SPELLINGS = tuple(
    spelling for spellings in JOINS.values() for spelling in spellings
)
# A word shaped like a requirement ID: capital letters, a hyphen, digits; and
# the numbers joined to it, as in "CON-30 and 31", "CON-30-33" or
# "CON-30 through CON-33". A number of another area starts a word of its own.
ID_SHAPED = re.compile(
    r"(?<![\w-])(?P<area>[A-Z]+)-(?P<number>\d+)"
    rf"(?P<ends>(?:{JOIN}(?:(?P=area)-)?\d+(?!\w))*)(?!\w)"
)
# Words of that shape that are names of other things, not requirement IDs:
# an encoding, a standard, a licence ("BSD-3-Clause") and a hash.
NOT_IDS = frozenset({"UTF-8", "ECMA-262", "BSD-3", "SHA-256"})
# The numbers after a citing word, joined as above: "0017", "0017 and 0019",
# "01, 02 or 03", "0017 through 0019", "0017\u20130019". A number followed by
# a point and a digit is a version, such as "spec 0.1", and ends the list.
NUMBERS = rf"(?P<rest>(?:\s+\d+(?!\.\d)(?:{JOIN}\d+(?!\.\d))*)?)"
# "ADR", "ADRs", "decision record", "Decision records"; "spec", "Spec",
# "specs", "specification", "Specifications".
ADR = re.compile(r"\b(?:ADRs?|[Dd]ecision records?)\b" + NUMBERS)
SPEC = re.compile(r"\b[Ss]pec(?:ification)?s?\b" + NUMBERS)

_END = re.compile(r"(?:[A-Z]+-)?(\d+)")
_DIGITS = re.compile(r"\d+")

#: What a citation names: a requirement, a decision record or a spec document.
REQUIREMENT, RECORD, DOCUMENT = "requirement", "ADR", "spec document"


@dataclass(frozen=True)
class Citation:
    """One cited requirement, record or document, and where the text names it.

    Attributes:
        kind: ``REQUIREMENT``, ``RECORD`` or ``DOCUMENT``.
        key: The requirement ID (``CON-30``) or the number as written
            (``0017``, ``01``).
        start: Where the words that name it start in the text.
        end: Where they end.
    """

    kind: str
    key: str
    start: int
    end: int


def find(text: str) -> Iterator[Citation]:
    """Every citation in ``text``: requirements first, then records, then documents.

    A list or range yields one citation per number. The first number of an
    ``ADR`` or ``spec`` citation spans the citing word too, so that a link
    reads "ADR 0017"; later numbers span their digits, and a requirement
    written again with its area spans the area too ("through CON-33").
    """
    for match in ID_SHAPED.finditer(text):
        area = match["area"]
        yield Citation(
            REQUIREMENT, f"{area}-{match['number']}", match.start(), match.end("number")
        )
        offset = match.start("ends")
        for end in _END.finditer(match["ends"]):
            yield Citation(
                REQUIREMENT,
                f"{area}-{end[1]}",
                offset + end.start(),
                offset + end.end(),
            )
    for pattern, kind in ((ADR, RECORD), (SPEC, DOCUMENT)):
        for match in pattern.finditer(text):
            offset = match.start("rest")
            for index, number in enumerate(_DIGITS.finditer(match["rest"])):
                start = match.start() if index == 0 else offset + number.start()
                yield Citation(kind, number[0], start, offset + number.end())


class Records:
    """The requirement IDs, spec documents and ADRs that a text may cite.

    Args:
        root: The repository root, holding ``spec/`` and ``docs/adr/``.
    """

    def __init__(self, root: Path) -> None:
        trace = spec_trace.Trace()
        spec_trace.read_spec(root / "spec", trace)
        self.problems_in_spec = list(trace.problems)
        self.live = {
            rid for rid, found in trace.requirements.items() if not found.withdrawn
        }
        self.withdrawn = set(trace.requirements) - self.live
        #: The file of each requirement, as ``01-contracts.md``.
        self.requirement_files = {
            rid: found.document for rid, found in trace.requirements.items()
        }
        self.documents = set(trace.documents)
        #: The file name stem of each spec document, by number.
        self.document_stems = {
            path.name[:2]: path.stem
            for path in (root / "spec").glob("[0-9][0-9]-*.md")
            if path.name[:2] in self.documents
        }
        #: The file name stem of each record, by number.
        self.adr_stems = {
            path.name[:4]: path.stem
            for path in (root / "docs" / "adr").glob("[0-9][0-9][0-9][0-9]-*.md")
        }
        self.adrs = set(self.adr_stems)

    def problem(self, citation: Citation) -> str | None:
        """Say what is wrong with ``citation``, or ``None`` if it names a record."""
        if citation.kind == REQUIREMENT:
            if citation.key in self.withdrawn:
                return f"cites withdrawn requirement {citation.key}"
            if citation.key not in self.live and citation.key not in NOT_IDS:
                return f"cites unknown requirement {citation.key}"
            return None
        known, width = (
            (self.adrs, 4) if citation.kind == RECORD else (self.documents, 2)
        )
        if len(citation.key) != width or citation.key not in known:
            return f"cites unknown {citation.kind} {citation.key}"
        return None

    def problems(self, text: str) -> Iterator[str]:
        """Say which citations in ``text`` name nothing."""
        for citation in find(text):
            said = self.problem(citation)
            if said is not None:
                yield said
