"""A refusal names what it refuses without running that object's code (#44).

A check that refuses a value names its class, a class by its name, or an
error by its class and message. A hostile object can raise from every one of
those reads: a metaclass answers ``__qualname__``, ``__name__``,
``__module__``, ``__dict__`` and ``__mro__``; an instance answers
``__class__``, which ``isinstance`` reads; ``repr`` and ``str`` run its own
methods; and a name may be a ``str`` subclass that raises when it is
formatted. Whatever it raises must not replace the refusal. These tests hand
each kind of hostile object to every check that names what it refuses, and
keep every name read in ``src/neolith`` going through ``neolith._naming``.
"""

import ast
import re
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Annotated, Any, NoReturn, Protocol, cast

import pytest
from django.core.management.base import CommandError
from django.utils.functional import SimpleLazyObject
from pydantic import Field, PrivateAttr, create_model

import neolith
from neolith import (
    ContractDefinitionError,
    ContractError,
    DeclarationError,
    NeolithAppConfig,
    Principal,
    _boot,
    _checks,
    _cli,
    _configure,
    _context,
    _databases,
    _host,
    _migrate,
    _naming,
    action,
    command_scope,
    connect,
    contract,
    implements,
)
from neolith._contracts import Contract
from neolith._crossing import check_request, codec
from neolith._jcs import canonical, canonical_text
from tests.test_contracts import declare_with_field
from tests.test_crossing import INVENTORY, RESERVE, OutOfStock, Reserve, Reserved

_SOURCE = Path(neolith.__file__).parent


def _boom(*args: object, **kwargs: object) -> NoReturn:
    raise RuntimeError("hostile code ran")


class Trap(str):
    """Text that raises when it is formatted, measured, sliced or compared."""

    __format__ = __str__ = __repr__ = __len__ = __getitem__ = _boom
    __contains__ = __add__ = __radd__ = __mod__ = __iter__ = _boom
    __eq__ = __ne__ = __lt__ = _boom
    __hash__ = str.__hash__


class HostileMeta(type):
    """A metaclass that raises for every attribute read on its classes."""

    def __getattribute__(cls, name: str) -> NoReturn:
        raise RuntimeError(f"hostile metaclass read {name}")

    __repr__ = __str__ = __format__ = _boom


class Hostile(metaclass=HostileMeta):
    """An instance that raises for every attribute read, repr and str."""

    def __getattribute__(self, name: str) -> NoReturn:
        raise RuntimeError(f"hostile instance read {name}")

    __repr__ = __str__ = __format__ = _boom


class HostileError(Exception, metaclass=HostileMeta):
    """An exception whose class and message both raise when read."""

    __str__ = _boom


class Renamed:
    pass


# Each name is a Trap, as a class's names may be.
Renamed.__qualname__ = Trap("Renamed")
Renamed.__name__ = Trap("Renamed")
Renamed.__module__ = Trap("renamed")


class TrapRepr:
    """``repr`` and ``str`` that return a Trap."""

    def __repr__(self) -> str:
        return Trap("TrapRepr()")

    __str__ = __repr__


class RaisingRepr:
    __repr__ = __str__ = _boom


def _function() -> None: ...


_function.__qualname__ = Trap("_function")


class EqRaises(type):
    """A metaclass whose == raises: ``in`` on a tuple of classes runs it."""

    __eq__ = __ne__ = _boom
    __hash__ = type.__hash__


class HashRaises(type):
    """A metaclass whose hash raises: ``in`` on a set of classes runs it,
    and so does ``issubclass`` against an ABC."""

    __hash__ = _boom


class Equated(metaclass=EqRaises):
    pass


class Hashed(metaclass=HashRaises):
    pass


HOSTILE: dict[str, object] = {
    "hostile instance": Hostile(),
    "hostile class": Hostile,
    "hostile error": HostileError(),
    "hostile error class": HostileError,
    "renamed instance": Renamed(),
    "renamed class": Renamed,
    "trap repr": TrapRepr(),
    "raising repr": RaisingRepr(),
    "renamed function": _function,
    "eq-raising class's instance": Equated(),
    "hash-raising class's instance": Hashed(),
    "str subclass": Trap("trap"),
}


