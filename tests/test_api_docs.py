"""The docstrings of the public API say what the code and the records say.

The public API is ``neolith.__all__`` without ``__version__``. For each name,
and each public member of a class, griffe reads the source and its docstring
in Google sections, and these tests check that:

- a docstring is present, with a one-line summary, or a ``#:`` comment above
  an alias or a constant;
- it holds only the sections ``Args``, ``Attributes``, ``Returns`` and
  ``Raises``, and griffe reads it without a warning;
- ``Args``, where it is given, names exactly the parameters, in order; a
  class documents its constructor's in its own docstring or the
  constructor's;
- ``Attributes`` names only attributes the class has, and every public
  attribute is documented there or by its own docstring;
- ``Raises`` names a builtin exception or one the public API exports;
- no ``Args``, ``Attributes`` or ``Returns`` entry writes a type, which the
  annotation holds;
- every requirement ID cited is a live requirement of ``spec/``, read by
  ``scripts/spec_trace.py``, and every ``ADR NNNN`` and ``spec NN`` cited has
  its file;
- no annotation, base class or overload's default names an object of the
  package that is not exported, and every other default, which the reference
  shows by its ``repr``, shows as a literal or as a marker such as
  ``<missing>``;
- a public member a class inherits from a class of the package that is not
  exported is checked as the class's own; one inherited from an exported
  class is checked there, and one from outside the package (a builtin,
  Django) is not checked.

The checks read text someone else writes, so each admits a stated set and
refuses the rest: a section, a word shaped like a requirement ID or a name in
an annotation that the check does not know is a problem, not a pass.
"""

import ast
import builtins
import enum
import functools
import importlib
import inspect
import logging
import re
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path

import griffe
import pytest

import neolith
from scripts.citations import ID_SHAPED as _ID_SHAPED
from scripts.citations import JOIN as _JOIN
from scripts.citations import JOIN_SPELLINGS as _JOIN_SPELLINGS
from scripts.citations import JOINS as _JOINS
from scripts.citations import NUMBERS as _NUMBERS
from scripts.citations import Records

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = sorted(set(neolith.__all__) - {"__version__"})

_SECTIONS = {"text", "parameters", "attributes", "returns", "raises"}


def _load(package: str, where: Path) -> griffe.Module:
    loaded = griffe.load(package, search_paths=[where], docstring_parser="google")
    assert isinstance(loaded, griffe.Module)
    return loaded


def _target(found: griffe.Object | griffe.Alias) -> griffe.Object:
    return found.final_target if isinstance(found, griffe.Alias) else found


def _comment(found: griffe.Object) -> str | None:
    """The ``#:`` comment directly above an object, or ``None``."""
    assert found.lineno is not None
    assert isinstance(found.filepath, Path)
    lines = found.filepath.read_text(encoding="utf-8").splitlines()
    above: list[str] = []
    for line in reversed(lines[: found.lineno - 1]):
        if not line.startswith("#:"):
            break
        above.append(line[2:].strip())
    return " ".join(reversed(above)) or None


