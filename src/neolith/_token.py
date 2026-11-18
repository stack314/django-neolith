"""The shared-secret token of spec 03: secrets, the check and the signer
(ADR 0027 Decisions 2 to 4), on the standard library alone.

The check is a plain function of the ``Authorization`` value, the identity
in the request path, the key table and *now*, so a test gives it any clock.
It never raises for a token: it returns the calling app, a refusal's fixed
message, or that it abstains.

Secret bytes and token text are handled only by the private functions
here. The two boundaries that call them, ``mint`` and the shared-secret
backend's ``authenticate``, catch any failure inside and raise a fixed
``Failure`` from outside the handler, so the error a host logs or shows has
no ``__context__``, and no frame on its traceback holds secret bytes or
token text (Decision 3). A secret is held in a ``Secret``, and a minted
token in a ``Token``, whose ``repr`` names them without showing them.
"""

import base64
import binascii
import hmac
import json
import math
import re
from collections.abc import Mapping
from typing import Final, NoReturn, final

from neolith import _naming
from neolith._portable import SAFE_INTEGER_MAX

__all__ = [
    "EXAMPLE_SECRET",
    "KEY_ID",
    "LIFETIME",
    "Failure",
    "Secret",
    "Token",
    "check",
    "decode",
    "mint",
    "secret_problem",
    "shown_key_id",
]

# The whole token after the scheme, in bytes.
LIMIT: Final = 4096
# The lifetime of a token this host mints, in seconds (Decision 4).
LIFETIME: Final = 60
_LONGEST: Final = 300
_SKEW: Final = 60
_ALG: Final = "HS256"
_TYP: Final = "neolith+jwt"
_TYPES: Final = frozenset({_TYP, f"application/{_TYP}"})
_HEADER: Final = frozenset({"alg", "typ", "kid"})
_CLAIMS: Final = frozenset({"iss", "aud", "iat", "exp"})
_B64: Final = re.compile(r"[A-Za-z0-9_-]+")
# A key id (the admitted set): 1 to 64 letters, digits, "-", "_" or ".".
KEY_ID: Final = re.compile(r"[A-Za-z0-9._-]{1,64}")
# A secret: 43 base64url characters, which is 32 bytes (Decision 3).
_SECRET_LENGTH: Final = 43
_SECRET_BYTES: Final = 32
_HEX: Final = re.compile(r"[0-9A-Fa-f]+")
# Spec 03's published example secret, in its two spellings.
EXAMPLE_SECRET: Final = bytes(range(32))
_EXAMPLE_HEX: Final = EXAMPLE_SECRET.hex()
_EXAMPLE_B64: Final = base64.urlsafe_b64encode(EXAMPLE_SECRET).rstrip(b"=").decode()
_GENERATE: Final = "generate one with secrets.token_urlsafe(32)"
_SHOWN_KEY_ID: Final = 32

# The refusals of a recognized token, in the order they are checked
# (Decision 2).
PARTS: Final = "the token is not three base64url parts of at most 4096 bytes"
MEMBERS: Final = (
    "the token's header or claims do not have exactly spec 03's members and types"
)
ALG: Final = "the token's alg is not HS256"
ISSUER: Final = "the token's issuer is not an app this host accepts calls from"
KID: Final = "the token's key id is not one this host holds for its issuer"
SIGNATURE: Final = "the token's signature does not verify"
AUDIENCE: Final = "the token is for another contract"
LIFE: Final = "the token's lifetime is not between 1 and 300 seconds"
FUTURE: Final = (
    "the token was issued in the future, beyond 60 seconds of clock difference"
)
EXPIRED: Final = "the token has expired"


class Failure(Exception):
    """A failure inside the token code, raised again with fixed text and no
    context, so no secret byte or token text reaches a log or a page.
    """


@final
class Secret:
    """A secret's 32 bytes, named by app and key id; its ``repr`` shows the
    names only, and it cannot be copied out by pickling.
    """

    __slots__ = ("_app", "_bytes", "_key_id")

    def __init__(self, data: bytes, app: str, key_id: str) -> None:
        self._bytes = data
        self._app = app
        self._key_id = key_id

    def __repr__(self) -> str:
        return f"<secret {self._app}/{shown_key_id(self._key_id)}>"

    @property
    def app(self) -> str:
        """The app the secret is held for."""
        return self._app

    @property
    def key_id(self) -> str:
        """The secret's key id."""
        return self._key_id

    def __reduce__(self) -> NoReturn:
        raise TypeError("a secret is not pickled")

    def same(self, other: "Secret") -> bool:
        """Whether two secrets hold the same bytes, compared in constant time."""
        return hmac.compare_digest(self._bytes, other._bytes)


