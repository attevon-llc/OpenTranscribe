"""FIPS strict mode must refuse the legacy JWT algorithm (algorithm downgrade).

``core/security.verify_token`` decides which signature algorithms it will accept from
``FIPS_MIGRATION_MODE``:

* ``strict``     → ``[JWT_ALGORITHM_V3]``            (HS512 only)
* ``compatible`` → ``[JWT_ALGORITHM_V3, JWT_ALGORITHM]`` (HS512, then HS256 for migration)

Before this file ``FIPS_MIGRATION_MODE`` appeared in the tests exactly once, as an
assertion that the *configuration value* is one of two strings — which says nothing about
enforcement. Mutating the literal ``"strict"`` (or widening ``allowed_algorithms``)
therefore downgraded a strict deployment to compatible with the suite still green, and an
HS256-signed token would be accepted where the deployment's compliance posture says only
HS512 is permitted. That is an algorithm-downgrade bypass: the migration window, which is
supposed to be closable, silently stays open forever.

The FIPS mode is **published by the test rather than read from the ambient
environment**, in both directions. ``tests/CLAUDE.md``'s rule is not to assert FIPS
behaviour when FIPS is off; the way to honour that without an ``if`` around every
assertion (which the test auditor detects, correctly, as a test that cannot fail) is to
set the mode explicitly. Every test here then exercises a real branch of the real
function and gives the same verdict whichever mode the suite was launched in.
"""

from __future__ import annotations

import time

import pytest
from fastapi import HTTPException

from app.auth.constants import TOKEN_TYPE_ACCESS
from app.auth.constants import TOKEN_TYPE_MFA
from app.core.config import settings
from app.core.security import verify_token
from tests.jwt_compat import jwt

#: HS512 wants a 64-byte key, and ``config.py`` warns when ``JWT_SECRET_KEY`` is shorter.
#: Pinned here so these tests neither depend on nor are weakened by the operator's .env.
FIPS_SECRET = "unit-test-hs512-secret-padded-to-sixty-four-bytes-exactly-0123"

SUBJECT = "019ec90a-1b2c-7def-8000-00000000ff01"


def _token(algorithm: str) -> str:
    """Mint an otherwise-valid access token signed with *algorithm*.

    Encoded through ``tests/jwt_compat.py`` — a second, independent path into joserfc —
    rather than through the app's own token minting, so a bug shared by the app's
    encode/decode pair cannot hide from the test meant to catch it.
    """
    return jwt.encode(
        {
            "sub": SUBJECT,
            "type": TOKEN_TYPE_ACCESS,
            "jti": "downgrade-test-jti",
            "iat": int(time.time()),
            "exp": int(time.time()) + 600,
        },
        FIPS_SECRET,
        algorithm=algorithm,
    )


@pytest.fixture
def audited(monkeypatch) -> list[dict]:
    """Capture the compliance record ``verify_token`` writes for a legacy fallback."""
    from app.auth import audit as audit_module

    events: list[dict] = []
    monkeypatch.setattr(audit_module.audit_logger, "log", lambda **kw: events.append(kw))
    return events


@pytest.fixture
def fips_140_3(monkeypatch) -> None:
    """A FIPS 140-3 deployment. The migration mode is set per test."""
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", FIPS_SECRET)
    monkeypatch.setattr(settings, "FIPS_MODE", True)
    monkeypatch.setattr(settings, "FIPS_VERSION", "140-3")


@pytest.fixture
def strict_mode(monkeypatch, fips_140_3) -> None:
    monkeypatch.setattr(settings, "FIPS_MIGRATION_MODE", "strict")


@pytest.fixture
def compatible_mode(monkeypatch, fips_140_3) -> None:
    monkeypatch.setattr(settings, "FIPS_MIGRATION_MODE", "compatible")


def _legacy() -> str:
    return _token(settings.JWT_ALGORITHM)


def _v3() -> str:
    return _token(settings.JWT_ALGORITHM_V3)


