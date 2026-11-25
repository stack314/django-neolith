"""How the check resolves a name: only through top-level imports, and only
for a name the file binds in no other way (ADR 0030 Decision 3, *How a
name is resolved*).

The rule is checked against the running interpreter: every binding form of
the language, in every kind of scope, under each declaration and at each
position inside a module-level compound statement, is generated, run, and
a name the rule follows must reach the object it imports, at every place
the file reads it.
"""

import ast
import contextlib
import itertools
import json
import symtable
import textwrap
from collections.abc import Iterator

import pytest

from neolith import _check_source
from neolith._check_source import LISTED, bound, canonical, resolved

_IMPORT = "from json import dumps as c"
# Every way Python binds a name (the language reference, "Binding of
# names"), as lines that bind c and run without an error.
_FORMS: dict[str, list[str]] = {
    "nothing": [],
    "assignment": ["c = 1"],
    "unpacking": ["(c, _) = (1, 2)"],
    "augmented": ["try:", "    c += 1", "except Exception:", "    pass"],
    "annotated": ["c: int = 1"],
    "annotation": ["c: int"],
    "import from": ["from os import sep as c"],
    "import": ["import os as c"],
    "def": ["def c():", "    pass"],
    "async def": ["async def c():", "    pass"],
    "class": ["class c:", "    pass"],
    "type": ["type c = int"],
    "for": ["for c in [1]:", "    pass"],
    "with": ["with _cm() as c:", "    pass"],
    "except": ["try:", "    raise ValueError", "except ValueError as c:", "    pass"],
    "except star": [
        "try:",
        "    raise ValueError",
        "except* ValueError as c:",
        "    pass",
    ],
    "walrus": ["(c := 1)"],
    "walrus in a comprehension": ["[(c := 1) for _ in [1]]"],
    "del": ["try:", "    del c", "except NameError:", "    pass"],
    "match capture": ["match 1:", "    case c:", "        pass"],
    "match as": ["match 1:", "    case 1 as c:", "        pass"],
    "match star": ["match [1]:", "    case [*c]:", "        pass"],
    "match rest": ["match {}:", "    case {**c}:", "        pass"],
    "list comprehension": ["[c for c in [1]]"],
    "set comprehension": ["{c for c in [1]}"],
    "dict comprehension": ["{c: 1 for c in [1]}"],
    "generator": ["list(c for c in [1])"],
    "nested comprehension": ["[[c for c in [1]] for _ in [1]]"],
    "type parameter of a function": ["def _g[c]():", "    pass"],
    "type parameter of a class": ["class _K[c]:", "    pass"],
    "type parameter of an alias": ["type _T[c] = int"],
    "lambda parameter": ["(lambda c: c)(1)"],
    "parameter": ["def _h(c=1):", "    return c", "_h()"],
    "star import": ["from json import *"],
}
_DECLARATIONS = {"none": [], "global": ["global c"], "nonlocal": ["nonlocal c"]}
_PROBE = ["try:", "    _probe(c)", "except NameError:", "    _probe(_MISSING)"]


def _indent(lines: list[str], by: int = 1) -> list[str]:
    return [("    " * by + line) if line else line for line in lines]


def _scoped(scope: str, body: list[str]) -> list[str]:
    """``body`` placed directly in a scope of the given kind, which runs."""
    if scope == "module":
        return body
    if scope == "function":
        return ["def _f():", *_indent(body or ["pass"]), "_f()"]
    if scope == "class":
        return ["class _C:", *_indent(body or ["pass"])]
    if scope == "generic function":
        return ["def _gf[_T]():", *_indent(body or ["pass"]), "_gf()"]
    if scope == "generic class":
        return ["class _GC[_T]:", *_indent(body or ["pass"])]
    if scope == "nested function":
        return [
            "def _o():",
            "    c = 0",
            "    def _i():",
            *_indent(body or ["pass"], 2),
            "    _i()",
            "_o()",
        ]
    raise AssertionError(scope)


# Each position inside a module-level compound statement that runs its
# block once.
_POSITIONS: dict[str, tuple[list[str], list[str]]] = {
    "plain": ([], []),
    "if": (["if True:"], []),
    "else": (["if False:", "    pass", "else:"], []),
    "try": (["try:"], ["except ImportError:", "    pass"]),
    "except": (["try:", "    raise ImportError", "except ImportError:"], []),
    "try else": (["try:", "    pass", "except ImportError:", "    pass", "else:"], []),
    "finally": (["try:", "    pass", "finally:"], []),
    "with": (["with _cm():"], []),
    "for": (["for _ in [1]:"], []),
    "for else": (["for _ in []:", "    pass", "else:"], []),
    "while": (["while True:"], ["    break"]),
    "while else": (["while False:", "    pass", "else:"], []),
    "match": (["match 1:", "    case _:"], []),
    "try star": (["try:"], ["except* ImportError:", "    pass"]),
    "except star": (["try:", "    raise ImportError", "except* ImportError:"], []),
    "try star else": (
        ["try:", "    pass", "except* ImportError:", "    pass", "else:"],
        [],
    ),
    "try star finally": (
        ["try:", "    pass", "except* ImportError:", "    pass", "finally:"],
        [],
    ),
}


