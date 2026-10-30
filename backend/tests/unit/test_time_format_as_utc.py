"""``as_utc``: the one naive-to-aware coercion for timestamps read back from the database.

It replaced identical copies in ``auth/token_service.py`` (the calls now live in
``auth/refresh_tokens.py``) and ``services/support_access_service.py``. A naive value
compared with an aware one raises ``TypeError``, which inside an auth path surfaces as a 500
instead of a denial.
"""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from app.utils.time_format import as_utc


def test_none_passes_through():
    assert as_utc(None) is None


def test_a_naive_value_is_taken_to_be_utc():
    out = as_utc(datetime(2026, 10, 29, 12, 30, 0))
    assert out == datetime(2026, 10, 29, 12, 30, 0, tzinfo=UTC)
    assert out.tzinfo is UTC


def test_an_aware_utc_value_is_returned_unchanged():
    value = datetime(2026, 10, 29, 12, 30, 0, tzinfo=UTC)
    assert as_utc(value) is value


def test_an_aware_non_utc_value_keeps_its_own_offset_not_rewritten():
    plus_five = timezone(timedelta(hours=5))
    value = datetime(2026, 10, 29, 12, 30, 0, tzinfo=plus_five)
    out = as_utc(value)
    assert out is value
    assert out.utcoffset() == timedelta(hours=5)


def test_the_result_compares_with_an_aware_now_without_typeerror():
    result = as_utc(datetime(2000, 1, 1))
    assert result is not None
    assert result < datetime.now(UTC)