class TestStrictModeRefusesTheLegacyAlgorithm:
    """Consequence prevented: an HS256 token accepted by a deployment that has closed
    its FIPS migration window — the whole point of ``strict``."""

    def test_a_legacy_signed_token_is_rejected(self, strict_mode):
        with pytest.raises(HTTPException) as exc:
            verify_token(_legacy())

        assert exc.value.status_code == 401

    def test_the_rejection_does_not_disclose_why(self, strict_mode):
        """The 401 body is the same one an expired or forged token gets."""
        with pytest.raises(HTTPException) as exc:
            verify_token(_legacy())

        assert exc.value.detail == "Invalid authentication credentials"

    def test_the_v3_algorithm_is_still_accepted(self, strict_mode):
        """The control: strict is narrow, not broken. Without this the test above would
        pass just as well if ``verify_token`` rejected everything."""
        payload = verify_token(_v3())

        assert payload["sub"] == SUBJECT

    def test_no_fallback_is_audited_because_none_happened(self, strict_mode, audited):
        with pytest.raises(HTTPException):
            verify_token(_legacy())

        assert audited == []


class TestCompatibleModeAcceptsTheLegacyAlgorithm:
    """Consequence prevented, in the other direction: hardening the default and signing
    every existing session out of a mid-migration deployment."""

    def test_a_legacy_signed_token_is_accepted(self, compatible_mode):
        payload = verify_token(_legacy())

        assert payload["sub"] == SUBJECT

    def test_the_v3_algorithm_is_accepted_too(self, compatible_mode):
        payload = verify_token(_v3())

        assert payload["sub"] == SUBJECT

    def test_the_legacy_acceptance_is_audited(self, compatible_mode, audited):
        """Compliance tracking is how an operator knows the window can be closed."""
        verify_token(_legacy())

        assert [e["details"]["warning"] for e in audited] == ["legacy_algorithm_fallback"]

    def test_a_v3_token_is_not_audited_as_a_fallback(self, compatible_mode, audited):
        verify_token(_v3())

        assert audited == []


class TestOutsideFipsBothAlgorithmsVerify:
    """Consequence prevented: a non-FIPS deployment (the default) inheriting strict's
    refusal and rejecting its own tokens."""

    @pytest.fixture
    def non_fips(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "JWT_SECRET_KEY", FIPS_SECRET)
        monkeypatch.setattr(settings, "FIPS_MODE", False)
        # Deliberately left at "strict": outside FIPS 140-3 the migration mode is not
        # consulted at all, and a mutation that read it unconditionally would fail here.
        monkeypatch.setattr(settings, "FIPS_MIGRATION_MODE", "strict")

    def test_a_legacy_signed_token_is_accepted(self, non_fips):
        payload = verify_token(_legacy())

        assert payload["sub"] == SUBJECT

    def test_a_v3_signed_token_is_accepted(self, non_fips):
        payload = verify_token(_v3())

        assert payload["sub"] == SUBJECT

    def test_nothing_is_audited_as_a_legacy_fallback(self, non_fips, audited):
        """The audit event is FIPS-specific; emitting it everywhere makes it noise."""
        verify_token(_legacy())

        assert audited == []


class TestPurposeBindingSurvivesEitherMode:
    """Consequence prevented: the algorithm decision being refactored in a way that
    skips the ``type`` check — the MFA half-token is signed with the same key, so that
    is a complete MFA bypass regardless of which algorithm is allowed."""

    @staticmethod
    def _mfa_half_token(algorithm: str) -> str:
        return jwt.encode(
            {
                "sub": SUBJECT,
                "type": TOKEN_TYPE_MFA,
                "jti": "downgrade-test-mfa",
                "iat": int(time.time()),
                "exp": int(time.time()) + 600,
            },
            FIPS_SECRET,
            algorithm=algorithm,
        )

    def test_strict_mode_refuses_a_non_access_token(self, strict_mode):
        with pytest.raises(HTTPException) as exc:
            verify_token(self._mfa_half_token(settings.JWT_ALGORITHM_V3))

        assert exc.value.status_code == 401

    def test_compatible_mode_refuses_a_non_access_token(self, compatible_mode):
        with pytest.raises(HTTPException) as exc:
            verify_token(self._mfa_half_token(settings.JWT_ALGORITHM))

        assert exc.value.status_code == 401


def test_the_two_algorithms_under_test_are_actually_different() -> None:
    """Guard the guard: if V3 ever equalled the legacy algorithm, every test in this
    file would pass while proving nothing about a downgrade."""
    assert settings.JWT_ALGORITHM != settings.JWT_ALGORITHM_V3
