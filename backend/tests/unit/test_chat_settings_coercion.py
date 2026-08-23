"""A stored chat setting outside its declared range must not break the panel.

`GET /admin/chat-settings` validates its response against the same ge/le bounds
the registry declares, so ONE row holding an out-of-range value returned a 500
for the ENTIRE settings payload — and the admin UI is the only place to fix such
a row, so the 500 broke the tool you would fix it with.

The API validates on write, which makes this look like "you edited the database
by hand". The real hazard is an upgrade: **tightening a bound in a later release
puts every deployment whose stored value exceeds the new bound into that 500**,
immediately, with no bad input from anyone.

Values are therefore CLAMPED, not replaced by the coded default: an operator who
raised a ceiling should land on the highest value allowed, not be silently
dropped below the number they started from.
"""

from __future__ import annotations

import pytest

from app.core.chat_flag_registry import CHAT_FLAG_REGISTRY
from app.services.chat.settings import BOUNDS
from app.services.chat.settings import DEFAULTS
from app.services.chat.settings import _coerce


def _spec(field: str):
    return next(s for s in CHAT_FLAG_REGISTRY if s.field == field)


def test_a_value_above_the_ceiling_is_clamped_to_the_ceiling():
    # The exact shape that produced the 500: messages_per_hour has le=10000 and
    # the row held 100000.
    spec = _spec("messages_per_hour")
    assert spec.le == 10000, "this test describes the shipped bound; update both together"

    assert _coerce("messages_per_hour", "100000") == 10000


def test_a_value_below_the_floor_is_clamped_to_the_floor():
    spec = _spec("max_concurrent_streams")
    assert spec.ge == 1

    assert _coerce("max_concurrent_streams", "0") == 1


def test_an_in_range_value_is_returned_untouched():
    # The control. Without it, "clamping works" also passes for an
    # implementation that returns the bound for every input.
    assert _coerce("messages_per_hour", "500") == 500
    assert _coerce("max_concurrent_streams", "3") == 3


def test_clamping_preserves_the_field_type():
    # `ge`/`le` come off the spec as numbers that may be float; an int field
    # returning 10000.0 would serialize differently and defeat the point.
    clamped = _coerce("messages_per_hour", "100000")
    assert isinstance(clamped, int)
    assert not isinstance(clamped, bool)


def test_a_non_numeric_value_still_falls_back_to_the_default():
    # The pre-existing behaviour, which clamping must not have replaced: a value
    # of the wrong TYPE has no position on the scale to clamp to.
    assert _coerce("messages_per_hour", "not-a-number") == DEFAULTS["messages_per_hour"]


def test_an_unset_value_is_the_coded_default():
    assert _coerce("messages_per_hour", None) == DEFAULTS["messages_per_hour"]


def test_bools_are_untouched_by_clamping():
    # Bool flags carry no bounds, so they must not acquire one.
    assert "trace_enabled" not in BOUNDS
    assert _coerce("trace_enabled", "false") is False
    assert _coerce("trace_enabled", "true") is True


@pytest.mark.parametrize(
    "spec", [s for s in CHAT_FLAG_REGISTRY if s.ge is not None or s.le is not None]
)
def test_every_bounded_field_survives_a_wildly_out_of_range_row(spec):
    """Whole-registry sweep: no bounded field may return something the schema rejects.

    Parametrized over the registry rather than a hand-picked list, so a flag
    added later is covered without anyone remembering to add it here — which is
    the same reason `SETTING_KEYS` and `DEFAULTS` are derived rather than
    hand-written.
    """
    # Substituting infinities for an absent bound keeps this ONE unconditional
    # assertion. Written as two `if`s it read as a test that could execute no
    # assertion at all — which for a field declaring neither bound it would,
    # passing vacuously. The parametrize already excludes those, but a shape
    # that only works because of a filter somewhere else is the shape this
    # repo's auditor exists to reject.
    low = spec.ge if spec.ge is not None else float("-inf")
    high = spec.le if spec.le is not None else float("inf")
    coerced = _coerce(spec.field, "999999999")

    assert low <= coerced <= high, (
        f"{spec.field} left its declared range [{low}, {high}]: {coerced}"
    )