_HOSTILE = pytest.mark.parametrize("key", HOSTILE)


# The helpers themselves.

_HELPERS: dict[str, Callable[[Any], str | None]] = {
    "qualname": _naming.qualname,
    "name": _naming.name,
    "module": _naming.module,
    "full_name": _naming.full_name,
    "type_name": _naming.type_name,
    "shown": _naming.shown,
    "text": _naming.text,
}


@pytest.mark.parametrize("helper", _HELPERS.values(), ids=_HELPERS.keys())
@_HOSTILE
def test_every_helper_names_every_hostile_object_without_running_it(
    helper: Callable[[object], str | None], key: str
) -> None:
    value = HOSTILE[key]
    found = helper(value)
    assert found is None or type(found) is str
    assert found is None or len(found) <= _naming.LIMIT


@_HOSTILE
def test_an_error_of_any_class_is_named_with_its_message(key: str) -> None:
    value = HOSTILE[key]
    error = value if issubclass(type(value), BaseException) else ValueError()
    found = _naming.cause(cast(BaseException, error))
    assert type(found) is str


def test_a_class_is_named_as_it_was_built_whatever_its_metaclass_answers() -> None:
    class Liar(type):
        def __getattribute__(cls, name: str) -> object:
            if name in ("__qualname__", "__name__", "__module__"):
                return "Other"
            return super().__getattribute__(name)

    class Real(metaclass=Liar):
        pass

    assert _naming.qualname(Real).endswith("<locals>.Real")
    assert _naming.name(Real) == "Real"
    assert _naming.module(Real) == __name__
    assert _naming.full_name(Real) == f"{__name__}.{_naming.qualname(Real)}"


def test_names_given_as_str_subclasses_are_read_as_their_text() -> None:
    assert _naming.qualname(Renamed) == "Renamed"
    assert _naming.name(Renamed) == "Renamed"
    assert _naming.module(Renamed) == "renamed"
    assert _naming.full_name(Renamed) == "renamed.Renamed"
    assert _naming.qualname(_function) == "_function"
    assert _naming.shown(TrapRepr()) == "TrapRepr()"


def test_what_cannot_be_read_is_named_by_a_fixed_description() -> None:
    assert _naming.shown(Hostile()) == "<Hostile that cannot be shown>"
    assert _naming.text(Hostile()) == "<the text cannot be read>"
    assert _naming.cause(HostileError()) == (
        "HostileError: <the message cannot be read>"
    )
    assert _naming.qualname(object()) == "<unnamed>"
    assert _naming.name(cast(type, object())) == "<unnamed>"
    assert _naming.module(cast(type, object())) is None


def test_names_and_text_are_bounded_and_an_empty_message_is_kept() -> None:
    long = type("L" * 500, (), {})
    assert _naming.qualname(long) == "L" * (_naming.LIMIT - 3) + "..."
    assert _naming.qualname(long, None) == "L" * 500
    assert _naming.shown("x" * 500, 20) == "'" + "x" * 16 + "..."
    assert _naming.cause(ValueError()) == "ValueError: "


# Each check that names what it refuses, given each hostile object.


@pytest.mark.spec("TRN-9", "TRN-14")
@_HOSTILE
def test_a_request_of_a_hostile_type_is_refused_as_a_type_error(key: str) -> None:
    value = HOSTILE[key]
    with pytest.raises(TypeError, match=r"the request is .*, not exactly a Reserve"):
        check_request(INVENTORY, RESERVE, value)


@pytest.mark.spec("TRN-9")
@_HOSTILE
def test_a_reply_or_detail_of_a_hostile_type_is_refused_as_a_type_error(
    key: str,
) -> None:
    value = HOSTILE[key]
    with pytest.raises(TypeError, match="not exactly a Reserved"):
        codec(INVENTORY, Reserved).send(value, "the reply")
    with pytest.raises(TypeError, match="not exactly a OutOfStock"):
        codec(INVENTORY, OutOfStock).send(value, "the detail")


