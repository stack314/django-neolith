"""A status line calls a section specified only if it carries requirement IDs.

Covers every spec document except 06 (see EXEMPT).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SPEC = Path(__file__).resolve().parent.parent / "spec"
# 06 is exempt: its sections restate other documents' IDs through the item
# table (ADR 0032) and carry none of their own.
EXEMPT = {"06-conformance.md"}
FILES = sorted(p.name for p in SPEC.glob("0*.md") if p.name not in EXEMPT)
ID = re.compile(r"\*\*[A-Z]+-\d+[.:]?\*\*")
HEADING = re.compile(r"^(#{2,3}) (.+)$", flags=re.M)


def _sections(text: str) -> dict[str, str]:
    """Map each ``##`` and ``###`` heading to its text up to the next heading
    of the same or a higher level, so a section counts its subsections' IDs."""
    found = list(HEADING.finditer(text))
    out: dict[str, str] = {}
    for i, m in enumerate(found):
        end = len(text)
        for n in found[i + 1 :]:
            if len(n.group(1)) <= len(m.group(1)):
                end = n.start()
                break
        out[m.group(2)] = text[m.end() : end]
    return out


@pytest.mark.parametrize("name", FILES)
def test_status_line_specifies_only_sections_with_ids(name: str) -> None:
    text = (SPEC / name).read_text(encoding="utf-8")
    status = re.search(r"^\*\*Status:\*\*(.+)$", text, flags=re.M)
    assert status
    sentences = re.split(r"(?<=\.)\s+", status.group(1))
    claimed = " ".join(
        x
        for x in sentences
        if "specified" in x.lower() and "described" not in x.lower()
    ).lower()
    for heading, body in _sections(text).items():
        if not ID.search(body):
            assert not re.search(rf"\b{re.escape(heading.lower())}\b", claimed), (
                f"{name}: status line calls '{heading}' specified,"
                " but it carries no requirement ID"
            )
