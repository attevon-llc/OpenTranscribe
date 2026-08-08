"""python-jose-compatible shim over joserfc, for tests only.

python-jose was removed from requirements.txt/requirements-ci.txt when the app
switched to joserfc everywhere (#33/#34). A handful of tests deliberately
encode/decode tokens with a *second*, independent JWT library rather than
calling back into app.core.security, so a bug shared by the app's own
encode/decode path can't hide from the test that's supposed to catch it. This
shim keeps that independence while giving those tests joserfc's guarantees
instead of an uninstalled dependency. Only the functions those tests actually
call are implemented.
"""

from joserfc import jwt as _jwt
from joserfc.jwk import OctKey
from joserfc.jws import extract_compact


class jwt:
    @staticmethod
    def encode(claims: dict, key: str, algorithm: str) -> str:
        return _jwt.encode(
            {"alg": algorithm}, claims, OctKey.import_key(key), algorithms=[algorithm]
        )

    @staticmethod
    def decode(token: str, key: str, algorithms: list[str], options: dict | None = None) -> dict:
        # joserfc verifies signature/algorithm only — it never checks `aud` (or
        # `exp`) on its own, so python-jose's `options={"verify_aud": False}`
        # is already the joserfc default and needs no translation here.
        del options
        return dict(_jwt.decode(token, OctKey.import_key(key), algorithms=algorithms).claims)

    @staticmethod
    def get_unverified_header(token: str) -> dict:
        return dict(extract_compact(token.encode()).headers())
