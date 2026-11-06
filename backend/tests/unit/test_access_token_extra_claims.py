"""The access-token issuer carries extra claims, but never lets them replace the reserved ones.

``app.auth.direct_auth.create_access_token`` is the one access-token issuer and the documented
way for an extension to mint a token with claims of its own (``data`` holds ``sub`` plus any
extras). ``exp``, ``iat``, ``jti`` and ``type`` are stamped by the issuer: a caller-supplied value
for any of them must lose, or an extra claim could re-purpose the token (``type``), outlive the
session ceiling (``exp``) or dodge revocation by reusing a known ``jti``.
"""

from __future__ import annotations

import time
from datetime import timedelta

from app.auth.direct_auth import create_access_token
from app.core.auth_types import TOKEN_TYPE_ACCESS
from app.core.security import verify_token

SUBJECT = "4b1d6f8e-0000-4000-8000-000000000001"
FAR_FUTURE = 4_102_444_800  # 2100-01-01
FIXED_JTI = "caller-chosen-jti"


def test_extra_claims_round_trip_beside_the_subject() -> None:
    payload = verify_token(
        create_access_token({"sub": SUBJECT, "acting_for": "org-7", "scope": "support"})
    )

    assert payload["sub"] == SUBJECT
    assert payload["acting_for"] == "org-7"
    assert payload["scope"] == "support"
    assert payload["type"] == TOKEN_TYPE_ACCESS


def test_reserved_claims_in_data_are_overwritten_by_the_issuer() -> None:
    before = int(time.time())
    payload = verify_token(
        create_access_token(
            {
                "sub": SUBJECT,
                "type": "refresh",
                "exp": FAR_FUTURE,
                "iat": 0,
                "jti": FIXED_JTI,
            },
            expires_delta=timedelta(minutes=5),
        )
    )

    assert payload["type"] == TOKEN_TYPE_ACCESS
    assert payload["exp"] <= before + 5 * 60 + 5
    assert payload["iat"] >= before - 5
    assert payload["jti"] != FIXED_JTI
    assert payload["sub"] == SUBJECT


def test_each_token_gets_a_fresh_jti() -> None:
    first = verify_token(create_access_token({"sub": SUBJECT}))
    second = verify_token(create_access_token({"sub": SUBJECT}))

    assert first["jti"] != second["jti"]


def test_the_callers_dict_is_not_mutated() -> None:
    data = {"sub": SUBJECT, "acting_for": "org-7"}

    create_access_token(data)

    assert data == {"sub": SUBJECT, "acting_for": "org-7"}
