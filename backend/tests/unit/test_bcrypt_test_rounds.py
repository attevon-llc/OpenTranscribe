"""The test-only bcrypt work factor must be fast in tests and inert everywhere else.

`app/core/security._bcrypt_rounds` lowers bcrypt's cost so the suite stops spending ~600 s
of CPU hashing throwaway passwords (issue #431). That is a work factor, not an algorithm
change — but it is still a security control being relaxed, so the conditions under which it
relaxes are pinned here rather than left to a code comment.

The gate is deliberately two independent conditions, matching the contract for ``TESTING``
documented at ``app/main.py:153-163``: the flag alone is never enough, because ``TESTING``
leaking into a real deployment must not weaken hashing.
"""

from __future__ import annotations

import importlib

import pytest
from passlib.context import CryptContext

from app.core import security


def _rounds_with(
    monkeypatch: pytest.MonkeyPatch,
    *,
    testing: str | None,
    hardened: bool,
    override: str | None = None,
):
    """Evaluate ``_bcrypt_rounds()`` under a specific environment."""
    if testing is None:
        monkeypatch.delenv("TESTING", raising=False)
    else:
        monkeypatch.setenv("TESTING", testing)
    if override is None:
        monkeypatch.delenv("TEST_BCRYPT_ROUNDS", raising=False)
    else:
        monkeypatch.setenv("TEST_BCRYPT_ROUNDS", override)
    monkeypatch.setattr(type(security.settings), "is_hardened", property(lambda self: hardened))
    return security._bcrypt_rounds()


def test_the_suite_gets_the_cheap_work_factor(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _rounds_with(monkeypatch, testing="True", hardened=False) == security.BCRYPT_TEST_ROUNDS


@pytest.mark.parametrize(
    "testing,hardened,why",
    [
        ("True", True, "TESTING leaked into a hardened deployment"),
        (None, False, "unhardened but not a test process"),
        ("False", False, "TESTING explicitly off"),
        (None, True, "ordinary production"),
    ],
)
def test_production_cost_is_kept_whenever_either_gate_fails(
    monkeypatch: pytest.MonkeyPatch, testing: str | None, hardened: bool, why: str
) -> None:
    """Both gates must hold. Either one failing means the real work factor."""
    assert _rounds_with(monkeypatch, testing=testing, hardened=hardened) == (
        security.BCRYPT_DEFAULT_ROUNDS
    ), why


def test_the_override_is_honoured_and_floored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A custom value is respected, but never below bcrypt's own minimum of 4."""
    assert _rounds_with(monkeypatch, testing="True", hardened=False, override="7") == 7
    assert _rounds_with(monkeypatch, testing="True", hardened=False, override="1") == 4
    # Garbage must not crash the process at import time — fall back to production cost.
    assert (
        _rounds_with(monkeypatch, testing="True", hardened=False, override="not-a-number")
        == security.BCRYPT_DEFAULT_ROUNDS
    )


def test_a_low_round_hash_still_round_trips_as_bcrypt_sha256() -> None:
    """The scheme under test is unchanged; only its cost moves.

    This is the substantive claim: lowering the work factor must not change which algorithm
    the app uses or break verification, because the rounds are embedded in the hash.
    """
    cheap = CryptContext(schemes=["bcrypt_sha256"], bcrypt_sha256__default_rounds=4)
    hashed = cheap.hash("password123")

    assert hashed.startswith("$bcrypt-sha256$")
    assert cheap.verify("password123", hashed)
    assert not cheap.verify("wrong-password", hashed)

    # A context configured for the production factor still verifies a cheap hash, which is
    # what makes the override safe: rounds travel with the hash, not with the context.
    strong = CryptContext(
        schemes=["bcrypt_sha256"], bcrypt_sha256__default_rounds=security.BCRYPT_DEFAULT_ROUNDS
    )
    assert strong.verify("password123", hashed)


def test_fips_iterations_are_not_reachable_by_the_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """PBKDF2 cost must stay real: ``tests/test_fips_140_3.py`` asserts the exact count.

    The knob only touches bcrypt. If a future change routed it through
    ``_get_pbkdf2_iterations`` the FIPS suite would fail — this pins the boundary directly so
    the reason is recorded next to the knob instead of surfacing as a confusing FIPS failure.
    """
    monkeypatch.setenv("TESTING", "True")
    monkeypatch.setenv("TEST_BCRYPT_ROUNDS", "4")
    monkeypatch.setattr(type(security.settings), "is_hardened", property(lambda self: False))

    assert security._get_pbkdf2_iterations() in (
        security.settings.PBKDF2_ITERATIONS,
        security.settings.PBKDF2_ITERATIONS_V3,
    )
    assert security._get_pbkdf2_iterations() >= 210_000


def test_the_module_imports_cleanly_under_the_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """``pwd_context`` is built at import time, so the knob must be safe to evaluate there."""
    monkeypatch.setenv("TESTING", "True")
    monkeypatch.setenv("TEST_BCRYPT_ROUNDS", "4")
    reloaded = importlib.reload(security)
    try:
        assert reloaded.pwd_context.hash("x").startswith(("$bcrypt-sha256$", "$pbkdf2-sha256$"))
    finally:
        importlib.reload(security)
