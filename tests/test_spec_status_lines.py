"""A status line calls a section specified only if it carries requirement IDs."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SPEC = Path(__file__).resolve().parent.parent / "spec"
FILES = [
    "00-overview.md",
    "02-transports.md",
    "03-http-transport.md",
    "05-app-declaration.md",
]
ID = re.compile(r"\*\*[A-Z]+-\d+[.:]?\*\*")


def _sections(text: str) -> dict[str, str]:
    parts = re.split(r"^## (.+)$", text, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


@pytest.mark.parametrize("name", FILES)
def test_status_line_specifies_only_sections_with_ids(name: str) -> None:
    text = (SPEC / name).read_text(encoding="utf-8")
    status = re.search(r"^\*\*Status:\*\*(.+)$", text, flags=re.M)
    assert status
    sentences = re.split(r"(?<=\.)\s+", status.group(1))
    claimed = " ".join(
        x for x in sentences if "specified" in x and "described" not in x
    ).lower()
    for heading, body in _sections(text).items():
        if not ID.search(body):
            assert heading.lower() not in claimed, (
                f"{name}: status line calls '{heading}' specified,"
                " but it carries no requirement ID"
            )
