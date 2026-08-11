"""``tests/helpers.does_not_raise`` must actually fail. Otherwise it is worse than nothing.

This helper replaced ~30 bare calls carrying a ``# must not raise`` comment. If it silently
passed on an exception it would convert those into genuinely vacuous tests — the exact defect
it exists to remove — and it would do so across the whole suite at once. So it is pinned here
in both directions, the same way `test_http_exception_passthrough` pins its own detector.
"""

from __future__ import annotations

import pytest

from tests.helpers import does_not_raise


def test_it_passes_when_the_block_completes() -> None:
    with does_not_raise("a no-op must not raise"):
        pass


@pytest.mark.parametrize(
    "raised",
    [
        ValueError("boom"),
        RuntimeError("kaboom"),
        KeyboardInterrupt(),  # a BaseException, which `except Exception` would let through
    ],
)
def test_it_fails_when_the_block_raises(raised: BaseException) -> None:
    """Including BaseException — a KeyboardInterrupt mid-assertion must not read as a pass."""
    with pytest.raises(pytest.fail.Exception) as failure:
        with does_not_raise("this block was supposed to complete"):
            raise raised

    message = str(failure.value)
    assert "this block was supposed to complete" in message, "the reason must reach the report"
    assert type(raised).__name__ in message, "the failure must name the exception type"


def test_the_reason_is_mandatory() -> None:
    """A reasonless "did not raise" is the thing being fixed, so it is refused outright."""
    for blank in ("", "   ", "\t"):
        with pytest.raises(ValueError, match="needs a reason"):
            with does_not_raise(blank):
                pass
