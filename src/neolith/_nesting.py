"""How deep a JSON text nests, judged before a reader that recurses sees it.

The json module's reader recurses on the C stack. On Python 3.12 and 3.13
its recursion limit assumes a stack of about 2 MB, so on a thread with less,
such as the 1 MB that Alpine's Python builds set, a text nested some 9,000
levels deep ends the process before ``RecursionError`` is raised. Catching
``RecursionError`` is only a backstop. Every text from a network that neolith
reads with the json module is judged here first, by a linear scan that does
not recurse, so the outcome never depends on the stack of the thread that
reads it (spec 01, CON-52).
"""

import itertools
import re
from typing import Final

__all__ = ["DEPTH", "deeper_than"]

# How deep a body may nest (CON-52). pydantic-core's reader, which the
# description check runs after the json module, admits 201 levels, so this
# bound refuses nothing it would admit. Below about 1 MB, pydantic-core's
# validators can still exhaust the stack on a record that contains itself,
# so smaller stacks are not supported.
DEPTH: Final = 256

# A string, from its opening quote to its closing one or to the end of an
# unterminated text; possessive, so a text of many quotes stays linear.
_STRING: Final = re.compile(rb'"[^"\\]*+(?:\\.[^"\\]*+)*+(?:"|\\?\Z)', re.DOTALL)
# Every byte but the brackets, and how each bracket moves the depth.
_NOT_BRACKETS: Final = bytes(set(range(256)) - set(b"[]{}"))
_STEP: Final = [0] * 256
for _bracket, _move in ((b"[", 1), (b"{", 1), (b"]", -1), (b"}", -1)):
    _STEP[_bracket[0]] = _move


def deeper_than(text: bytes, limit: int = DEPTH) -> bool:
    """Whether a text's arrays and objects nest more than ``limit`` deep.

    Counted on the bytes, without recursion, outside its strings. In UTF-8
    no byte of a multi-byte sequence is a bracket, a quote or a backslash,
    so the count is the same as on the decoded text. A text that is not
    JSON may be counted deeper than a reader would go, which only refuses it
    sooner; a closer with no opener ends the json module's reading before it
    goes deeper, so the count, which it lowers, still bounds the reading up
    to there.
    """
    # Nesting is never deeper than the number of openers, which is cheap to
    # count, so most texts need no closer look.
    if text.count(b"[") + text.count(b"{") <= limit:
        return False
    brackets = _STRING.sub(b"", text).translate(None, _NOT_BRACKETS)
    return (
        max(itertools.accumulate(map(_STEP.__getitem__, brackets)), default=0) > limit
    )
