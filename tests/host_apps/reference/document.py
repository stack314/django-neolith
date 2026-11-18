"""The reference document: the projection of ``reference.v1`` alone.

``spec/conformance/reference.v1.openapi.json`` holds it, indented for
reading; ``tests/test_reference_contract.py`` checks that the file is this
text. After a change to the reference contract, write it again with::

    python -m tests.host_apps.reference.document
"""

import json
from pathlib import Path
from typing import Final

from neolith import Contract, _openapi
from tests.host_apps.reference.contract import Reference

PATH: Final = (
    Path(__file__).resolve().parents[3]
    / "spec"
    / "conformance"
    / "reference.v1.openapi.json"
)


def text() -> str:
    """The reference document, as the committed file holds it."""
    tree = json.loads(_openapi.document([Contract.of(Reference)]))
    return json.dumps(tree, indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    PATH.write_text(text(), encoding="utf-8")
