"""Shared assertion helpers.

Available to every suite without an import dance: ``tests/`` is on ``sys.path`` (the root
conftest puts ``backend/`` there), so ``from tests.helpers import does_not_raise`` works from
any test module.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import pytest


@contextlib.contextmanager
def does_not_raise(reason: str) -> Iterator[None]:
    """Assert the block completes without raising, and say why that matters.

    "Calling this must not raise" is a real invariant — containment paths, no-op seams and
    boundary-accepting validators all have it. But written as a bare call with a
    ``# must not raise`` comment it is indistinguishable from an empty test: the comment is
    not executable, and nothing reports *what* was expected when it does raise. Roughly 30
    tests in this suite were in that shape (issue #431).

    This makes the invariant explicit and gives the failure a sentence instead of a bare
    traceback::

        with does_not_raise("5 GB is under the 15 GB ceiling"):
            validate_file_size_for_tenant(5 * GB, None)

    Prefer a stronger assertion when one exists — a return value, a recorded side effect, a
    log line. Use this when "it completed" genuinely IS the contract.

    Args:
        reason: What the caller is asserting, phrased so the failure message reads as a
            sentence. Not optional: "did not raise" without a why is the problem being fixed.

    Raises:
        Failed: via ``pytest.fail``, if the block raises anything.
    """
    if not reason.strip():
        raise ValueError("does_not_raise(reason=...) needs a reason; that is the point of it")
    try:
        yield
    except BaseException as exc:  # noqa: BLE001 - re-reported as a test failure, not swallowed
        pytest.fail(f"{reason} — but it raised {type(exc).__name__}: {exc}")