def _placed(position: str, block: list[str]) -> list[str]:
    head, tail = _POSITIONS[position]
    if not head:
        return block
    depth = 2 if position == "match" else 1
    return [*head, *_indent(block or ["pass"], depth), *tail]


def _generated() -> Iterator[tuple[str, str]]:
    scopes = [
        "module",
        "function",
        "class",
        "generic function",
        "generic class",
        "nested function",
    ]
    for form, scope, declaration, position in itertools.product(
        _FORMS, scopes, _DECLARATIONS, _POSITIONS
    ):
        body = [*_DECLARATIONS[declaration], *_FORMS[form], *_PROBE]
        lines = [
            *_placed(position, [_IMPORT]),
            *_placed(position, _scoped(scope, body)),
            *_PROBE,
        ]
        source = "\n".join(lines) + "\n"
        yield f"{form}, {scope}, {declaration}, {position}", source


def _compiles(source: str) -> bool:
    try:
        compile(source, "<generated>", "exec", dont_inherit=True)
    except SyntaxError:
        return False
    return True


_MISSING = object()


def _run(source: str) -> list[object]:
    seen: list[object] = []
    namespace = {
        "__name__": "generated",
        "_probe": seen.append,
        "_cm": contextlib.nullcontext,
        "_MISSING": _MISSING,
    }
    exec(compile(source, "<generated>", "exec", dont_inherit=True), namespace)  # noqa: S102
    return seen


def _collected(tree: ast.Module, package: str = "") -> set[str]:
    return {
        name
        for node in _check_source._top_level(tree)
        for name, _ in _check_source._binds(node, package)
    }


def test_a_name_the_rule_follows_reaches_what_it_imports_wherever_it_is_read() -> None:
    ran = followed = 0
    for case, source in _generated():
        if not _compiles(source):
            continue
        tree = ast.parse(source)
        table = symtable.symtable(source, "<generated>", "exec")
        names = bound(tree, table, "")
        seen = _run(source)
        ran += 1
        assert seen, case
        if "c" in names:
            followed += 1
            assert names["c"] == "json.dumps", case
            assert all(value is json.dumps for value in seen), (case, source)
        imported = {
            symbol.get_name() for symbol in table.get_symbols() if symbol.is_imported()
        }
        assert imported <= _collected(tree), (case, source)
    # Every form stops the name being followed, as a binding should; with
    # no form, it is followed at each position and in each scope but the
    # nested function, whose outer function assigns it.
    assert ran > 1000
    assert followed >= len(_POSITIONS) * 5


@pytest.mark.parametrize(
    ("source", "followed"),
    [
        # A parameter that shadows an import, and a loop target.
        ("from neolith import connect\ndef f(connect):\n    connect('x')\n", False),
        ("from neolith import connect\nfor connect in []:\n    pass\n", False),
        # Two functions that import different connects.
        (
            "def f():\n    from neolith import connect\n"
            "def g():\n    from other import connect\n",
            False,
        ),
        # A method under a class-level import.
        (
            "from neolith import connect\nclass K:\n    from other import connect\n"
            "    def m(self):\n        connect('x')\n",
            False,
        ),
        # A comprehension target that shadows an import of its own scope.
        ("from neolith import connect\n[connect for connect in []]\n", False),
        (
            "def f():\n    from neolith import connect\n"
            "    return [connect for connect in []]\n",
            False,
        ),
        # An assignment expression in a list comprehension, at module level
        # and in a function.
        ("from neolith import connect\n[(connect := 1) for _ in []]\n", False),
        (
            "from neolith import connect\ndef f():\n    [(connect := 1) for _ in []]\n",
            False,
        ),
        # A bare annotation, a type statement and a type parameter.
        ("from neolith import connect\nconnect: int\n", False),
        ("from neolith import connect\ntype connect = int\n", False),
        ("from neolith import connect\ndef f[connect]():\n    pass\n", False),
        # The compatibility form of include.
        (
            "try:\n    from django.urls import include\n"
            "except ImportError:\n    from django.conf.urls import include\n",
            True,
        ),
        # A later import of the same name from another module, in a
        # module-level with, for, while and match block.
        (
            "from neolith import connect\nwith x:\n    from other import connect\n",
            False,
        ),
        (
            "from neolith import connect\nfor _ in x:\n    from other import connect\n",
            False,
        ),
        (
            "from neolith import connect\nwhile x:\n    from other import connect\n",
            False,
        ),
        (
            "from neolith import connect\nmatch x:\n    case _:\n"
            "        from other import connect\n",
            False,
        ),
        # A later import of the same name in a module-level try/except*.
        (
            "from neolith import connect\ntry:\n    pass\n"
            "except* ValueError:\n    from other import connect\n",
            False,
        ),
        # A relative import Python refuses, of the same name.
        ("from neolith import connect\nif x:\n    from . import connect\n", False),
        # A star import after an import of the same name.
        ("from neolith import connect\nfrom other import *\n", False),
        # The nearest admitted shapes: the same import twice, and one in a
        # module-level block.
        ("from neolith import connect\nfrom neolith import connect\n", True),
        ("if x:\n    from neolith import connect\n", True),
        ("from neolith._host import connect\nfrom neolith import connect\n", True),
    ],
)
def test_each_named_fixture_is_followed_only_where_the_rule_says(
    source: str, followed: bool
) -> None:
    tree = ast.parse(source)
    table = symtable.symtable(source, "<fixture>", "exec")
    names = bound(tree, table, "")
    name = "include" if "include" in source else "connect"
    assert (name in names) is followed, names
    if followed:
        assert canonical(names[name]) in LISTED