@final
class Token:
    """A minted token, held until the transport writes ``Authorization``;
    its ``repr`` names the app and the contract only.
    """

    __slots__ = ("_app", "_identity", "_text")

    def __init__(self, app: str, identity: str, text: str) -> None:
        self._app = app
        self._identity = identity
        self._text = text

    def __repr__(self) -> str:
        return f"<token {self._app} to {self._identity}>"

    def __reduce__(self) -> NoReturn:
        raise TypeError("a token is not pickled")

    def authorization(self) -> str:
        """The value of ``Authorization``: ``Bearer`` and the token."""
        return f"Bearer {self._text}"


def shown_key_id(key_id: str) -> str:
    """A key id for a message or a ``repr``: as written when it is shorter
    than 32 characters, else a placeholder, since a secret pasted in its
    place would be at least that long.
    """
    if len(key_id) < _SHOWN_KEY_ID:
        return key_id
    return f"(a key id of {len(key_id)} characters, not shown)"


def decode(part: str) -> bytes | None:
    """Strict base64url: the alphabet only, no padding, no whitespace and
    zero unused bits (spec 03); ``None`` for anything else.
    """
    if not _B64.fullmatch(part) or len(part) % 4 == 1:
        return None
    try:
        data = base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
    except (binascii.Error, ValueError):
        return None
    return data if _encode(data) == part else None


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def secret_problem(value: object) -> str | None:
    """Why ``value`` is not an admitted secret, or ``None`` (Decision 3).

    The text says what was found and never shows the value.
    """
    if type(value) is not str:
        return (
            f"is a {_naming.type_name(value)}, not exactly a str; a secret is a "
            f"str of 43 base64url characters, which is 32 bytes ({_GENERATE})"
        )
    text: str = value
    if text in (_EXAMPLE_HEX, _EXAMPLE_HEX.upper(), _EXAMPLE_B64):
        return f"is spec 03's example secret, which anyone can read; {_GENERATE}"
    if len(text) == 2 * _SECRET_BYTES and _HEX.fullmatch(text):
        return (
            "looks like hexadecimal text, which is not read as hexadecimal; a "
            f"secret is 43 base64url characters, which is 32 bytes ({_GENERATE})"
        )
    if text.endswith("="):
        return (
            "is padded with '='; a secret is 43 base64url characters with no "
            f"padding ({_GENERATE})"
        )
    if not _B64.fullmatch(text):
        return (
            "holds a character outside base64url (letters, digits, '-' and '_'); "
            f"{_GENERATE}"
        )
    if len(text) != _SECRET_LENGTH:
        return (
            f"is {len(text)} characters long; a secret is 43 base64url "
            f"characters, which is 32 bytes ({_GENERATE})"
        )
    if decode(text) is None:
        return f"does not end with zero unused bits; {_GENERATE}"
    return None


def secret(value: str, app: str, key_id: str) -> Secret:
    """The ``Secret`` of an admitted text."""
    data = decode(value)
    if data is None or len(data) != _SECRET_BYTES:  # pragma: no cover
        raise ValueError("not an admitted secret")
    return Secret(data, app, key_id)


def _strict(data: bytes) -> dict[str, object] | None:
    """An I-JSON object (spec 03, ADR 0019), or ``None``: UTF-8, no member
    name twice, no lone surrogate, no ``NaN`` or infinity, and integers with
    no fraction or exponent within ±(2^53 - 1); a number with a fraction or
    an exponent is read as a double, and must be finite.

    A text nested deeper than the json module reads (``RecursionError``) is
    ``None`` too, as ``read_principal`` treats it. Within the 4096-byte
    bound of a valid token no first part can be nested that deep, so this
    changes the outcome of no token that could be valid.
    """

    class _Refused(Exception):
        pass

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        found = dict(items)
        if len(found) != len(items):
            raise _Refused
        return found

    def constant(literal: str) -> NoReturn:
        raise _Refused

    def integer(literal: str) -> int:
        # Bounded before int() reads it, so no long text is converted.
        if len(literal.lstrip("-")) > len(str(SAFE_INTEGER_MAX)):
            raise _Refused
        number = int(literal)
        if abs(number) > SAFE_INTEGER_MAX:
            raise _Refused
        return number

    def double(literal: str) -> float:
        number = float(literal)
        if not math.isfinite(number):  # such as 1e400
            raise _Refused
        return number

    try:
        text = data.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=pairs,
            parse_float=double,
            parse_constant=constant,
            parse_int=integer,
        )
    except (_Refused, ValueError, RecursionError):
        return None
    if type(value) is not dict or not _texts_encode(value):
        return None
    found: dict[str, object] = value
    return found