class _Warnings(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.said: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.said.append(record.getMessage())


def _sections(found: griffe.Object) -> tuple[dict[str, list[str]], list[str]]:
    """The names each section of a docstring lists, and what is wrong with it."""
    docstring = found.docstring
    assert docstring is not None
    problems: list[str] = []
    lines = docstring.value.splitlines()
    if not lines[0].strip() or (len(lines) > 1 and lines[1].strip()):
        problems.append("its summary is not one line followed by a blank line")
    listener = _Warnings()
    logger = logging.getLogger("griffe")
    level = logger.level
    logger.addHandler(listener)
    logger.setLevel(logging.WARNING)
    try:
        parsed = docstring.parse("google", warnings=True)
    finally:
        logger.removeHandler(listener)
        logger.setLevel(level)
    problems += [f"griffe: {said}" for said in listener.said]
    problems += _written_types(docstring.value)
    listed: dict[str, list[str]] = {}
    for section in parsed:
        kind = section.kind.value
        if kind not in _SECTIONS:
            problems.append(f"has a section the check does not read: {section.title}")
        elif kind != "text" and kind in listed:
            problems.append(f"has two {kind} sections")
        elif kind in ("parameters", "attributes"):
            listed[kind] = [item.name for item in section.value]
        elif kind == "raises":
            listed[kind] = [str(item.annotation) for item in section.value]
        else:
            listed[kind] = []
    return listed, problems


_TITLES = {"parameters": "Args", "attributes": "Attributes", "returns": "Returns"}


def _leading_expression(description: str) -> str | None:
    """The text before the first colon of the first line, if it is code."""
    head, colon, _ = description.partition("\n")[0].partition(":")
    head = head.replace("`", "").strip()
    if not colon or not head:
        return None
    try:
        ast.parse(head, mode="eval")
    except SyntaxError:
        return None
    return head


def _written_types(text: str) -> list[str]:
    """Say which entries write a type, which belongs in the annotation.

    The docstring is read with no owner, so that griffe has no annotation of
    its own to give: any type it finds was written in the text, as in
    ``name (int): ...``, ``Returns: int: ...`` or ``Returns: (int): ...``.
    griffe splits off only a bare word before a ``Returns`` colon, so what
    it leaves in the description is read too: text before the first colon
    that parses as a Python expression, backticks aside, is a type, such as
    ``tuple[str, int]: ...`` or ``str | None: ...``.
    """
    problems: list[str] = []
    for section in griffe.Docstring(text, parser="google").parse(warnings=False):
        kind = section.kind.value
        if kind not in ("parameters", "attributes", "returns"):
            continue
        for item in section.value:
            written = item.annotation or (item.name if kind == "returns" else None)
            if not written and kind == "returns":
                written = _leading_expression(item.description)
            if written:
                problems.append(
                    f"{_TITLES[kind]}: writes the type {written}; a "
                    "type belongs in the annotation"
                )
    return problems


def _parameters(found: griffe.Object) -> list[str] | None:
    """The parameters a caller passes, or ``None`` if the object takes none."""
    if isinstance(found, griffe.Class):
        if "__init__" not in found.members:
            return None
        found = _target(found.members["__init__"])
    if not isinstance(found, griffe.Function):
        return None
    stars = {
        griffe.ParameterKind.var_positional: "*",
        griffe.ParameterKind.var_keyword: "**",
    }
    names = [
        stars.get(parameter.kind, "") + parameter.name
        for parameter in found.parameters
        if parameter.kind is not None
    ]
    bound = found.parent is not None and found.parent.is_class
    return names[1:] if bound and "staticmethod" not in found.labels else names


def _annotations(found: griffe.Object) -> Iterator[str | griffe.Expr | None]:
    if isinstance(found, griffe.Class) and "__init__" in found.members:
        yield from _annotations(_target(found.members["__init__"]))
    if isinstance(found, griffe.Function):
        for function in (found, *(found.overloads or ())):
            yield function.returns
            for parameter in function.parameters:
                yield parameter.annotation
    if isinstance(found, griffe.Attribute):
        yield found.annotation


def _defaults(found: griffe.Object) -> Iterator[str | griffe.Expr | None]:
    """The defaults of an overload, which the reference shows as written."""
    if isinstance(found, griffe.Class) and "__init__" in found.members:
        yield from _defaults(_target(found.members["__init__"]))
    if isinstance(found, griffe.Function):
        for function in found.overloads or ():
            for parameter in function.parameters:
                # griffe gives a literal as its text, which names no object.
                if isinstance(parameter.default, griffe.Expr):
                    yield parameter.default


# A marker shown in place of a value, as dataclasses shows "<factory>".
_MARKER = re.compile(r"<[a-z]+>")
# The address Sphinx strips from a repr.
_ADDRESS = re.compile(r" at 0x[0-9a-f]{8,16}(?=>)")


def _shown(value: object, seen: frozenset[int] = frozenset()) -> str:
    """A default as the reference shows it.

    This is ``sphinx.util.inspect.object_description`` of Sphinx 9.2, in its
    order: a dict, set, frozenset, tuple or list, subclasses too, is shown by
    its items, and an enum member by its class and name unless its enum has
    its own ``__repr__``. ``test_shown_is_what_sphinx_shows`` holds the two
    equal.
    """
    for kind in (dict, set, frozenset):
        if isinstance(value, kind):
            if id(value) in seen:
                return f"{kind.__name__}(...)"
            seen |= {id(value)}
            try:
                keys = sorted(value)
            except TypeError:
                keys = sorted(value, key=lambda key: _shown(key, seen))
            if isinstance(value, dict):
                shown = (
                    f"{_shown(key, seen)}: {_shown(value[key], seen)}" for key in keys
                )
            else:
                shown = (_shown(key, seen) for key in keys)
            inner = "{" + ", ".join(shown) + "}"
            return f"frozenset({inner})" if kind is frozenset else inner
    if isinstance(value, enum.Enum):
        if type(value).__repr__ is not enum.Enum.__repr__:
            return repr(value)
        return f"{type(value).__name__}.{value.name}"
    for sequence in (tuple, list):
        if isinstance(value, sequence):
            if id(value) in seen:
                return f"{sequence.__name__}(...)"
            seen |= {id(value)}
            items = [_shown(item, seen) for item in value]
            if isinstance(value, list):
                return f"[{', '.join(items)}]"
            return f"({', '.join(items)}{',' * (len(items) == 1)})"
    return _ADDRESS.sub("", repr(value)).replace("\n", " ")


def _live(found: griffe.Object) -> object:
    """The object griffe read, imported."""
    module = found.module.path
    live: object = importlib.import_module(module)
    for part in found.path.removeprefix(f"{module}.").split("."):
        live = getattr(live, part)
    return live


def _merged(found: griffe.Object) -> frozenset[str] | None:
    """The parameters whose live default an overloaded function shows.

    Sphinx shows only the overloads of an overloaded function, and puts the
    implementation's default in place of each one written ``...``
    (``_merge_default_value``); an overload's other defaults are shown as
    written and read by name. ``None`` for a function with no overload,
    whose every default is shown.
    """
    if not isinstance(found, griffe.Function) or not found.overloads:
        return None
    return frozenset(
        parameter.name
        for overload in found.overloads
        for parameter in overload.parameters
        if parameter.default == "..."
    )


def _signed(found: griffe.Object) -> list[tuple[object, frozenset[str] | None]]:
    """Each callable whose defaults the reference shows, and which of them.

    As Sphinx 9.2 chooses them: a class shows its own ``__new__`` and
    ``__init__``, and a function itself; a ``functools`` single-dispatch
    function or method also shows each function registered on it but the
    one for ``object``. Which defaults are shown is ``_merged``'s.
    """
    if not isinstance(found, griffe.Class | griffe.Function):
        return []
    live = _live(found)
    if isinstance(found, griffe.Class):
        assert isinstance(live, type)
        return [
            (getattr(live, name), _merged(_target(found.members[name])))
            if name in found.members
            # A method griffe does not read, such as a dataclass's __init__,
            # has no overload.
            else (getattr(live, name), None)
            for name in ("__new__", "__init__")
            if name in vars(live)
        ]
    signed: list[tuple[object, frozenset[str] | None]] = [(live, _merged(found))]
    registry: Mapping[object, object] = {}
    if isinstance(found.parent, griffe.Class):
        raw = inspect.getattr_static(_live(found.parent), found.name)
        if isinstance(raw, functools.singledispatchmethod):
            registry = raw.dispatcher.registry
    elif (
        inspect.isfunction(live)
        and hasattr(live, "register")
        and getattr(getattr(live, "dispatch", None), "__module__", None) == "functools"
    ):
        registry = getattr(live, "registry", {})
    signed += [
        (function, None) for kind, function in registry.items() if kind is not object
    ]
    return signed


def _shown_default_problems(found: griffe.Object) -> Iterator[tuple[str, str, str]]:
    """Each default the reference shows that is neither a literal nor a marker.

    The reference shows a default by its value (docs/site/conf.py leaves
    ``autodoc_preserve_defaults`` off), as ``help()`` does. A literal names
    no object, and a marker such as ``<missing>`` names none either; any
    other text may name one the package does not export.
    """
    try:
        shown = [
            (parameter.name, _shown(parameter.default))
            for live, merged in _signed(found)
            for parameter in inspect.signature(live).parameters.values()  # type: ignore[arg-type]
            if parameter.default is not parameter.empty
            and (merged is None or parameter.name in merged)
        ]
    except Exception as error:  # an import that fails is reported, not raised
        yield "defaults", "", f"its defaults cannot be read: {error!r}"
        return
    for name, text in shown:
        if _MARKER.fullmatch(text):
            continue
        try:
            ast.literal_eval(text)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            yield (
                "defaults",
                name,
                f"its default for {name} shows as {text}, which is neither a "
                "literal nor a marker such as <missing>",
            )


def _bases(found: griffe.Object) -> Iterator[str | griffe.Expr | None]:
    if isinstance(found, griffe.Class):
        yield from found.bases


def _private_names(
    found: griffe.Object, exported: set[str]
) -> Iterator[tuple[str, str, str]]:
    """Where the object names something the package does not export, and what.

    Its annotations, its base classes and its parameters' defaults are read:
    an overload's default as written, any other as the reference shows it.
    Each finding is where, the name (or the text griffe could not read), and
    the problem in words.
    """
    yield from _shown_default_problems(found)
    package = found.package.name
    for where, expressions in (
        ("annotations", _annotations(found)),
        ("bases", _bases(found)),
        ("defaults", _defaults(found)),
    ):
        for expression in expressions:
            # griffe gives a constant as its text; None is the one an
            # annotation uses.
            if expression is None or expression == "None":
                continue
            if isinstance(expression, str):
                yield (
                    where,
                    expression,
                    f"its {where} hold {expression}, which griffe could not read",
                )
                continue
            for part in expression.iterate(flat=True):
                if not isinstance(part, griffe.ExprName):
                    continue
                path = part.canonical_path
                # A type parameter's path ends "function[T]"; it names no object.
                own = path == package or path.startswith(f"{package}.")
                if own and not path.endswith("]") and path not in exported:
                    yield (
                        where,
                        part.name,
                        (
                            f"its {where} name {part.name}, which {package} does "
                            "not export"
                        ),
                    )


def _raised(name: str, package: griffe.Module, exported: dict[str, str]) -> bool:
    """Whether ``name`` is a builtin exception, or one the package exports."""
    if isinstance(found := getattr(builtins, name, None), type):
        return issubclass(found, BaseException)
    if name not in exported:
        return False
    pending, seen = [_target(package[name])], set[str]()
    while pending:
        klass = pending.pop()
        if not isinstance(klass, griffe.Class) or klass.path in seen:
            continue
        seen.add(klass.path)
        for base in klass.bases:
            path = base if isinstance(base, str) else base.canonical_path
            if _raised(path.rpartition(".")[2], package, {}):
                return True
            if path in klass.modules_collection:
                pending.append(_target(klass.modules_collection[path]))
            elif _outside_exception(path, package.name):
                return True
    return False


def _outside_exception(path: str, package: str) -> bool:
    """Whether ``path`` names an exception of another package, such as Django.

    griffe loads only the package, so such a base is imported to be read.
    """
    module, _, name = path.rpartition(".")
    if not module or path.startswith(f"{package}."):
        return False
    try:
        found = getattr(importlib.import_module(module), name, None)
    except ImportError:
        return False
    return isinstance(found, type) and issubclass(found, BaseException)


def api_problems(
    package: griffe.Module, names: list[str], records: Records
) -> list[str]:
    """Everything wrong with the docstrings of ``names`` and their members."""
    exported: dict[str, str] = {}
    for name in names:
        if name not in package.members:
            return [f"{package.name}.{name}: exported, but not defined"]
        exported[name] = _target(package[name]).canonical_path
    problems: list[str] = []
    for name in names:
        top = _target(package[name])
        problems += _object_problems(f"{package.name}.{name}", top, package, exported)
    return [
        *problems,
        *(
            f"{label}: {problem}"
            for label, text in _texts(package, names)
            for problem in records.problems(text)
        ),
    ]


def _members(
    found: griffe.Object, exported: set[str]
) -> Iterator[tuple[str, griffe.Object]]:
    """The public members a class shows: its own, and those it inherits.

    An inherited member is shown, and checked, where it is defined, unless
    that class is in the package and not exported; then the class that
    inherits it shows it. A member from outside the package, such as one of
    ``object`` or of Django's ``AppConfig``, is not checked here.
    """
    if not isinstance(found, griffe.Class):
        return
    package = found.package.name
    for name, member in found.all_members.items():
        if name.startswith("_"):
            continue
        if not member.inherited:
            yield name, _target(member)
            continue
        target = _target(member)
        owner = target.parent.path if target.parent is not None else ""
        inside = owner.startswith(f"{package}.")
        if inside and owner not in exported:
            yield name, target


def _base_problems(found: griffe.Object) -> Iterator[str]:
    """Say which base classes of the package griffe cannot follow.

    griffe leaves out, without a word, the members of a base it cannot
    resolve, and every inherited member when the class has no MRO.
    """
    if not isinstance(found, griffe.Class):
        return
    package = found.package.name
    for base in found.bases:
        path = base if isinstance(base, str) else base.canonical_path
        # A name with no module is a builtin, or a name griffe could not
        # place; one of another package is not loaded and is not followed.
        bare = "." not in path and not hasattr(builtins, path)
        if not (bare or path.startswith(f"{package}.")):
            continue
        try:
            found.modules_collection.get_member(path)
        except KeyError:
            yield f"its base {path} cannot be resolved"
    try:
        found.mro()
    except ValueError as error:
        yield f"it has no method resolution order: {error}"


def _texts(package: griffe.Module, names: list[str]) -> Iterator[tuple[str, str]]:
    """Every docstring and ``#:`` comment of the public API, with its owner."""
    exported = {_target(package[name]).path for name in names}
    for name in names:
        top = _target(package[name])
        label = f"{package.name}.{name}"
        for where, found in ((label, top), *_members(top, exported)):
            if where != label:
                where = f"{label}.{where}"
            if found.docstring is not None:
                yield where, found.docstring.value
            elif isinstance(found.parent, griffe.Module):
                yield where, _comment(found) or ""
        if isinstance(top, griffe.Class) and "__init__" in top.members:
            constructor = _target(top.members["__init__"])
            if constructor.docstring is not None:
                yield f"{label}.__init__", constructor.docstring.value


def _object_problems(
    label: str, found: griffe.Object, package: griffe.Module, exported: dict[str, str]
) -> list[str]:
    problems = [
        f"{label}: {problem}"
        for _, _, problem in _private_names(found, set(exported.values()))
    ]
    problems += [f"{label}: {problem}" for problem in _base_problems(found)]
    if found.docstring is None:
        if isinstance(found, griffe.Attribute) and _comment(found):
            return problems
        return [*problems, f"{label}: has no docstring"]
    listed, wrong = _sections(found)
    problems += [f"{label}: {problem}" for problem in wrong]
    parameters = _parameters(found)
    if "parameters" in listed and listed["parameters"] != (parameters or []):
        problems.append(
            f"{label}: Args lists {listed['parameters']}, but it takes {parameters}"
        )
    for name in listed.get("raises", []):
        if not _raised(name, package, exported):
            problems.append(
                f"{label}: Raises names {name}, which is neither a builtin "
                f"exception nor one that {package.name} exports"
            )
    if not isinstance(found, griffe.Class):
        if "attributes" in listed:
            problems.append(f"{label}: Attributes belongs to a class")
        return problems
    if "__init__" in found.members:
        constructor = _target(found.members["__init__"])
        if constructor.docstring is not None:
            problems += _object_problems(
                f"{label}.__init__", constructor, package, exported
            )
    documented = listed.get("attributes", [])
    members = dict(_members(found, set(exported.values())))
    for name in documented:
        if not isinstance(members.get(name), griffe.Attribute):
            problems.append(f"{label}: Attributes lists {name}, which it does not have")
    for name, member in members.items():
        if member.docstring is not None:
            problems += _object_problems(f"{label}.{name}", member, package, exported)
            if name in documented:
                problems.append(f"{label}.{name}: documented twice")
            continue
        problems += [
            f"{label}.{name}: {problem}"
            for _, _, problem in _private_names(member, set(exported.values()))
        ]
        if not isinstance(member, griffe.Attribute) or name not in documented:
            problems.append(f"{label}.{name}: has no docstring")
    return problems


# The public API


@pytest.fixture(scope="module")
def records() -> Records:
    return Records(ROOT)


def test_the_public_api_is_documented(records: Records) -> None:
    package = _load("neolith", Path(neolith.__file__).resolve().parents[1])
    assert api_problems(package, PUBLIC, records) == []


def test_every_exported_name_is_checked() -> None:
    package = _load("neolith", Path(neolith.__file__).resolve().parents[1])
    checked = [label for label, _ in _texts(package, PUBLIC)]
    for name in PUBLIC:
        assert f"neolith.{name}" in checked
    # The members the reference shows, so that a rename cannot empty the check.
    assert "neolith.Contract.describe" in checked
    assert "neolith.Principal.claims" in checked


# Each alias of a portable form, and the form's name under spec 01's Wire
# encodings.
_FORMS = {"Binary": "bytes", "Decimal128": "decimal", "Duration": "duration"}
# A bound in a form's line: from "below" or "at most" to the end of the clause.
_BOUND = re.compile(r"\b(?:below|at most) [^:,.;]+")


def _wire_encodings() -> dict[str, str]:
    """Each form's line under spec 01's Wire encodings, by the form's names."""
    text = (ROOT / "spec" / "01-contracts.md").read_text(encoding="utf-8")
    section = text.split("\n## Wire encodings\n", 1)[1].split("\n## ", 1)[0]
    lines: dict[str, str] = {}
    for line in section.splitlines():
        if found := re.match(r"- \*\*(.+?):\*\* ", line):
            for name in re.split(r"\*\* and \*\*", found[1]):
                lines[name] = line
    return lines


def test_each_alias_states_its_forms_bounds_as_spec_01_words_them() -> None:
    package = _load("neolith", Path(neolith.__file__).resolve().parents[1])
    aliases = {
        name for name in PUBLIC if isinstance(_target(package[name]), griffe.Attribute)
    }
    assert aliases == set(_FORMS)
    lines = _wire_encodings()
    for alias, form in _FORMS.items():
        comment = _comment(_target(package[alias]))
        assert comment is not None
        bounds = _BOUND.findall(lines[form])
        missing = [bound for bound in bounds if bound not in comment]
        assert missing == [], f"{alias}: {comment}"
    # The bounds the forms have today, so that a change of wording cannot
    # empty the check.
    assert _BOUND.findall(lines["duration"]) == ["below 10^11 seconds in magnitude"]
    assert "at most 34 digits" in _BOUND.findall(lines["decimal"])


def test_the_records_are_read(records: Records) -> None:
    assert "CON-30" in records.live
    assert {"01", "03"} <= records.documents
    assert {"0008", "0017"} <= records.adrs


# The checks themselves, on a sample package

_GOOD = '''
"""A sample."""
import enum
import functools
import json
from collections.abc import Mapping
from typing import Annotated, NamedTuple, overload

__all__ = ["Bigger", "Limit", "Problem", "Thing", "Unreadable", "make"]


class _Hidden:
    """Not exported."""


class Problem(ValueError):
    """What ``make`` raises."""


class Unreadable(json.JSONDecodeError):
    """What ``make`` raises for text it cannot read."""


class _Kind(enum.Enum):
    ONE = 1


class _Marker(enum.Enum):
    MISSING = 1

    def __repr__(self) -> str:
        return "<missing>"


class _Named:
    def __repr__(self) -> str:
        return "_MISSING"


class _Names(tuple):
    def __repr__(self) -> str:
        return "<none>"


#: The limit (spec 01): at most 32 levels, as CON-30 says, in UTF-8.
Limit = Annotated[int, _Hidden()]


class Thing:
    """A thing (ADR 0008; ADRs 0017 and 0019, spec 01).

    Args:
        name: Its name.
        parts: Its parts.

    Attributes:
        name: Its name.

    Raises:
        TypeError: If ``name`` is not a ``str``.
    """

    name: str

    def __init__(self, name: str, parts: Mapping[str, object] | None = None) -> None:
        self.name = name
        self._parts: Mapping[str, _Hidden] = {}

    @property
    def size(self) -> int:
        """How many parts it has."""
        return 0

    @classmethod
    def of(cls, other: object) -> "Thing":
        """Return the thing behind another object."""
        return cls("")

    @staticmethod
    def plain(value: int) -> int:
        """Return the value.

        Args:
            value: Any integer.
        """
        return value

    def _helper(self, hidden: _Hidden) -> None:
        return None


class Bigger(Thing):
    """A thing with more parts; what it inherits is checked on ``Thing``."""


def make[T](kind: type[T], /, *names: str, strict: bool = False, **rest: int) -> T:
    """Make one.

    Args:
        kind: What to make.
        *names: Its names.
        strict: Whether to check.
        **rest: The rest.

    Returns:
        The new object.

    Raises:
        Problem: If it cannot be made.
        KeyboardInterrupt: If interrupted.
        Unreadable: If a name cannot be read.
    """
    return kind()
'''


def _sample(tmp_path: Path, source: str, records: Records) -> list[str]:
    package = tmp_path / "sample"
    package.mkdir()
    (package / "__init__.py").write_text(source, encoding="utf-8")
    loaded = _load("sample", tmp_path)
    exported = [str(name) for name in loaded.exports or []]
    # The defaults are read from the imported sample, as the reference reads
    # them.
    sys.modules.pop("sample", None)
    sys.path.insert(0, str(tmp_path))
    try:
        return api_problems(loaded, sorted(exported), records)
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("sample", None)


def test_a_sample_in_every_admitted_shape_passes(
    tmp_path: Path, records: Records
) -> None:
    assert _sample(tmp_path, _GOOD, records) == []


@pytest.mark.parametrize(
    ("old", "new", "problem"),
    [
        pytest.param(
            '        """Return the thing behind another object."""\n',
            "",
            "sample.Thing.of: has no docstring",
            id="method-without-docstring",
        ),
        pytest.param(
            '    """What ``make`` raises."""\n',
            "    pass\n",
            "sample.Problem: has no docstring",
            id="class-without-docstring",
        ),
        pytest.param(
            '    """A thing (ADR 0008; ADRs 0017 and 0019, spec 01).\n',
            '    """\n',
            "sample.Thing: its summary is not one line followed by a blank line",
            id="no-summary",
        ),
        pytest.param(
            '    """Make one.\n',
            '    """Make one,\n    or two.\n',
            "sample.make: its summary is not one line followed by a blank line",
            id="two-line-summary",
        ),
        pytest.param(
            "#: The limit",
            "# The limit",
            "sample.Limit: has no docstring",
            id="alias-with-a-plain-comment",
        ),
        pytest.param(
            '        """How many parts it has."""\n',
            "",
            "sample.Thing.size: has no docstring",
            id="property-without-docstring",
        ),
        pytest.param(
            "    Attributes:\n        name: Its name.\n\n",
            "",
            "sample.Thing.name: has no docstring",
            id="attribute-not-documented",
        ),
        pytest.param(
            "        name: Its name.\n\n    Raises:",
            "        name: Its name.\n        colour: Its colour.\n\n    Raises:",
            "sample.Thing: Attributes lists colour, which it does not have",
            id="attribute-that-does-not-exist",
        ),
        pytest.param(
            "        name: Its name.\n\n    Raises:",
            "        name: Its name.\n        of: A method.\n\n    Raises:",
            "sample.Thing: Attributes lists of, which it does not have",
            id="method-listed-as-attribute",
        ),
        pytest.param(
            "        parts: Its parts.\n",
            "",
            "sample.Thing: Args lists ['name'], but it takes ['name', 'parts']",
            id="constructor-parameter-missing",
        ),
        pytest.param(
            "        self.name = name\n",
            '        """Build it.\n\n        Args:\n            name: Its name.\n'
            '        """\n        self.name = name\n',
            "sample.Thing.__init__: Args lists ['name'], but it takes "
            "['name', 'parts']",
            id="constructor-docstring-of-its-own",
        ),
        pytest.param(
            "        kind: What to make.\n        *names: Its names.\n",
            "        *names: Its names.\n        kind: What to make.\n",
            "sample.make: Args lists ['*names', 'kind', 'strict', '**rest'], but it "
            "takes ['kind', '*names', 'strict', '**rest']",
            id="parameters-out-of-order",
        ),
        pytest.param(
            "            value: Any integer.\n",
            "            value: Any integer.\n            other: Another.\n",
            "sample.Thing.plain: Args lists ['value', 'other'], but it takes ['value']",
            id="parameter-that-does-not-exist",
        ),
        pytest.param(
            '        """Return the thing behind another object."""\n',
            '        """Return the thing.\n\n        Args:\n            cls: It.\n'
            '            other: It.\n        """\n',
            "sample.Thing.of: Args lists ['cls', 'other'], but it takes ['other']",
            id="cls-listed",
        ),
        pytest.param(
            "        Problem: If it cannot be made.\n",
            "        Trouble: If it cannot be made.\n",
            "sample.make: Raises names Trouble, which is neither a builtin "
            "exception nor one that sample exports",
            id="raises-unknown-name",
        ),
        pytest.param(
            "        Problem: If it cannot be made.\n",
            "        Thing: If it cannot be made.\n",
            "sample.make: Raises names Thing, which is neither a builtin "
            "exception nor one that sample exports",
            id="raises-a-class-that-is-no-exception",
        ),
        pytest.param(
            "        KeyboardInterrupt: If interrupted.\n",
            "        print: If interrupted.\n",
            "sample.make: Raises names print, which is neither a builtin "
            "exception nor one that sample exports",
            id="raises-a-builtin-that-is-no-exception",
        ),
        pytest.param(
            "        Problem: If it cannot be made.\n",
            "        _Hidden: If it cannot be made.\n",
            "sample.make: Raises names _Hidden, which is neither a builtin "
            "exception nor one that sample exports",
            id="raises-a-private-class",
        ),
        pytest.param(
            "class Unreadable(json.JSONDecodeError):",
            "class Unreadable(json.JSONDecoder):",
            "sample.make: Raises names Unreadable, which is neither a builtin "
            "exception nor one that sample exports",
            id="raises-a-class-of-another-package-that-is-no-exception",
        ),
        pytest.param(
            "    Returns:\n        The new object.\n",
            "    Note:\n        The new object.\n",
            "sample.make: has a section the check does not read: Note",
            id="unknown-section",
        ),
        pytest.param(
            "    Returns:\n        The new object.\n",
            "    Raises:\n        TypeError: Once.\n",
            "sample.make: has two raises sections",
            id="section-twice",
        ),
        pytest.param(
            "        Args:\n            value: Any integer.\n",
            "        Attributes:\n            value: Any integer.\n",
            "sample.Thing.plain: Attributes belongs to a class",
            id="attributes-on-a-function",
        ),
        pytest.param("as CON-30 says", "as CON-9999 says", "CON-9999", id="unknown-id"),
        pytest.param("as CON-30 says", "as CON-030 says", "CON-030", id="padded-id"),
        pytest.param("as CON-30 says", "as CNO-30 says", "CNO-30", id="unknown-area"),
        pytest.param("as CON-30 says", "as ISO-8601 says", "ISO-8601", id="id-shaped"),
        pytest.param("ADR 0008;", "ADR 9999;", "unknown ADR 9999", id="unknown-adr"),
        pytest.param("ADR 0008;", "ADR 8;", "unknown ADR 8", id="short-adr"),
        pytest.param(
            "ADRs 0017 and 0019", "ADRs 0017 and 9998", "9998", id="second-adr"
        ),
        pytest.param(
            "ADRs 0017 and 0019", "ADRs 0017, 0019 or 9997", "9997", id="third-adr"
        ),
        pytest.param(", spec 01)", ", spec 99)", "spec document 99", id="spec"),
        pytest.param("(spec 01):", "(spec 77):", "spec document 77", id="comment"),
        pytest.param(
            "parts: Mapping[str, object] | None = None",
            "parts: Mapping[str, _Hidden] | None = None",
            "sample.Thing: its annotations name _Hidden, which sample does not export",
            id="private-name-in-constructor",
        ),
        pytest.param(
            "def size(self) -> int:",
            "def size(self) -> '_Hidden | int':",
            "sample.Thing.size: its annotations name _Hidden, which sample does "
            "not export",
            id="private-name-in-a-quoted-annotation",
        ),
        pytest.param(
            "    name: str\n\n    def __init__",
            "    name: list[_Hidden]\n\n    def __init__",
            "sample.Thing.name: its annotations name _Hidden, which sample does "
            "not export",
            id="private-name-on-an-attribute",
        ),
        pytest.param(
            "class Thing:\n",
            "class Thing(_Hidden):\n",
            "sample.Thing: its bases name _Hidden, which sample does not export",
            id="private-base",
        ),
        pytest.param(
            "class Problem(ValueError):",
            "class Problem(ValueError, list[_Hidden]):",
            "sample.Problem: its bases name _Hidden, which sample does not export",
            id="private-name-in-a-generic-base",
        ),
        pytest.param(
            "parts: Mapping[str, object] | None = None",
            "parts: Mapping[str, object] | None = _Hidden()",
            "sample.Thing: its default for parts shows as <sample._Hidden object>, "
            "which is neither a literal nor a marker such as <missing>",
            id="private-default",
        ),
        pytest.param(
            "parts: Mapping[str, object] | None = None",
            "parts: Mapping[str, object] | None = _Kind.ONE",
            "sample.Thing: its default for parts shows as _Kind.ONE,",
            id="private-enum-default",
        ),
        pytest.param(
            "def make[T](kind",
            "@overload\ndef make[T](kind: type[T], /, *, strict: bool = "
            "_Hidden.__doc__) -> T: ...\n\n\ndef make[T](kind",
            "sample.make: its defaults name _Hidden, which sample does not export",
            id="private-name-in-an-overload-default",
        ),
        pytest.param(
            "def make[T](kind: type[T], /, *names: str, strict: bool = False",
            "@overload\ndef make[T](kind: type[T], /, *, strict: bool = ...) -> T: ..."
            "\n\n\ndef make[T](kind: type[T], /, *names: str, strict: bool = "
            "_Hidden()",
            "sample.make: its default for strict shows as <sample._Hidden object>",
            id="private-default-behind-an-overload-ellipsis",
        ),
        pytest.param(
            "    name: str\n",
            "    name: str\n\n    def __new__(cls, *args: object, hidden: object = "
            "_Hidden()) -> 'Thing':\n        return super().__new__(cls)\n",
            "sample.Thing: its default for hidden shows as <sample._Hidden object>",
            id="private-default-of-own-new",
        ),
        pytest.param(
            "    return kind()\n",
            "    return kind()\n\n\n@functools.singledispatch\ndef convert(value: "
            'object) -> object:\n    """Convert it."""\n    return value\n\n\n'
            "@convert.register\ndef _(value: int, strict: object = _Hidden()) -> "
            'object:\n    return value\n\n\n__all__ += ["convert"]\n',
            "sample.convert: its default for strict shows as <sample._Hidden object>",
            id="private-default-of-a-registered-function",
        ),
        pytest.param(
            "    @staticmethod\n    def plain(",
            "    @functools.singledispatchmethod\n    def meth(self, value: object) -> "
            'object:\n        """Convert it."""\n        return value\n\n'
            "    @meth.register\n    def _(self, value: int, strict: object = "
            "_Hidden()) -> object:\n        return value\n\n"
            "    @staticmethod\n    def plain(",
            "sample.Thing.meth: its default for strict shows as "
            "<sample._Hidden object>",
            id="private-default-of-a-registered-method",
        ),
        pytest.param(
            "    def __init__(self, name: str, parts: Mapping[str, object] | None = "
            "None) -> None:",
            "    @overload\n    def __init__(self, name: str, parts: None = ...) -> "
            "None: ...\n\n    def __init__(self, name: str, parts: Mapping[str, "
            "object] | None = _Hidden()) -> None:",
            "sample.Thing: its default for parts shows as <sample._Hidden object>",
            id="private-default-behind-an-overloaded-init-ellipsis",
        ),
        pytest.param(
            "\n\nclass Bigger(Thing):",
            '\n\nclass Pair(NamedTuple):\n    """Two values."""\n\n    left: int\n'
            '    right: object = _Hidden()\n\n\n__all__ += ["Pair"]'
            "\n\n\nclass Bigger(Thing):",
            "sample.Pair: its default for right shows as <sample._Hidden object>",
            id="private-default-of-a-generated-new",
        ),
        pytest.param(
            '    """Not exported."""\n\n\nclass Problem(ValueError):',
            '    """Not exported."""\n\n    def extra(self) -> None:\n'
            "        return None\n\n\nclass Problem(ValueError, _Hidden):",
            "sample.Problem.extra: has no docstring",
            id="inherited-member-without-docstring",
        ),
        pytest.param(
            '    """Not exported."""\n\n\nclass Problem(ValueError):',
            '    """Not exported."""\n\n    def extra(self) -> "_Hidden":\n'
            '        """Return it."""\n        return self\n\n\n'
            "class Problem(ValueError, _Hidden):",
            "sample.Problem.extra: its annotations name _Hidden, which sample "
            "does not export",
            id="inherited-member-naming-a-private-class",
        ),
        pytest.param(
            "class Bigger(Thing):",
            "class Bigger(Thing, _Gone):",
            "sample.Bigger: its base _Gone cannot be resolved",
            id="base-that-cannot-be-resolved",
        ),
        pytest.param(
            "strict: bool = False, **rest: int) -> T:",
            "strict: bool = False, **rest: int) -> T | _Hidden:",
            "sample.make: its annotations name _Hidden, which sample does not export",
            id="private-name-in-a-return",
        ),
        # A rule each, so that removing any rule fails its sample.
        pytest.param(
            "        Problem: If it cannot be made.\n",
            "        Problem If it cannot be made.\n",
            "sample.make: griffe: ",
            id="griffe-warning",
        ),
        pytest.param(
            "        name: Its name.\n\n    Raises:",
            "        name: Its name.\n        size: How many parts.\n\n    Raises:",
            "sample.Thing.size: documented twice",
            id="documented-twice",
        ),
        pytest.param(
            '__all__ = ["Bigger",',
            '__all__ = ["Gone", "Bigger",',
            "sample.Gone: exported, but not defined",
            id="exported-but-not-defined",
        ),
        pytest.param(
            "def size(self) -> int:",
            "def size(self) -> 'int +':",
            "sample.Thing.size: its annotations hold 'int +', which griffe could "
            "not read",
            id="annotation-griffe-cannot-read",
        ),
        pytest.param(
            'is checked on ``Thing``."""\n',
            'is checked on ``Thing``."""\n\n\nclass Bad(Thing, Bigger):\n'
            '    """No order."""\n\n\n__all__ += ["Bad"]\n',
            "sample.Bad: it has no method resolution order",
            id="no-method-resolution-order",
        ),
        # Citations, in each spelling English gives them.
        pytest.param(
            "        self.name = name\n",
            '        """Build it (ADR 9994)."""\n        self.name = name\n',
            "sample.Thing.__init__: cites unknown ADR 9994",
            id="citation-in-a-constructor",
        ),
        pytest.param(", spec 01)", ", Spec 99)", "spec document 99", id="Spec"),
        pytest.param(", spec 01)", ", specs 01 and 99)", "99", id="specs"),
        pytest.param(
            ", spec 01)", ", specification 99)", "spec document 99", id="specification"
        ),
        pytest.param(
            "ADR 0008;", "decision record 9999;", "unknown ADR 9999", id="record"
        ),
        pytest.param(
            "ADRs 0017 and 0019", "ADRs 0017, 0019, and 9996", "9996", id="oxford"
        ),
        pytest.param("ADRs 0017 and 0019", "ADRs 0017 to 9995", "9995", id="to"),
        pytest.param(
            "as CON-30 says", "as CON-9999's text says", "CON-9999", id="possessive"
        ),
        pytest.param("as CON-30 says", "as (CON-9999) says", "CON-9999", id="paren"),
        # Section titles: another title for a section is still checked, and a
        # title the check does not read is refused.
        pytest.param(
            "        Args:\n            value: Any integer.\n",
            "        Arguments:\n            value: Any integer.\n"
            "            other: Another.\n",
            "sample.Thing.plain: Args lists ['value', 'other']",
            id="Arguments-checked",
        ),
        pytest.param(
            "        Args:\n            value: Any integer.\n",
            "        Parameters:\n            other: Another.\n",
            "sample.Thing.plain: Args lists ['other']",
            id="Parameters-checked",
        ),
        pytest.param(
            "        Args:\n            value: Any integer.\n",
            "        args:\n            other: Another.\n",
            "sample.Thing.plain: Args lists ['other']",
            id="lowercase-args-checked",
        ),
        pytest.param(
            "    Raises:\n        Problem: If it cannot be made.\n",
            "    Exceptions:\n        Trouble: If it cannot be made.\n",
            "sample.make: Raises names Trouble",
            id="Exceptions-checked",
        ),
        pytest.param(
            "    Returns:\n        The new object.\n",
            "    Return:\n        The new object.\n",
            "sample.make: has a section the check does not read: Return",
            id="Return",
        ),
        pytest.param(
            "    Raises:\n        Problem: If it cannot be made.\n",
            "    Raise:\n        Problem: If it cannot be made.\n",
            "sample.make: has a section the check does not read: Raise",
            id="Raise",
        ),
        pytest.param(
            "        **rest: The rest.\n",
            "        **rest: The rest.\n\n"
            "    Keyword Args:\n        strict: Whether.\n",
            "sample.make: has a section the check does not read",
            id="Keyword-Args",
        ),
        pytest.param(
            "    Returns:\n        The new object.\n",
            "    Yields:\n        The new object.\n",
            "sample.make: has a section the check does not read",
            id="Yields",
        ),
        pytest.param(
            "    Returns:\n        The new object.\n",
            "    Examples:\n        >>> make(int)\n",
            "sample.make: has a section the check does not read",
            id="Examples",
        ),
        # A type written in an entry, in each spelling Google style gives it.
        pytest.param(
            "        name: Its name.\n        parts:",
            "        name (str): Its name.\n        parts:",
            "sample.Thing: Args: writes the type str",
            id="type-in-args",
        ),
        pytest.param(
            "        strict: Whether to check.\n",
            "        strict (bool, optional): Whether to check.\n",
            "sample.make: Args: writes the type bool",
            id="optional-type-in-args",
        ),
        pytest.param(
            "        name: Its name.\n\n    Raises:",
            "        name (str): Its name.\n\n    Raises:",
            "sample.Thing: Attributes: writes the type str",
            id="type-in-attributes",
        ),
        pytest.param(
            "        The new object.\n",
            "        T: The new object.\n",
            "sample.make: Returns: writes the type T",
            id="type-in-returns",
        ),
        pytest.param(
            "        The new object.\n",
            "        (T): The new object.\n",
            "sample.make: Returns: writes the type T",
            id="parenthesized-type-in-returns",
        ),
        pytest.param(
            "        The new object.\n",
            "        tuple[str, int]: The new object.\n",
            "sample.make: Returns: writes the type tuple[str, int]",
            id="generic-type-in-returns",
        ),
        pytest.param(
            "        The new object.\n",
            "        str | None: The new object.\n",
            "sample.make: Returns: writes the type str | None",
            id="union-type-in-returns",
        ),
    ],
)
def test_each_rule_refuses_its_nearest_wrong_shape(
    tmp_path: Path, records: Records, old: str, new: str, problem: str
) -> None:
    assert _GOOD.count(old) == 1
    found = _sample(tmp_path, _GOOD.replace(old, new), records)
    assert any(problem in said for said in found), found


@pytest.mark.parametrize(
    ("default", "shown"),
    [
        pytest.param("_Hidden", "<class 'sample._Hidden'>", id="class"),
        pytest.param("_Hidden()", "<sample._Hidden object>", id="instance"),
        pytest.param("_Kind.ONE", "_Kind.ONE", id="enum-member"),
        pytest.param("_Named()", "_MISSING", id="repr-a-name"),
        pytest.param("object()", "<object object>", id="object"),
        pytest.param("(1, _Kind.ONE)", "(1, _Kind.ONE)", id="in-a-tuple"),
        pytest.param(
            "_Names((_Kind.ONE,))", "(_Kind.ONE,)", id="tuple-with-a-marker-repr"
        ),
        pytest.param(
            "{'a': _Hidden}", "{'a': <class 'sample._Hidden'>}", id="in-a-dict"
        ),
        pytest.param("len", "<built-in function len>", id="function"),
    ],
)
def test_a_default_shown_as_neither_a_literal_nor_a_marker_is_refused(
    tmp_path: Path, records: Records, default: str, shown: str
) -> None:
    source = _GOOD.replace("strict: bool = False", f"strict: bool = {default}")
    assert _sample(tmp_path, source, records) == [
        f"sample.make: its default for strict shows as {shown}, which is "
        "neither a literal nor a marker such as <missing>"
    ]


def test_an_overloaded_init_that_writes_a_literal_hides_its_default(
    tmp_path: Path, records: Records
) -> None:
    source = _GOOD.replace(
        "    def __init__(self, name: str, parts: Mapping[str, object] | None = "
        "None) -> None:",
        "    @overload\n    def __init__(self, name: str, parts: None = None) -> "
        "None: ...\n\n    def __init__(self, name: str, parts: Mapping[str, "
        "object] | None = _Hidden()) -> None:",
    )
    assert _sample(tmp_path, source, records) == []


def test_a_static_single_dispatch_method_hides_its_registrations(
    tmp_path: Path, records: Records
) -> None:
    # Sphinx reads registrations only of a singledispatchmethod, so it shows
    # stat(value: object) alone.
    source = _GOOD.replace(
        "    @staticmethod\n    def plain(",
        "    @staticmethod\n    @functools.singledispatch\n    def stat(value: "
        'object) -> object:\n        """Convert it."""\n        return value\n\n'
        "    @staticmethod\n    def plain(",
    ).replace(
        "\n\nclass Bigger(Thing):",
        "\n\n@Thing.stat.register\ndef _(value: int, strict: object = _Hidden()) -> "
        "object:\n    return value\n\n\nclass Bigger(Thing):",
    )
    assert _sample(tmp_path, source, records) == []


def test_an_overload_that_writes_a_literal_hides_the_implementation_default(
    tmp_path: Path, records: Records
) -> None:
    # Sphinx shows the overload's False, not the implementation's _Hidden().
    source = _GOOD.replace(
        "def make[T](kind: type[T], /, *names: str, strict: bool = False",
        "@overload\ndef make[T](kind: type[T], /, *, strict: bool = False) -> T: ..."
        "\n\n\ndef make[T](kind: type[T], /, *names: str, strict: bool = _Hidden()",
    )
    assert _sample(tmp_path, source, records) == []


@pytest.mark.parametrize(
    "default",
    ["_Marker.MISSING", "None", "True", "-1.5", "('a', 1)", "'<missing>'", "()"],
)
def test_a_default_shown_as_a_literal_or_a_marker_passes(
    tmp_path: Path, records: Records, default: str
) -> None:
    source = _GOOD.replace("strict: bool = False", f"strict: bool = {default}")
    assert _sample(tmp_path, source, records) == []


class _Names(tuple[object, ...]):
    def __repr__(self) -> str:
        return "<none>"


class _Plain(enum.Enum):
    ONE = 1


class _Marked(enum.Enum):
    ONE = 1

    def __repr__(self) -> str:
        return "<one>"


_SHOWN_SAMPLES: list[object] = [
    _Names((_Marked.ONE, _Plain.ONE)),
    _Marked.ONE,
    None,
    ("a", 1),
    [1, (2,)],
    {"b": 1, "a": (None,)},
    {2, 1},
    frozenset({"x"}),
    _Plain.ONE,
    object(),
    "a\nb",
]


def test_shown_is_what_sphinx_shows() -> None:
    sphinx = pytest.importorskip("sphinx.util.inspect")
    for value in _SHOWN_SAMPLES:
        assert _shown(value) == sphinx.object_description(value)


def test_the_reference_shows_a_default_by_its_repr() -> None:
    """The check above reads a default as the reference shows it."""
    conf = ast.parse((ROOT / "docs" / "site" / "conf.py").read_text("utf-8"))
    assigned = {
        target.id: node.value
        for node in conf.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    preserve = assigned.get("autodoc_preserve_defaults")
    assert preserve is None or ast.literal_eval(preserve) is False
    assert repr(inspect.signature(neolith.Provided).parameters["contract"].default) == (
        "<missing>"
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pytest.param(
            '    """What ``make`` raises."""\n',
            '    """\n    What ``make`` raises.\n    """\n',
            id="summary-on-the-second-line",
        ),
        pytest.param("(spec 01):", "(spec 01, as of spec 0.1):", id="spec-version"),
        pytest.param(", spec 01)", ", Spec 01)", id="Spec"),
        pytest.param("ADRs 0017 and 0019", "ADRs 0017\u20130019", id="dash-range"),
        pytest.param(", spec 01)", ", specs 01 through 03)", id="through-range"),
        pytest.param("as CON-30 says", "as CON-23 to CON-24 say", id="id-range"),
        pytest.param("as CON-30 says", "as CON-23-24 say", id="id-hyphen-range"),
        pytest.param("as CON-30 says", "as CON-23, 24 and 30 say", id="id-list"),
        pytest.param("as CON-30 says", "as CON-30 and CON-31 say", id="id-and-id"),
        pytest.param("as CON-30 says", "as CON-30, TRN-1 say", id="two-areas"),
        pytest.param("as CON-30 says", "as CON-30-alpha says", id="id-then-word"),
        *(
            pytest.param("        The new object.\n", f"        {prose}\n", id=name)
            for name, prose in (
                ("returns-prose", "The kinds: a map from each kind."),
                ("returns-code-prose", "``True`` if it holds: otherwise not."),
                ("returns-parenthesis", "A map (by kind): from each kind."),
                ("returns-citation", "The URL: see spec 01."),
            )
        ),
        pytest.param(", spec 01)", ", specs 01 and 03)", id="specs"),
        pytest.param(", spec 01)", ", specification 01)", id="specification"),
        pytest.param("ADR 0008;", "decision record 0008;", id="record"),
        pytest.param("ADRs 0017 and 0019", "ADRs 0017, 0019, and 0008", id="oxford"),
        pytest.param("ADRs 0017 and 0019", "ADRs 0017 to 0019", id="to"),
        pytest.param("as CON-30 says", "as CON-30's text says", id="possessive"),
        pytest.param("as CON-30 says", "as (CON-30) says", id="paren"),
        pytest.param(
            "#: The limit (spec 01): at most 32 levels, as CON-30 says, in UTF-8.",
            "#: The limit (spec 01): at most 32 levels,\n#: as CON-30 says, in UTF-8.",
            id="comment-over-two-lines",
        ),
        pytest.param(
            "        Args:\n            value:",
            "        Arguments:\n            value:",
            id="Arguments",
        ),
        pytest.param(
            "        Args:\n            value:",
            "        Parameters:\n            value:",
            id="Parameters",
        ),
        pytest.param(
            "        Args:\n            value:",
            "        Params:\n            value:",
            id="Params",
        ),
        pytest.param(
            "        Args:\n            value:",
            "        args:\n            value:",
            id="lowercase-args",
        ),
        pytest.param(
            "    Raises:\n        Problem:",
            "    Exceptions:\n        Problem:",
            id="Exceptions",
        ),
    ],
)
def test_each_admitted_spelling_passes(
    tmp_path: Path, records: Records, old: str, new: str
) -> None:
    assert _GOOD.count(old) == 1
    assert _sample(tmp_path, _GOOD.replace(old, new), records) == []


# Every refused form, written again in each variant English or Google style
# gives it, so that a rule cannot pass a spelling it was not shown.
_TYPES = (
    "int",
    "tuple[str, int]",
    "Mapping[str, type[BaseModel]]",
    "str | None",
    "collections.abc.Mapping",
    "``int``",
    "``tuple[str, int]``",
    "``str | None``",
)
_TYPE_PLACES = (
    (
        "        strict: Whether to check.\n",
        "        strict ({}): Whether to check.\n",
        "sample.make: Args: writes the type",
    ),
    (
        "        name: Its name.\n\n    Raises:",
        "        name ({}): Its name.\n\n    Raises:",
        "sample.Thing: Attributes: writes the type",
    ),
    (
        "        The new object.\n",
        "        {}: The new object.\n",
        "sample.make: Returns: writes the type",
    ),
    (
        "        The new object.\n",
        "        ({}): The new object.\n",
        "sample.make: Returns: writes the type",
    ),
)
_CITATIONS = (
    ("ADR", "0017", "9999", "cites unknown ADR 9999"),
    ("ADRs", "0017", "9999", "cites unknown ADR 9999"),
    ("decision records", "0017", "9999", "cites unknown ADR 9999"),
    ("spec", "01", "99", "cites unknown spec document 99"),
    ("Spec", "01", "99", "cites unknown spec document 99"),
    ("specs", "01", "99", "cites unknown spec document 99"),
    ("specification", "01", "99", "cites unknown spec document 99"),
)
# The ends a list or range after a requirement ID is written with.
_ID_ENDS = ("9999", "CON-9999")


def _variants() -> Iterator[tuple[str, str, str]]:
    for old, template, problem in _TYPE_PLACES:
        for written in _TYPES:
            yield old, template.format(written), problem
    cite = "as CON-30 says"
    for word, good, bad, problem in _CITATIONS:
        yield cite, f"as ``{word} {bad}`` says", problem
        for join in _JOIN_SPELLINGS:
            yield cite, f"as {word} {good}{join}{bad} says", problem
    yield cite, "as ``CON-9999`` says", "cites unknown requirement CON-9999"
    for join in _JOIN_SPELLINGS:
        for end in _ID_ENDS:
            yield cite, f"as CON-30{join}{end} say", "unknown requirement CON-9999"
        # A list of three, whose last number alone is unknown.
        yield cite, f"as CON-30{join}31{join}9999 say", "unknown requirement CON-9999"


@pytest.mark.parametrize(("old", "new", "problem"), list(_variants()))
def test_each_refused_form_is_refused_in_every_variant(
    tmp_path: Path, records: Records, old: str, new: str, problem: str
) -> None:
    assert _GOOD.count(old) == 1
    found = _sample(tmp_path, _GOOD.replace(old, new), records)
    assert any(problem in said for said in found), found


def test_every_citation_rule_and_variant_uses_the_one_set_of_joins() -> None:
    # Each spelling is one of its own pattern's, and of the shared one.
    for pattern, spellings in _JOINS.items():
        for spelling in spellings:
            assert re.fullmatch(pattern, spelling), spelling
            assert re.fullmatch(_JOIN, spelling), spelling
    # Both citation rules read the shared pattern.
    assert _JOIN in _NUMBERS
    assert _JOIN in _ID_SHAPED.pattern
    # Every citation form is tried with every spelling, so that no form is
    # tried with a reduced set.
    tried = [new for _, new, _ in _variants()]
    forms = [f"{word} {good}" for word, good, _, _ in _CITATIONS] + ["CON-30"]
    for form in forms:
        for join in _JOIN_SPELLINGS:
            assert any(f"{form}{join}" in new for new in tried), (form, join)


def test_a_withdrawn_requirement_is_refused(tmp_path: Path) -> None:
    spec = tmp_path / "spec"
    spec.mkdir()
    (spec / "README.md").write_text(
        "| Document | Title | Area |\n|---|---|---|\n| 01 | Things | `THG` |\n",
        encoding="utf-8",
    )
    (spec / "01-things.md").write_text(
        "# Things\n\n**THG-1.** A thing MUST have a name.\n\n"
        "~~**THG-2.**~~ Withdrawn in spec 0.2: replaced by THG-1.\n",
        encoding="utf-8",
    )
    records = Records(tmp_path)
    assert records.live == {"THG-1"}
    assert list(records.problems("As THG-1 says.")) == []
    assert list(records.problems("As THG-2 says.")) == [
        "cites withdrawn requirement THG-2"
    ]
    assert list(records.problems("ADR 0001")) == ["cites unknown ADR 0001"]