@pytest.mark.spec("TRN-9", "TRN-10")
@_HOSTILE
def test_a_record_holding_a_hostile_value_is_refused_as_a_value_error(
    key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = HOSTILE[key]
    # pydantic-core's serializer reports what a Trap name raises while it
    # writes its own error as unraisable, and goes on; it does not escape.
    # It is collected here, since pytest's own hook would show the Trap.
    unraisable: list[object] = []
    monkeypatch.setattr(sys, "unraisablehook", unraisable.append)
    held = Reserve.model_construct(order=value)
    with pytest.raises(ValueError, match="the request"):
        check_request(INVENTORY, RESERVE, held)
    keyed = Reserve.model_construct(order=1, tags={value: []}, parts=[], note=None)
    with pytest.raises(ValueError, match="the request"):
        check_request(INVENTORY, RESERVE, keyed)


@_HOSTILE
def test_a_contract_error_given_hostile_parts_is_a_type_error(key: str) -> None:
    value = HOSTILE[key]
    with pytest.raises(TypeError, match="kind is a str"):
        ContractError(cast(str, value), "message")
    with pytest.raises(TypeError, match="message is a str"):
        ContractError("not_found", cast(str, value))
    with pytest.raises(TypeError, match="carries its detail record"):
        ContractError("out_of_stock", "message", cast(Any, value))
    with pytest.raises(TypeError, match="untyped detail"):
        ContractError("out_of_stock", "message", {"field": value})


@_HOSTILE
def test_action_given_hostile_arguments_is_refused(key: str) -> None:
    value = HOSTILE[key]
    with pytest.raises(ContractDefinitionError, match="takes True or False"):
        action(idempotent=cast(bool, value))
    if value is not _function:
        with pytest.raises(ContractDefinitionError, match="marks a function"):
            action(cast(Any, value))


def test_a_function_with_a_hostile_name_marked_twice_is_refused() -> None:
    def twice(self: object, request: Reserve) -> None: ...

    twice.__qualname__ = Trap("twice")
    action(twice)
    with pytest.raises(ContractDefinitionError, match="twice is marked with @action"):
        action(twice)


@_HOSTILE
def test_contract_given_hostile_arguments_is_refused(key: str) -> None:
    value = HOSTILE[key]
    with pytest.raises(ContractDefinitionError, match="is not a contract identity"):
        contract(cast(str, value))
    with pytest.raises(ContractDefinitionError, match=r"hostile\.v1: "):
        contract("hostile.v1")(cast(Any, value))
    with pytest.raises(ContractDefinitionError, match="is not a contract"):
        Contract.of(value)
    with pytest.raises(ContractDefinitionError, match=r"crossing\.v1"):
        implements(INVENTORY.declaration)(cast(Any, value))


@_HOSTILE
def test_a_hostile_request_type_is_refused(key: str) -> None:
    value = HOSTILE[key]

    def send(self: object, request: object) -> None: ...

    send.__annotations__["request"] = value
    with pytest.raises(ContractDefinitionError, match=r"hostile\.v1, action 'send'"):
        contract("hostile.v1")(
            cast(Any, type("Hostiles", (Protocol,), {"send": action(send)}))
        )


def test_a_default_factory_raising_a_hostile_error_is_named() -> None:
    def factory() -> int:
        raise HostileError

    record = create_model("Made", n=(int, Field(default_factory=factory)))
    with pytest.raises(
        ContractDefinitionError,
        match=re.escape("raised HostileError: <the message cannot be read>"),
    ):
        declare_with_field(record)


def test_a_default_validator_raising_a_hostile_error_is_named() -> None:
    def check(value: object) -> object:
        raise HostileError

    from pydantic import BeforeValidator

    record = create_model(
        "Checked", n=(Annotated[int, BeforeValidator(check)], Field(default=1))
    )
    with pytest.raises(ContractDefinitionError, match="HostileError"):
        declare_with_field(record)


@_HOSTILE
def test_a_principal_given_hostile_claims_is_a_type_error(key: str) -> None:
    value = HOSTILE[key]
    with pytest.raises(TypeError, match="claims are a mapping"):
        Principal("issuer", "subject", cast(Any, value))
    with pytest.raises(TypeError, match="claim's name is a str"):
        Principal("issuer", "subject", {cast(str, value): "x"})
    with pytest.raises(TypeError, match="claim's value is a str"):
        Principal("issuer", "subject", {"name": cast(str, value)})


@_HOSTILE
def test_call_options_given_hostile_values_are_type_errors(key: str) -> None:
    value = HOSTILE[key]
    with pytest.raises(TypeError, match="an app is named by a str"):
        _context._app(value)
    with pytest.raises(TypeError, match="takes a tuple of"):
        _context.names(value, "apps")
    with pytest.raises(TypeError, match="each a str"):
        _context.names((value,), "apps")
    with pytest.raises(TypeError, match="timeout= takes a number"):
        _context.timeout_option(value)
    with pytest.raises(TypeError, match="context= takes a CallContext"):
        _context.parent(value)


@_HOSTILE
def test_host_entry_points_given_hostile_values_are_type_errors(key: str) -> None:
    value = HOSTILE[key]
    with pytest.raises(TypeError, match="takes a contract identity as a str"):
        connect(cast(str, value))
    with pytest.raises(TypeError, match="takes an app's label as a str"):
        command_scope(cast(str, value))
    with pytest.raises(TypeError, match="principal= takes a Principal"):
        command_scope("app", principal=cast(Principal, value))


@_HOSTILE
def test_canonical_json_given_hostile_values_is_refused(key: str) -> None:
    value = HOSTILE[key]
    with pytest.raises(ValueError, match="is not a JSON value"):
        canonical(value)
    with pytest.raises(ValueError, match="a member name is"):
        canonical({value: 1})
    with pytest.raises(TypeError, match="JSON text is a str or bytes"):
        canonical_text(cast(str, value))


@_HOSTILE
def test_a_form_marker_on_a_hostile_type_is_refused(key: str) -> None:
    # Handed over as Pydantic hands it; typing's own Annotated[...] reads
    # the type before neolith sees it.
    value = HOSTILE[key]
    marker = neolith.Binary.__metadata__[0]  # type: ignore[attr-defined]
    with pytest.raises(TypeError, match=r"neolith\.Binary annotates bytes, not"):
        marker.__get_pydantic_core_schema__(value, _boom)


# Each module's own name for an error it reports.
_CAUSES: dict[str, Callable[[Exception], str]] = {
    "boot": _boot._cause,
    "cli": _cli._cause,
    "configure": _configure._cause,
    "host": _host._cause,
    "migrate": _migrate._cause,
}


@pytest.mark.parametrize("cause", _CAUSES.values(), ids=_CAUSES.keys())
def test_every_module_names_a_hostile_error_without_running_it(
    cause: Callable[[Exception], str],
) -> None:
    assert cause(HostileError()) == "HostileError: <the message cannot be read>"

    class Odd(ValueError):
        def __str__(self) -> str:
            return Trap("odd")

    Odd.__qualname__ = Trap("Odd")
    assert cause(Odd()) == "Odd: odd"


def test_the_command_line_reports_a_hostile_refusal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Refusal(CommandError, metaclass=HostileMeta):
        __str__ = _boom

    _cli._report(Refusal(), set())
    assert capsys.readouterr().err == "neolith: <the text cannot be read>\n"


@_HOSTILE
def test_settings_checks_given_hostile_values_name_them(key: str) -> None:
    value = HOSTILE[key]
    assert _checks.middleware_problems(value)
    assert _checks.silenced_problems(value)
    assert _checks.silenced_problems([value])
    assert _databases.databases_problems(value, [])
    assert _configure.databases_difference(value, {})
    assert _configure.databases_difference({value: {}}, {})


# A check that looks a name up, or compares a class, runs none of the
# object's code either: a key is compared only once it is exactly a str, and
# a class only by identity.


class EqMeta(type):
    """A metaclass whose == raises once armed, after its classes are built."""

    armed = False

    def __eq__(cls, other: object) -> bool:
        if EqMeta.armed:
            raise RuntimeError("metaclass __eq__ ran")
        return type.__eq__(cls, other)

    __hash__ = type.__hash__


class EqBase(metaclass=EqMeta):
    pass


class EqDerived(EqBase):
    pass


@pytest.fixture
def armed() -> Iterator[None]:
    EqMeta.armed = True
    try:
        yield
    finally:
        EqMeta.armed = False


@pytest.mark.usefixtures("armed")
def test_a_class_whose_metaclass_eq_raises_is_refused() -> None:
    def send(self: object, request: object) -> None: ...

    send.__annotations__["request"] = EqBase
    with pytest.raises(ContractDefinitionError, match="must be a record"):
        contract("hostile.v1")(
            cast(Any, type("Sends", (Protocol,), {"send": action(send)}))
        )
    with pytest.raises(ContractDefinitionError, match=r"\(Protocol\), with no other"):
        contract("hostile.v1")(EqDerived)


@pytest.mark.parametrize("name", ["stray", "__neolith_contract__"])
def test_a_class_with_a_key_that_is_not_exactly_a_str_is_refused(name: str) -> None:
    keyed = type("Keyed", (), {Trap(name): 1})
    with pytest.raises(ContractDefinitionError, match="is not a contract"):
        Contract.of(keyed)
    with pytest.raises(ContractDefinitionError, match="is not a contract"):
        implements(keyed)
    declared = type("Keyed", (Protocol,), {Trap(name): 1})
    with pytest.raises(ContractDefinitionError, match="not exactly a str"):
        contract("hostile.v1")(declared)


def test_a_function_with_a_key_that_is_not_exactly_a_str_is_refused() -> None:
    def send(self: object, request: Reserve) -> None: ...

    vars(send)[Trap("__neolith_action__")] = 1
    with pytest.raises(ContractDefinitionError, match="not exactly a str"):
        action(send)


def test_an_identity_given_as_a_str_subclass_is_read_as_its_text() -> None:
    class Identity(str):
        __format__ = __str__ = __repr__ = __add__ = __radd__ = __mod__ = _boom

    with pytest.raises(ContractDefinitionError, match=r"hostile\.v1: @contract"):
        contract(Identity("hostile.v1"))(cast(Any, 3))


def test_settings_keys_and_values_that_run_code_are_refused() -> None:
    sqlite = "django.db.backends.sqlite3"
    built = {"default": {"ENGINE": sqlite, "NAME": "/db.sqlite3", "OPTIONS": {}}}
    assert (
        _configure.databases_difference(
            {Trap("default"): {"ENGINE": sqlite, "NAME": "/db.sqlite3"}}, built
        )
        == "alias <Trap that cannot be shown> is not one the host builds"
    )
    assert (
        _configure.databases_difference(
            {
                "default": {
                    "ENGINE": sqlite,
                    "NAME": "/db.sqlite3",
                    "OPTIONS": Hostile(),
                }
            },
            built,
        )
        == "['default']['OPTIONS'] is a Hostile, the host builds a dict"
    )
    assert _databases.databases_problems(
        {"default": {Trap("ENGINE"): sqlite, "NAME": "x"}}, []
    ) == ["database default: a key of type Trap is not a setting's name"]
    assert _databases.databases_problems(
        {"default": {"ENGINE": sqlite, "NAME": Hostile()}}, []
    ) == ["database default: NAME: expected a str or a path, got Hostile"]


def test_a_lazy_mapping_is_still_judged_by_what_it_wraps() -> None:
    lazy = SimpleLazyObject(lambda: {"role": "admin"})
    assert Principal("issuer", "subject", cast(Any, lazy)).claims == {"role": "admin"}
    assert _naming.is_a(lazy, dict)
    assert not _naming.is_a(Hostile(), dict)


@_HOSTILE
def test_more_checks_given_hostile_values_refuse_them(key: str) -> None:
    value = HOSTILE[key]
    with pytest.raises(TypeError, match="timeout= takes a number"):
        command_scope("app", timeout=cast(float, value))
    assert _checks._deadline_problems(value)
    with pytest.raises(DeclarationError, match="provides: got"):
        type(
            "HostileConfig",
            (NeolithAppConfig,),
            {"__module__": __name__, "provides": value},
        )


@_HOSTILE
def test_a_default_holding_a_hostile_value_is_refused(key: str) -> None:
    value = HOSTILE[key]
    for default in (value, [value], {"a": value}):
        made = create_model("Made", n=(int, Field(default_factory=lambda d=default: d)))
        with pytest.raises(ContractDefinitionError):
            declare_with_field(made)
        try:
            record = create_model("Defaulted", n=(int, Field(default=default)))
        except RuntimeError:  # Pydantic's own reads refuse it first
            continue
        with pytest.raises(ContractDefinitionError):
            declare_with_field(record)


_PRIVATE = type(PrivateAttr())


class _OwnPrivate(_PRIVATE):  # type: ignore[misc,valid-type]
    pass


_OwnPrivate.__qualname__ = Trap("_OwnPrivate")


class _Unsigned:
    """A factory whose signature raises an error that cannot be read."""

    @property
    def __signature__(self) -> NoReturn:
        raise HostileError

    def __call__(self) -> int:
        return 0


def test_a_private_attribute_with_hostile_parts_is_named() -> None:
    renamed = create_model(
        "Renamed", n=(int, 0), _p=(int, _OwnPrivate(default_factory=lambda: 0))
    )
    with pytest.raises(ContractDefinitionError, match="which is a _OwnPrivate, not"):
        declare_with_field(renamed)
    unsigned = create_model(
        "Unsigned", n=(int, 0), _p=(int, PrivateAttr(default_factory=_Unsigned()))
    )
    with pytest.raises(
        ContractDefinitionError,
        match=re.escape("cannot be read (HostileError: <the message cannot be read>)"),
    ):
        declare_with_field(unsigned)


# An app config class whose metaclass, or whose own body, runs code.

_CONFIG_METACLASSES: dict[str, type] = {
    "eq-raising": EqRaises,
    "hash-raising": HashRaises,
    "attribute-raising": HostileMeta,
}


def _config(metaclass: type, body: dict[object, object], *bases: type) -> type:
    namespace: dict[object, object] = {"__module__": __name__, **body}
    made: type = metaclass("HostileConfig", bases or (NeolithAppConfig,), namespace)
    return made


@pytest.mark.parametrize(
    "metaclass", _CONFIG_METACLASSES.values(), ids=_CONFIG_METACLASSES.keys()
)
def test_an_app_config_class_whose_metaclass_runs_code_is_refused(
    metaclass: type,
) -> None:
    with pytest.raises(DeclarationError, match="label"):
        _config(metaclass, {"name": "shop.stock", "label": "Bad Label"})
    parent = type(
        "ParentConfig",
        (NeolithAppConfig,),
        {"__module__": __name__, "name": "shop.stock", "label": "stock"},
    )
    with pytest.raises(DeclarationError, match="which holds a declaration"):
        _config(metaclass, {}, parent)
    mixin = metaclass("Mixin", (), {"helper": 1})
    with pytest.raises(DeclarationError, match=r"helper: .*Mixin holds it"):
        _config(
            metaclass, {"name": "shop.stock", "label": "stock"}, mixin, NeolithAppConfig
        )


@pytest.mark.filterwarnings("ignore:non-string key:RuntimeWarning")
@pytest.mark.parametrize("key", [Trap("extra"), 5], ids=["str subclass", "int"])
def test_an_app_config_body_with_a_key_that_is_not_a_str_is_refused(
    key: object,
) -> None:
    with pytest.raises(DeclarationError) as refused:
        _config(type, {key: 1, "name": "shop.stock", "label": "Bad Label"})
    said = str(refused.value)
    assert "HostileConfig has an attribute whose name is not exactly a str" in said
    assert "label: " in said
    mixin = type("Mixin", (), cast("dict[str, object]", {key: 1}))
    with pytest.raises(DeclarationError, match="Mixin has an attribute whose name"):
        _config(type, {"name": "shop.stock", "label": "stock"}, mixin, NeolithAppConfig)


class LateHash(str):
    """A key whose hash raises once armed, after the dict holding it is built."""

    armed = False

    def __hash__(self) -> int:
        if LateHash.armed:
            raise RuntimeError("hostile code ran")
        return str.__hash__(self)


def test_settings_keys_that_run_code_at_any_depth_are_refused() -> None:
    sqlite = "django.db.backends.sqlite3"
    built = {"default": {"ENGINE": sqlite, "NAME": "/db.sqlite3", "OPTIONS": {}}}
    for found in (
        {"default": {"ENGINE": sqlite, "NAME": "/db.sqlite3", LateHash("CUSTOM"): 1}},
        {
            "default": {
                "ENGINE": sqlite,
                "NAME": "/db.sqlite3",
                "OPTIONS": {LateHash("x"): 1},
            }
        },
    ):
        LateHash.armed = True
        try:
            difference = _configure.databases_difference(found, built)
        finally:
            LateHash.armed = False
        assert difference is not None
        assert difference.endswith("['CUSTOM'] is not one the host builds") or (
            difference.endswith("['x'] is not one the host builds")
        )


# Every name read goes through neolith._naming.

# Name reads on neolith's own objects, which no author can replace.
_OWN_NAMES = {
    ("_checker.py", "kind.__module__"),
    ("_frozen.py", "cls.__name__"),
    ("_contracts.py", "type(self).__name__"),
    ("_forms.py", "self.python_type.__name__"),
    ("_portable.py", "named.python_type.__name__"),
}


def test_every_name_read_in_the_package_goes_through_naming() -> None:
    found = set()
    for path in sorted(_SOURCE.glob("*.py")):
        if path.name == "_naming.py":
            continue
        source = path.read_text()
        for node in ast.walk(ast.parse(source)):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.ctx, ast.Load)
                and node.attr in ("__qualname__", "__name__", "__module__")
            ):
                found.add((path.name, ast.get_source_segment(source, node)))
    assert found == _OWN_NAMES