def _texts_encode(value: object) -> bool:
    """Whether every string in a decoded JSON value, member names included,
    is encodable as UTF-8: no lone surrogate.
    """
    stack = [value]
    while stack:
        item = stack.pop()
        if type(item) is str:
            try:
                item.encode("utf-8")
            except UnicodeEncodeError:
                return False
        elif type(item) is dict:
            stack.extend(item.keys())
            stack.extend(item.values())
        elif type(item) is list:
            stack.extend(item)
    return True


def _is_typ(value: object) -> bool:
    return type(value) is str and value.isascii() and value.lower() in _TYPES


def _token_of(authorization: str) -> str | None:
    """The token of a ``Bearer`` credential, ``OWS`` at either end removed,
    or ``None`` for another scheme.
    """
    scheme, space, rest = authorization.strip(" \t").partition(" ")
    if not space or not scheme.isascii() or scheme.lower() != "bearer":
        return None
    return rest.lstrip(" ")


def check(
    authorization: object,
    identity: str,
    keys: Mapping[str, Mapping[str, Secret]],
    now: int,
) -> tuple[str | None, str | None]:
    """Check an ``Authorization`` value under spec 03's rules.

    Returns ``(app, None)`` for a valid token, ``(None, message)`` for a
    recognized token that is not valid, with the refusal of the first rule
    it breaks (Decision 2), and ``(None, None)`` to abstain. Private: call
    it only inside a boundary, as the shared-secret backend does.

    Args:
        authorization: The header's value, or ``None`` when it is absent.
        identity: The identity of the contract in the request path.
        keys: Each calling app this host accepts, with its secrets by key id.
        now: The provider's time, in whole seconds since the epoch.
    """
    if type(authorization) is not str:
        return None, None
    token = _token_of(authorization)
    if token is None:
        return None, None
    parts = token.split(".")
    first = decode(parts[0])
    header = None if first is None else _strict(first)
    if header is None or not _is_typ(header.get("typ")):
        return None, None
    # Recognized: from here on, every failure is a refusal (spec 03).
    if len(token) > LIMIT or len(parts) != 3 or any(decode(p) is None for p in parts):
        return None, PARTS
    claims = _strict(decode(parts[1]))  # type: ignore[arg-type]  # decoded above
    if (
        claims is None
        or header.keys() != _HEADER
        or claims.keys() != _CLAIMS
        or any(type(header[name]) is not str for name in _HEADER)
        or type(claims["iss"]) is not str
        or type(claims["aud"]) is not str
        or type(claims["iat"]) is not int
        or type(claims["exp"]) is not int
    ):
        return None, MEMBERS
    if header["alg"] != _ALG:
        return None, ALG
    held = keys.get(claims["iss"])
    if held is None:
        return None, ISSUER
    secret = held.get(header["kid"])  # type: ignore[call-overload]
    if secret is None:
        return None, KID
    signature = decode(parts[2])
    signed = f"{parts[0]}.{parts[1]}".encode("ascii")
    expected = hmac.digest(secret._bytes, signed, "sha256")
    if signature is None or not hmac.compare_digest(signature, expected):
        return None, SIGNATURE
    if claims["aud"] != identity:
        return None, AUDIENCE
    iat, exp = claims["iat"], claims["exp"]
    if not iat < exp <= iat + _LONGEST:
        return None, LIFE
    if iat > now + _SKEW:
        return None, FUTURE
    if now >= exp + _SKEW:
        return None, EXPIRED
    return claims["iss"], None


def _signed(secret: Secret, identity: str, now: int, lifetime: int) -> str:
    """The token text: the header and claims as compact JSON, in spec 03's
    order, and the HMAC SHA-256 of the two parts as sent.
    """
    header = json.dumps(
        {"alg": _ALG, "typ": _TYP, "kid": secret.key_id}, separators=(",", ":")
    )
    claims = json.dumps(
        {"iss": secret.app, "aud": identity, "iat": now, "exp": now + lifetime},
        separators=(",", ":"),
    )
    signing = f"{_encode(header.encode())}.{_encode(claims.encode())}"
    signature = hmac.digest(secret._bytes, signing.encode("ascii"), "sha256")
    return f"{signing}.{_encode(signature)}"


def mint(secret: Secret, identity: str, now: int, lifetime: int = LIFETIME) -> Token:
    """A fresh token of the secret's app for a call to ``identity``, issued
    at ``now``, signed under ``secret`` with its key id.

    Raises:
        Failure: With fixed text and no context, if signing fails.
    """
    failed = False
    try:
        text = _signed(secret, identity, now, lifetime)
    except Exception:
        failed = True
    if failed:
        raise Failure("the token could not be signed")
    return Token(secret.app, identity, text)
