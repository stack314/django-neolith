"""Hypothesis strategies shared by the generated-value tests."""

import warnings
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from hypothesis import strategies as st
from hypothesis.errors import HypothesisWarning
from hypothesis_jsonschema import from_schema

# A timestamp's instant falls in years 0001 to 9999 in UTC, and its text in
# those years too (CON-40, spec 01). The instant is drawn a day inside both
# ends, so every offset up to 23:59 keeps the UTC value and the local text in
# range. The bounds sit on the strategy; nothing is filtered.
_INSTANTS = st.datetimes(
    min_value=datetime(1, 1, 2),
    max_value=datetime(9999, 12, 30),
    timezones=st.just(UTC),
)
_OFFSETS = st.integers(-(24 * 60 - 1), 24 * 60 - 1)


@st.composite
def _timestamps(draw: st.DrawFn) -> str:
    instant = draw(_INSTANTS)
    minutes = draw(_OFFSETS)
    local = instant.astimezone(timezone(timedelta(minutes=minutes)))
    return local.isoformat().replace("+00:00", "Z")


TIMESTAMPS = _timestamps()


@st.composite
def admitted_by(draw: st.DrawFn, schema: dict[str, Any]) -> Any:  # noqa: ANN401
    """Values the description admits, with timestamps the spec admits.

    hypothesis-jsonschema draws any RFC 3339 text for ``date-time``,
    including an instant before year 1 in UTC, which a receiver refuses. Its
    warning that the format is overridden is the point here, not news.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", HypothesisWarning)
        return draw(from_schema(schema, custom_formats={"date-time": TIMESTAMPS}))