def _is_type_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "type"
        and len(node.args) == 1
    )


def test_no_class_of_a_value_is_compared_by_equality_or_hash() -> None:
    # In each function, a type(...) call, or a name assigned from one, is
    # never the left of ==, !=, in or not in, nor a key looked up: each of
    # those runs the class's metaclass __eq__ or __hash__.
    found = set()
    for path in sorted(_SOURCE.glob("*.py")):
        source = path.read_text()
        for function in ast.walk(ast.parse(source)):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            kinds = {
                target.id
                for node in ast.walk(function)
                if isinstance(node, ast.Assign) and _is_type_call(node.value)
                for target in node.targets
                if isinstance(target, ast.Name)
            }

            def is_kind(node: ast.AST, kinds: set[str] = kinds) -> bool:
                return _is_type_call(node) or (
                    isinstance(node, ast.Name) and node.id in kinds
                )

            for node in ast.walk(function):
                compared = isinstance(node, ast.Compare) and (
                    (
                        is_kind(node.left)
                        and any(
                            isinstance(op, ast.Eq | ast.NotEq | ast.In | ast.NotIn)
                            for op in node.ops
                        )
                    )
                    or (
                        any(isinstance(op, ast.Eq | ast.NotEq) for op in node.ops)
                        and any(is_kind(other) for other in node.comparators)
                    )
                )
                looked_up = (
                    isinstance(node, ast.Subscript) and is_kind(node.slice)
                ) or (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("get", "setdefault", "pop")
                    and bool(node.args)
                    and is_kind(node.args[0])
                )
                if compared or looked_up:
                    found.add(f"{path.name}:{ast.get_source_segment(source, node)}")
    assert found == set()