@pytest.mark.parametrize(
    ("source", "expression", "listed"),
    [
        ("from django.conf.urls import include", "include", "django.urls.conf.include"),
        ("from django.urls import include", "include", "django.urls.conf.include"),
        ("from django.urls.conf import include", "include", "django.urls.conf.include"),
        ("from django import urls", "urls.include", "django.urls.conf.include"),
        ("from django.urls import include as inc", "inc", "django.urls.conf.include"),
        ("import django.urls", "django.urls.include", "django.urls.conf.include"),
        ("import django.conf.urls as u", "u.include", "django.urls.conf.include"),
        ("import neolith.testing", "neolith.testing.boot", "neolith.testing.boot"),
        ("from neolith import testing", "testing.boot", "neolith.testing.boot"),
        ("from neolith.testing import boot", "boot", "neolith.testing.boot"),
        ("import neolith", "neolith.connect", "neolith._host.connect"),
        ("from neolith import connect as c", "c", "neolith._host.connect"),
        (
            "from neolith import command_scope",
            "command_scope",
            "neolith._host.command_scope",
        ),
        (
            "from django.db.transaction import atomic",
            "atomic",
            "django.db.transaction.atomic",
        ),
        (
            "from django.db import transaction",
            "transaction.atomic",
            "django.db.transaction.atomic",
        ),
        (
            "from django.db.transaction import non_atomic_requests",
            "non_atomic_requests",
            "django.db.transaction.non_atomic_requests",
        ),
        ("from django.db import connection as conn", "conn", "django.db.connection"),
        ("import django.db", "django.db.connections", "django.db.connections"),
        (
            "from django.db import DEFAULT_DB_ALIAS",
            "DEFAULT_DB_ALIAS",
            "django.db.utils.DEFAULT_DB_ALIAS",
        ),
        (
            "from django.db.utils import DEFAULT_DB_ALIAS as D",
            "D",
            "django.db.utils.DEFAULT_DB_ALIAS",
        ),
        (
            "from django.contrib.auth import get_user_model",
            "get_user_model",
            "django.contrib.auth.get_user_model",
        ),
        ("import importlib", "importlib.import_module", "importlib.import_module"),
        (
            "from importlib import import_module",
            "import_module",
            "importlib.import_module",
        ),
        (
            "from django.utils.module_loading import import_string",
            "import_string",
            "django.utils.module_loading.import_string",
        ),
    ],
)
def test_every_way_to_bind_a_listed_name_resolves_to_it(
    source: str, expression: str, listed: str
) -> None:
    text = f"{source}\n{expression}\n"
    tree = ast.parse(text)
    table = symtable.symtable(text, "<fixture>", "exec")
    names = bound(tree, table, "")
    node = tree.body[-1]
    assert isinstance(node, ast.Expr)
    found = resolved(node.value, names)
    assert found is not None
    assert canonical(found) == listed


def test_a_bare_decorator_and_a_keyword_resolve_as_their_names() -> None:
    text = textwrap.dedent(
        """\
        from django.db.transaction import non_atomic_requests
        from django.db import DEFAULT_DB_ALIAS

        @non_atomic_requests
        def view(request):
            return Thing.objects.using(DEFAULT_DB_ALIAS)
        """
    )
    tree = ast.parse(text)
    table = symtable.symtable(text, "<fixture>", "exec")
    names = bound(tree, table, "")
    function = tree.body[2]
    assert isinstance(function, ast.FunctionDef)
    decorator = resolved(function.decorator_list[0], names)
    assert decorator is not None
    assert canonical(decorator) == "django.db.transaction.non_atomic_requests"
    call = function.body[0]
    assert isinstance(call, ast.Return)
    assert isinstance(call.value, ast.Call)
    argument = resolved(call.value.args[0], names)
    assert argument is not None
    assert canonical(argument) == "django.db.utils.DEFAULT_DB_ALIAS"


def test_a_relative_import_binds_its_resolved_name() -> None:
    text = "from . import views\nfrom .. import other\nfrom ... import past\n"
    tree = ast.parse(text)
    table = symtable.symtable(text, "<fixture>", "exec")
    assert bound(tree, table, "shop.sub") == {
        "views": "shop.sub.views",
        "other": "shop.other",
    }
