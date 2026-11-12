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
- every requirement ID cited is a live requirement of ``spec/``, read by
  ``scripts/spec_trace.py``, and every ``ADR NNNN`` and ``spec NN`` cited has
  its file;
- no annotation names an object of the package that is not exported.

The checks read text someone else writes, so each admits a stated set and
refuses the rest: a section, a word shaped like a requirement ID or a name in
an annotation that the check does not know is a problem, not a pass.
"""

import builtins
import logging
import re
from collections.abc import Iterator
from pathlib import Path

import griffe
import pytest

import neolith
from scripts import spec_trace

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = sorted(set(neolith.__all__) - {"__version__"})

_SECTIONS = {"text", "parameters", "attributes", "returns", "raises"}
# A word shaped like a requirement ID: capital letters, a hyphen, digits.
_ID_SHAPED = re.compile(r"(?<![\w-])[A-Z]+-\d+(?![\w-])")
# Words of that shape that are names of other things, not requirement IDs.
_NOT_IDS = frozenset({"UTF-8", "ECMA-262"})
_ADR = re.compile(r"\bADRs?\b(?P<rest>(?:\s+(?:and\s+|or\s+)?\d+,?)*)")
_SPEC = re.compile(r"\bspecs?\b(?P<rest>(?:\s+(?:and\s+|or\s+)?\d+,?)*)")


class Records:
    """The requirement IDs, spec documents and ADRs that a docstring may cite."""

    def __init__(self, root: Path) -> None:
        trace = spec_trace.Trace()
        spec_trace.read_spec(root / "spec", trace)
        self.live = {
            rid for rid, found in trace.requirements.items() if not found.withdrawn
        }
        self.withdrawn = set(trace.requirements) - self.live
        self.documents = set(trace.documents)
        self.adrs = {
            path.name[:4]
            for path in (root / "docs" / "adr").glob("[0-9][0-9][0-9][0-9]-*.md")
        }

    def problems(self, text: str) -> Iterator[str]:
        """Say which citations in ``text`` name nothing."""
        for word in _ID_SHAPED.findall(text):
            if word in self.withdrawn:
                yield f"cites withdrawn requirement {word}"
            elif word not in self.live and word not in _NOT_IDS:
                yield f"cites unknown requirement {word}"
        for pattern, known, width, what in (
            (_ADR, self.adrs, 4, "ADR"),
            (_SPEC, self.documents, 2, "spec document"),
        ):
            for match in pattern.finditer(text):
                for number in re.findall(r"\d+", match["rest"]):
                    if len(number) != width or number not in known:
                        yield f"cites unknown {what} {number}"


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


def _private_names(found: griffe.Object, exported: set[str]) -> Iterator[str]:
    """Names in the object's annotations that the package does not export."""
    package = found.package.name
    for annotation in _annotations(found):
        # griffe gives a constant as its text; None is the one an annotation uses.
        if annotation is None or annotation == "None":
            continue
        if isinstance(annotation, str):
            yield f"{annotation!r}, which griffe could not read"
            continue
        for part in annotation.iterate(flat=True):
            if not isinstance(part, griffe.ExprName):
                continue
            path = part.canonical_path
            # A type parameter's path ends "function[T]"; it names no object.
            own = path == package or path.startswith(f"{package}.")
            if own and not path.endswith("]") and path not in exported:
                yield part.name


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
    return False


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


def _members(found: griffe.Object) -> Iterator[tuple[str, griffe.Object]]:
    if isinstance(found, griffe.Class):
        for name, member in found.members.items():
            if not name.startswith("_"):
                yield name, _target(member)


def _texts(package: griffe.Module, names: list[str]) -> Iterator[tuple[str, str]]:
    """Every docstring and ``#:`` comment of the public API, with its owner."""
    for name in names:
        top = _target(package[name])
        label = f"{package.name}.{name}"
        for where, found in ((label, top), *_members(top)):
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
        f"{label}: its annotations name {name}, which {package.name} does not export"
        for name in _private_names(found, set(exported.values()))
    ]
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
    members = dict(_members(found))
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
            f"{label}.{name}: its annotations name {private}, which "
            f"{package.name} does not export"
            for private in _private_names(member, set(exported.values()))
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


def test_the_records_are_read(records: Records) -> None:
    assert "CON-30" in records.live
    assert {"01", "03"} <= records.documents
    assert {"0008", "0017"} <= records.adrs


# The checks themselves, on a sample package

_GOOD = '''
"""A sample."""
from collections.abc import Mapping
from typing import Annotated

__all__ = ["Limit", "Problem", "Thing", "make"]


class _Hidden:
    """Not exported."""


class Problem(ValueError):
    """What ``make`` raises."""


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
    """
    return kind()
'''


def _sample(tmp_path: Path, source: str, records: Records) -> list[str]:
    package = tmp_path / "sample"
    package.mkdir()
    (package / "__init__.py").write_text(source, encoding="utf-8")
    loaded = _load("sample", tmp_path)
    exported = [str(name) for name in loaded.exports or []]
    return api_problems(loaded, sorted(exported), records)


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
        pytest.param("as CON-30 says", "as SHA-256 says", "SHA-256", id="id-shaped"),
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
            "strict: bool = False, **rest: int) -> T:",
            "strict: bool = False, **rest: int) -> T | _Hidden:",
            "sample.make: its annotations name _Hidden, which sample does not export",
            id="private-name-in-a-return",
        ),
    ],
)
def test_each_rule_refuses_its_nearest_wrong_shape(
    tmp_path: Path, records: Records, old: str, new: str, problem: str
) -> None:
    assert _GOOD.count(old) == 1
    found = _sample(tmp_path, _GOOD.replace(old, new), records)
    assert any(problem in said for said in found), found


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
