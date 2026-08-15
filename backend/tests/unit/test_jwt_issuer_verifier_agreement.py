# mypy: disable-error-code="arg-type"
# This suite passes structural stand-ins (a fake session, a recording Path) to
# signatures that declare Session/Path. Declared once here rather than as a cast at
# every call site — a cast buries the thing being asserted, and widening a production
# signature to suit a test is worse. Same convention as
# tests/unit/test_proxy_identity_consistency.py.
"""A token minted under a config must verify under that same config. Every type.

This is the invariant that three separate live defects violated at once, and it is
stated here as one property rather than as three regression tests, because the next
copy of "which algorithm?" will break it in a fourth way:

    for every token type t and every supported configuration c:
        a token minted by any issuer under c is accepted by every verifier of t under c.

What it would have caught, in the state of the tree before this file existed:

1. ``token_service.create_refresh_token`` selected its algorithm with
   ``JWT_ALGORITHM_V3 if FIPS_VERSION == "140-3" else "HS256"``. ``FIPS_VERSION``
   defaults to ``"140-3"``, so **every deployment signed refresh tokens with HS512**
   while its access tokens were HS256. It stayed invisible for as long as it did
   because the one refresh verifier tried both algorithms — but
   ``api/endpoints/auth/sessions.py``'s logout handler, which documents itself as
   accepting a refresh token ("logging out with a refresh token is still a logout"),
   decodes with ``[settings.JWT_ALGORITHM]`` and so silently revoked nothing.
2. ``token_service.create_token`` carried a third inline copy of the same branch,
   with a different gate.
3. ``core.security.verify_token`` — the WebSocket and SAML verifier — accepted
   ``[JWT_ALGORITHM_V3]`` and nothing else under ``FIPS_MIGRATION_MODE=strict``,
   while the HTTP verifiers hardcoded ``[settings.JWT_ALGORITHM]``. No issuer in
   this codebase has ever minted ``JWT_ALGORITHM_V3``, so turning strict on refused
   **every WebSocket handshake** while HTTP requests carried on working.

The configuration is **published by the test**, never read from the ambient
environment, so each case exercises a real branch and gives the same verdict whether
the suite was launched with ``FIPS_MODE=true`` or without — the rule established by
``tests/unit/test_jwt_algorithm_downgrade.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from joserfc.errors import JoseError
from joserfc.jwk import OctKey

from app.auth.constants import TOKEN_TYPE_ACCESS
from app.auth.constants import TOKEN_TYPE_MFA
from app.auth.constants import TOKEN_TYPE_REFRESH
from app.core.config import settings
from app.core.security import accepted_algorithms
from app.core.security import signing_algorithm
from app.core.security import verify_token
from tests.jwt_compat import jwt

#: HS512 wants a 512-bit key and ``config.py`` warns when ``JWT_SECRET_KEY`` is
#: shorter. Pinned so these tests neither depend on nor are weakened by the
#: operator's .env.
SECRET = "agreement-suite-hs512-secret-padded-to-sixty-four-bytes-01234567"

SUBJECT = "019ec90a-1b2c-7def-8000-0000000ab1e5"

#: ``(FIPS_MODE, FIPS_VERSION, FIPS_MIGRATION_MODE, JWT_ALGORITHM)``.
#:
#: The last element matters: ``JWT_ALGORITHM=HS512`` is the documented way to
#: actually run HS512 (``docs-site/docs/operations/security-hardening.md``), and it
#: is the only configuration in which strict mode has anything to refuse. Without it
#: the strict rows would all be HS256-signs-HS256 and would not exercise a closed
#: migration window at all.
CONFIGURATIONS = [
    pytest.param((False, "140-2", "compatible", "HS256"), id="non-fips"),
    pytest.param((False, "140-3", "compatible", "HS256"), id="non-fips-default-version"),
    pytest.param((False, "140-3", "strict", "HS256"), id="non-fips-strict-is-ignored"),
    pytest.param((True, "140-2", "compatible", "HS256"), id="fips-140-2"),
    pytest.param((True, "140-3", "compatible", "HS256"), id="fips-140-3-compatible"),
    pytest.param((True, "140-3", "strict", "HS256"), id="fips-140-3-strict"),
    pytest.param((True, "140-3", "compatible", "HS512"), id="fips-140-3-compatible-hs512"),
    pytest.param((True, "140-3", "strict", "HS512"), id="fips-140-3-strict-hs512"),
    pytest.param((False, "140-3", "compatible", "HS512"), id="non-fips-hs512"),
]


class _FakeSession:
    """The narrowest stand-in that ``create_refresh_token`` actually uses.

    Deliberately not the ``db_session`` fixture: refresh-token *signing* is the
    defect under test and it has nothing to do with the database, so this test must
    not be one of the ~5,000 that queue on the shared Postgres. ``add``/``commit``/
    ``refresh`` are the whole surface; ``get_auth_settings`` raises on this object
    and the issuer's own ``except Exception`` degrades to the ``.env`` values, which
    is the documented behaviour (``_session_lifetime_minutes``).
    """

    def add(self, _obj) -> None: ...

    def commit(self) -> None: ...

    def refresh(self, _obj) -> None: ...


@pytest.fixture
def configured(monkeypatch, request) -> None:
    """Publish one row of :data:`CONFIGURATIONS` onto ``settings``."""
    fips_mode, fips_version, migration_mode, algorithm = request.param
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", SECRET)
    monkeypatch.setattr(settings, "FIPS_MODE", fips_mode)
    monkeypatch.setattr(settings, "FIPS_VERSION", fips_version)
    monkeypatch.setattr(settings, "FIPS_MIGRATION_MODE", migration_mode)
    monkeypatch.setattr(settings, "JWT_ALGORITHM", algorithm)
    monkeypatch.setattr(settings, "JWT_ALGORITHM_V3", "HS512")


@pytest.fixture
def silent_audit(monkeypatch) -> None:
    """Keep the audit logger off the network; it is not what is under test here."""
    from app.auth import audit as audit_module

    monkeypatch.setattr(audit_module.audit_logger, "log", lambda **_kw: None)


def _issuers() -> dict[str, tuple[str, Callable[[], str]]]:
    """Every JWT issuer in the codebase, keyed by name -> (token type, mint).

    Includes the two that no production caller reaches
    (``core.security.create_access_token``, ``token_service.create_token``) on
    purpose: an unreachable issuer is exactly where a divergent copy survives long
    enough to be treated as the pattern to follow.
    """
    from app.api.endpoints.auth.mfa_tokens import _create_mfa_token
    from app.auth.direct_auth import create_access_token as production_access_issuer
    from app.auth.token_service import TokenService
    from app.core.security import create_access_token as core_access_issuer

    service = TokenService()

    return {
        "auth.direct_auth.create_access_token": (
            TOKEN_TYPE_ACCESS,
            lambda: production_access_issuer({"sub": SUBJECT, "role": "user"}),
        ),
        "core.security.create_access_token": (
            TOKEN_TYPE_ACCESS,
            lambda: core_access_issuer(subject=SUBJECT),
        ),
        "auth.token_service.create_token[access]": (
            TOKEN_TYPE_ACCESS,
            lambda: service.create_token({"sub": SUBJECT}, token_type=TOKEN_TYPE_ACCESS),
        ),
        "auth.token_service.create_token[refresh]": (
            TOKEN_TYPE_REFRESH,
            lambda: service.create_token({"sub": SUBJECT}, token_type=TOKEN_TYPE_REFRESH),
        ),
        "auth.token_service.create_refresh_token": (
            TOKEN_TYPE_REFRESH,
            lambda: service.create_refresh_token(
                db=_FakeSession(), user_id=1, user_uuid=SUBJECT, role="user"
            )[0],
        ),
        "api.endpoints.auth.mfa_tokens._create_mfa_token": (
            TOKEN_TYPE_MFA,
            lambda: _create_mfa_token(SUBJECT, "user"),
        ),
    }


def _verifiers() -> dict[str, tuple[tuple[str, ...], Callable[[str, str], object]]]:
    """Every verifier the algorithm decision reaches, keyed by name.

    The value is ``(token types it handles, verify(token, token_type))``.
    """
    from app.auth.token_service import TokenService

    service = TokenService()

    def request_path(token: str, token_type: str) -> object:
        """What ``api/endpoints/auth/dependencies.py`` does, byte for byte.

        Reproduced rather than called because both dependencies there need a
        ``Request``, a database and a live user row. That the real module builds its
        list this same way is enforced statically by
        ``tests/unit/test_jwt_algorithm_single_owner.py``, which fails if either
        decode site stops calling ``accepted_algorithms``.
        """
        from joserfc import jwt as joserfc_jwt

        key = OctKey.import_key(settings.JWT_SECRET_KEY)
        return joserfc_jwt.decode(token, key, algorithms=accepted_algorithms(token_type)).claims

    return {
        "core.security.verify_token": (
            (TOKEN_TYPE_ACCESS, TOKEN_TYPE_REFRESH, TOKEN_TYPE_MFA),
            lambda token, token_type: verify_token(token, expected_type=token_type),
        ),
        "auth.token_service.verify_token_with_fallback": (
            (TOKEN_TYPE_REFRESH,),
            lambda token, _token_type: service.verify_token_with_fallback(token),
        ),
        "api.endpoints.auth.dependencies (request path)": (
            (TOKEN_TYPE_ACCESS, TOKEN_TYPE_REFRESH, TOKEN_TYPE_MFA),
            request_path,
        ),
    }


class TestEveryIssuerAgreesWithEveryVerifier:
    """The invariant, exhaustively: issuers × verifiers × configurations."""

    @pytest.mark.parametrize("configured", CONFIGURATIONS, indirect=True)
    def test_every_minted_token_verifies_under_the_config_that_minted_it(
        self, configured, silent_audit
    ):
        issuers = _issuers()
        verifiers = _verifiers()
        assert issuers, "no issuers under test — this test is not looking at anything"
        assert verifiers, "no verifiers under test — this test is not looking at anything"

        checked = 0
        for issuer_name, (token_type, mint) in issuers.items():
            token = mint()
            algorithm = jwt.get_unverified_header(token)["alg"]

            assert algorithm in accepted_algorithms(token_type), (
                f"{issuer_name} signed a {token_type} token with {algorithm}, which is "
                f"not in the accepted set {accepted_algorithms(token_type)} for this "
                f"configuration — every consumer of that token would 401"
            )

            for verifier_name, (handles, verify) in verifiers.items():
                if token_type not in handles:
                    continue
                try:
                    verify(token, token_type)
                except (JoseError, Exception) as exc:  # noqa: B014 - HTTPException too
                    pytest.fail(
                        f"{verifier_name} refused a {token_type} token from "
                        f"{issuer_name} (alg={algorithm}) under FIPS_MODE="
                        f"{settings.FIPS_MODE}, FIPS_VERSION={settings.FIPS_VERSION}, "
                        f"FIPS_MIGRATION_MODE={settings.FIPS_MIGRATION_MODE}, "
                        f"JWT_ALGORITHM={settings.JWT_ALGORITHM}: {exc!r}"
                    )
                checked += 1

        # Guard the guard: a refactor that empties `handles` would make the loop
        # above assert nothing while still passing.
        assert checked >= len(issuers), (
            f"only {checked} issuer/verifier pairs were exercised for "
            f"{len(issuers)} issuers — the matrix collapsed"
        )

    @pytest.mark.parametrize("configured", CONFIGURATIONS, indirect=True)
    def test_the_signing_algorithm_is_always_accepted(self, configured):
        """The property the two owner functions must satisfy by construction.

        Stated separately from the round-trip above because it is the thing a future
        change is most likely to break: narrowing acceptance (a "hardening") without
        noticing that it excluded what this deployment issues.
        """
        for token_type in (TOKEN_TYPE_ACCESS, TOKEN_TYPE_REFRESH, TOKEN_TYPE_MFA):
            assert signing_algorithm(token_type) in accepted_algorithms(token_type), (
                f"{token_type}: signs with {signing_algorithm(token_type)} but accepts "
                f"only {accepted_algorithms(token_type)}"
            )
            assert accepted_algorithms(token_type)[0] == signing_algorithm(token_type), (
                "the signing algorithm must be tried first"
            )


class TestRefreshTokensNoLongerCarryTheFipsProfileByDefault:
    """The specific live defect: HS512 refresh tokens on every non-FIPS install.

    Consequence prevented: an ordinary deployment issuing one token type under a
    profile it never enabled, with a ``JWT_SECRET_KEY`` that ``config.py`` only
    requires to be 64 bytes when HS512 was asked for.
    """

    @pytest.fixture
    def non_fips(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "JWT_SECRET_KEY", SECRET)
        monkeypatch.setattr(settings, "FIPS_MODE", False)
        # The default, and the whole point: FIPS_VERSION says 140-3 on every
        # deployment, so a gate that reads it alone is unconditionally true.
        monkeypatch.setattr(settings, "FIPS_VERSION", "140-3")
        monkeypatch.setattr(settings, "JWT_ALGORITHM", "HS256")
        monkeypatch.setattr(settings, "JWT_ALGORITHM_V3", "HS512")

    def test_a_refresh_token_is_signed_with_the_same_algorithm_as_an_access_token(
        self, non_fips, silent_audit
    ):
        from app.auth.direct_auth import create_access_token
        from app.auth.token_service import TokenService

        access = create_access_token({"sub": SUBJECT, "role": "user"})
        refresh, _row = TokenService().create_refresh_token(
            db=_FakeSession(), user_id=1, user_uuid=SUBJECT, role="user"
        )

        assert jwt.get_unverified_header(refresh)["alg"] == "HS256"
        assert jwt.get_unverified_header(refresh)["alg"] == jwt.get_unverified_header(access)["alg"]

    def test_the_deployment_wide_knob_moves_refresh_tokens_too(
        self, non_fips, monkeypatch, silent_audit
    ):
        """The control: refresh is not pinned to HS256, it follows ``JWT_ALGORITHM``.

        Without this, the test above would pass equally well if the algorithm had
        simply been hardcoded — which is the other way to be wrong.
        """
        from app.auth.token_service import TokenService

        monkeypatch.setattr(settings, "JWT_ALGORITHM", "HS512")
        refresh, _row = TokenService().create_refresh_token(
            db=_FakeSession(), user_id=1, user_uuid=SUBJECT, role="user"
        )

        assert jwt.get_unverified_header(refresh)["alg"] == "HS512"

    def test_a_refresh_token_signed_before_this_change_still_verifies(self, non_fips, silent_audit):
        """MIGRATION SAFETY. Every session in every live deployment is an HS512
        refresh token today; changing the signer must not sign any of them out.

        Encoded through ``tests/jwt_compat.py`` — a second, independent path into
        joserfc — rather than by reverting the issuer, so this keeps testing the
        real thing after the old branch is gone.
        """
        from app.auth.token_service import TokenService

        legacy = jwt.encode(
            {
                "sub": SUBJECT,
                "type": TOKEN_TYPE_REFRESH,
                "jti": "pre-change-refresh-jti",
                "iat": int(datetime.now(UTC).timestamp()),
                "exp": int((datetime.now(UTC) + timedelta(days=7)).timestamp()),
            },
            SECRET,
            algorithm="HS512",
        )

        payload = TokenService().verify_token_with_fallback(legacy)

        assert payload["sub"] == SUBJECT
        assert payload["type"] == TOKEN_TYPE_REFRESH


class TestStrictModeStillVerifiesItsOwnTokens:
    """The WebSocket regression, pinned.

    ``core.security.verify_token`` is the verifier for WebSocket handshakes and the
    SAML paths. Under ``FIPS_MODE=true`` + ``FIPS_VERSION=140-3`` +
    ``FIPS_MIGRATION_MODE=strict`` it accepted ``[JWT_ALGORITHM_V3]`` only — an
    algorithm no issuer in this codebase produces — so a strict deployment refused
    every WebSocket connection while HTTP requests, which decoded with a different
    hardcoded list, kept working. Closing the migration window must narrow
    acceptance to what this deployment SIGNS, which is a policy, not an outage.
    """

    @pytest.fixture
    def fips_strict(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "JWT_SECRET_KEY", SECRET)
        monkeypatch.setattr(settings, "FIPS_MODE", True)
        monkeypatch.setattr(settings, "FIPS_VERSION", "140-3")
        monkeypatch.setattr(settings, "FIPS_MIGRATION_MODE", "strict")
        monkeypatch.setattr(settings, "JWT_ALGORITHM", "HS256")
        monkeypatch.setattr(settings, "JWT_ALGORITHM_V3", "HS512")

    def test_a_production_access_token_verifies(self, fips_strict):
        from app.auth.direct_auth import create_access_token

        payload = verify_token(create_access_token({"sub": SUBJECT, "role": "user"}))

        assert payload["sub"] == SUBJECT

    def test_a_refresh_token_verifies(self, fips_strict, silent_audit):
        from app.auth.token_service import TokenService

        service = TokenService()
        token, _row = service.create_refresh_token(
            db=_FakeSession(), user_id=1, user_uuid=SUBJECT, role="user"
        )

        assert service.verify_token_with_fallback(token)["sub"] == SUBJECT

    def test_strict_is_still_narrow(self, fips_strict):
        """The control. Strict must refuse the algorithm it does not sign with,
        or the test above would pass just as well against a verifier that accepts
        everything."""
        assert accepted_algorithms(TOKEN_TYPE_ACCESS) == ["HS256"]
        assert "HS512" not in accepted_algorithms(TOKEN_TYPE_ACCESS)


def test_the_two_configured_algorithms_are_actually_different() -> None:
    """Guard the guard: if ``JWT_ALGORITHM_V3`` ever equalled ``JWT_ALGORITHM``,
    every dual-accept assertion in this file would pass while proving nothing."""
    assert settings.JWT_ALGORITHM != settings.JWT_ALGORITHM_V3
