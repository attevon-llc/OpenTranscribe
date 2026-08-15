"""``core.security.verify_token`` — the claim checks joserfc does NOT do for you.

Written from surviving mutants (issue #431). ``app/core/security.py`` had 133 survivors and
`_verify_token` the largest share; these are the ones a caller can observe.

The headline is the same shape found earlier in ``dependencies.py`` and, being the same shape
in a *different* function, it needed its own test: **joserfc verifies the signature and the
algorithm and nothing else.** It does not check `exp`. `verify_token` therefore validates it
explicitly with ``JWTClaimsRegistry(exp={"essential": True})``, and `essential` decides what
happens to a token that carries **no `exp` claim at all**:

* ``True``  — a token without `exp` is rejected.
* ``False`` — a token without `exp` is **accepted, forever**. It is correctly signed, so
  every other check passes. There is no revocation window because there is no expiry.

Flipping that one flag survived the whole suite. Minting such a token requires the signing
key, so this is not an unauthenticated forgery — but any path that mints a token without
setting `exp` produces a credential that never dies, and nothing would have told us.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi import HTTPException
from joserfc import jwt
from joserfc.jwk import OctKey

from app.auth.constants import TOKEN_TYPE_ACCESS
from app.auth.constants import TOKEN_TYPE_REFRESH
from app.core import security
from app.core.config import settings


def _mint(claims: dict[str, Any], *, algorithm: str | None = None) -> str:
    """Sign ``claims`` with the app's real key — no mocking of the crypto.

    The point of these tests is what `verify_token` accepts, so the token has to be genuinely
    valid apart from the claim under test. A stand-in signer would let a rejection pass for
    the wrong reason.
    """
    key = OctKey.import_key(settings.JWT_SECRET_KEY)
    header = {"alg": algorithm or settings.JWT_ALGORITHM}
    return jwt.encode(header, claims, key)


def test_a_token_with_no_exp_claim_is_rejected():
    """The mutant: ``exp={"essential": True}`` -> ``False`` makes this token IMMORTAL.

    joserfc does not check `exp` on its own, so with `essential` false a token that simply
    omits the claim validates forever. Every other check — signature, algorithm, type —
    passes, and there is no expiry to revoke against.
    """
    token = _mint({"sub": "1", "type": TOKEN_TYPE_ACCESS})

    with pytest.raises(HTTPException) as exc:
        security.verify_token(token)

    assert exc.value.status_code == 401


def test_a_token_with_a_future_exp_is_accepted():
    """Positive control: "reject everything" would satisfy the test above.

    Without this, deleting the claims validation entirely — or hard-failing — reads as a pass.
    """
    payload = security.verify_token(
        _mint({"sub": "1", "type": TOKEN_TYPE_ACCESS, "exp": int(time.time()) + 600})
    )

    assert payload["sub"] == "1"


def test_an_expired_token_is_rejected():
    """The ordinary case, asserted here because it is what `exp` is FOR.

    Kept alongside the no-`exp` test so the pair distinguishes "the claim is validated" from
    "the claim is merely required to be present".
    """
    with pytest.raises(HTTPException) as exc:
        security.verify_token(
            _mint({"sub": "1", "type": TOKEN_TYPE_ACCESS, "exp": int(time.time()) - 60})
        )

    assert exc.value.status_code == 401


def test_a_wrong_type_rejection_carries_the_www_authenticate_header():
    """``headers={"WWW-Authenticate": "Bearer"}`` -> ``None`` survived.

    Not cosmetic: the header is what tells a client this is an *authentication* failure rather
    than an authorization one, and `nginx`'s `auth_request` treats only a 401 as
    unauthenticated. Dropping it degrades a clear challenge into a bare refusal.
    """
    refresh_like = _mint({"sub": "1", "type": TOKEN_TYPE_REFRESH, "exp": int(time.time()) + 600})

    with pytest.raises(HTTPException) as exc:
        security.verify_token(refresh_like, expected_type=TOKEN_TYPE_ACCESS)

    assert exc.value.status_code == 401
    assert exc.value.headers == {"WWW-Authenticate": "Bearer"}
    assert exc.value.detail, "a 401 with no detail tells the caller nothing"


def test_expected_type_none_accepts_any_type_deliberately():
    """`expected_type=None` is an opt-out, and it must really opt out.

    Pinned because the default is `access`: a mutant changing the default, or the `is not
    None` guard, would silently make every caller that passes `None` start enforcing a type —
    breaking the WebSocket and MFA paths that check purpose elsewhere.
    """
    token = _mint({"sub": "1", "type": TOKEN_TYPE_REFRESH, "exp": int(time.time()) + 600})

    payload = security.verify_token(token, expected_type=None)

    assert payload["type"] == TOKEN_TYPE_REFRESH


def test_the_default_expected_type_is_access():
    """The default is the security property: a caller must opt OUT deliberately.

    Asserted on the signature rather than by behaviour, because a mutant flipping the default
    to `None` would make every existing type-binding test pass while removing the binding
    from every caller that relies on the default.
    """
    import inspect

    default = inspect.signature(security.verify_token).parameters["expected_type"].default
    assert default == TOKEN_TYPE_ACCESS


def test_an_unparseable_token_is_a_401_not_a_crash():
    """`contextlib.suppress(JoseError)` around the header read.

    The suppression exists so a malformed token falls through to `decode`, which raises the
    proper 401. A mutant that changes what is suppressed turns a garbage `Authorization`
    header — trivially sent by anyone — into a 500.
    """
    with pytest.raises(HTTPException) as exc:
        security.verify_token("not-a-jwt")

    assert exc.value.status_code == 401


def test_a_token_signed_with_the_wrong_key_is_rejected():
    """The floor under everything above: none of it means anything if signatures are ignored."""
    foreign = jwt.encode(
        {"alg": settings.JWT_ALGORITHM},
        {"sub": "1", "type": TOKEN_TYPE_ACCESS, "exp": int(time.time()) + 600},
        OctKey.import_key("a-different-secret-of-sufficient-length-for-hs256-xxxxx"),
    )

    with pytest.raises(HTTPException) as exc:
        security.verify_token(foreign)

    assert exc.value.status_code == 401
