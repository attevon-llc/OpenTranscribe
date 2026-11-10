"""An ID token with a valid signature but no ``sub`` is refused, never a 500.

``validate_token`` documents that a login attempt can never 500. ``sub`` is the identity
the account is keyed on, so the claims registry must require it: the refusal then comes from
``_decode_token`` (None) like every other bad claim, instead of a ``KeyError`` later.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

from joserfc import jwt
from joserfc.jwk import RSAKey

from app.auth.oidc.claims import _decode_token

KEY = RSAKey.generate_key(2048, private=True, parameters={"kid": "k1"})
JWKS = {"keys": [KEY.as_dict(private=False)]}
CFG: Any = SimpleNamespace(verify_audience=False, verify_issuer=False)


def _token(**claims: Any) -> str:
    payload = {"exp": int(time.time()) + 600, **claims}
    return jwt.encode({"alg": "RS256", "kid": "k1"}, payload, KEY)


def test_token_without_sub_is_refused():
    assert _decode_token(_token(email="a@example.com"), JWKS, CFG, "https://idp") is None


def test_token_with_sub_is_accepted():
    claims = _decode_token(_token(sub="user-1"), JWKS, CFG, "https://idp")

    assert claims is not None
    assert claims["sub"] == "user-1"
