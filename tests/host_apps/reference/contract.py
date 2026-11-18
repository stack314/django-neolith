"""The reference contract, ``reference.v1``: interfaces and types only.

The conformance suite's contract (ADR 0032 Decision 3). Its neutral
definition is the committed document ``spec/conformance/reference.v1.openapi.json``,
the projection of this contract alone; ``tests/test_reference_contract.py``
holds the two equal. The calling apps import it from here, as an app imports
a contract package, and nothing here reads a Django setting.
"""

import builtins
import datetime
import enum
from typing import Protocol

from pydantic import AwareDatetime, BaseModel

from neolith import Binary, Decimal128, Duration, action, contract


class Colour(enum.StrEnum):
    """The enum form."""

    RED = "red"
    GREEN = "green"


class Nested(BaseModel):
    """The nested record form."""

    label: str


class Values(BaseModel):
    """Every portable form of spec 01, each once required and once optional."""

    boolean: bool
    integer: int
    number: float
    string: str
    bytes: Binary
    decimal: Decimal128
    timestamp: AwareDatetime
    date: datetime.date
    duration: Duration
    list: builtins.list[int]
    map: dict[str, int]
    record: Nested
    enum: Colour
    optional_boolean: bool | None = None
    optional_integer: int | None = None
    optional_number: float | None = None
    optional_string: str | None = None
    optional_bytes: Binary | None = None
    optional_decimal: Decimal128 | None = None
    optional_timestamp: AwareDatetime | None = None
    optional_date: datetime.date | None = None
    optional_duration: Duration | None = None
    optional_list: builtins.list[int] | None = None
    optional_map: dict[str, int] | None = None
    optional_record: Nested | None = None
    optional_enum: Colour | None = None


class RefusalKind(enum.StrEnum):
    """The kinds ``refuse`` raises: five standard kinds, and the declared
    kind ``gone``.
    """

    INVALID_ARGUMENT = "invalid_argument"
    UNAUTHENTICATED = "unauthenticated"
    PERMISSION_DENIED = "permission_denied"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    GONE = "gone"


class Refusal(BaseModel):
    """What ``refuse`` raises: a kind and its message."""

    kind: RefusalKind
    message: str


class Gone(BaseModel):
    """The detail of the declared kind ``gone``: the request's message."""

    reason: str


class Empty(BaseModel):
    """A request with no fields."""


class SeenPrincipal(BaseModel):
    """The principal an action received. Its claims are their JSON text, keys
    sorted, since a claim's value has no single portable form.
    """

    issuer: str
    subject: str
    claims: str


class Seen(BaseModel):
    """What an action's call context held."""

    principal: SeenPrincipal | None = None
    caller: str | None = None
    deadline: bool
    idempotency_key: bool


class Deposit(BaseModel):
    """An amount to add to an account."""

    account: str
    amount: int


class Balance(BaseModel):
    """An account's total."""

    total: int


@contract("reference.v1")
class Reference(Protocol):
    @action(idempotent=True)
    def echo(self, request: Values) -> Values: ...

    @action
    def accept(self, request: Values) -> None: ...

    @action(idempotent=True, errors={"gone": Gone})
    def refuse(self, request: Refusal) -> Values: ...

    @action(idempotent=True)
    def seen(self, request: Empty) -> Seen: ...

    @action(idempotent=True)
    def deposit(self, request: Deposit) -> Balance: ...

    @action(idempotent=True)
    def leak(self, request: Empty) -> Values: ...

    @action(idempotent=True)
    def boom(self, request: Empty) -> Values: ...

    @action(idempotent=True)
    def stall(self, request: Empty) -> Values: ...
