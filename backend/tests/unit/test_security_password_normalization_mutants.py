"""Pins the NFKC-normalisation / legacy-hash fallback in ``core.security``.

``verify_password`` and ``verify_and_update_password`` hash the NFKC-normalised
password, then fall back to the RAW string so hashes written before normalisation
existed keep working. Mutation testing showed the fallback's guard and its call
arguments could be changed with the suite still green (15 survivors), i.e. a user
with a pre-normalisation hash and a non-ASCII passphrase could be locked out, or a
wrong password accepted, unnoticed.
"""

from app.core.security import get_password_hash
from app.core.security import pwd_context
from app.core.security import verify_and_update_password
from app.core.security import verify_password

# "e" + COMBINING ACUTE ACCENT: NFKC composes it to a single "é", so the raw and
# normalised forms differ (WRONG_NON_ASCII is decomposed too, so its raw != normalised).
DECOMPOSED = "cafe\u0301-passphrase"
COMPOSED = "caf\u00e9-passphrase"
WRONG_NON_ASCII = "cafe\u0301-other-phrase"


def _legacy_hash(raw: str) -> str:
    """A hash computed over the raw string, as before normalisation existed."""
    return str(pwd_context.hash(raw))


class TestVerifyPasswordLegacyFallback:
    def test_a_pre_normalisation_hash_still_verifies(self):
        assert verify_password(DECOMPOSED, _legacy_hash(DECOMPOSED)) is True

    def test_a_wrong_non_ascii_password_is_rejected_by_the_fallback(self):
        assert verify_password(WRONG_NON_ASCII, _legacy_hash(DECOMPOSED)) is False

    def test_a_wrong_ascii_password_is_rejected(self):
        assert verify_password("wrong-ascii", get_password_hash("right-ascii")) is False

    def test_composed_and_decomposed_forms_are_interchangeable_on_new_hashes(self):
        hashed = get_password_hash(DECOMPOSED)
        assert verify_password(COMPOSED, hashed) is True
        assert verify_password(DECOMPOSED, hashed) is True


class TestVerifyAndUpdatePasswordLegacyFallback:
    def test_a_pre_normalisation_hash_still_verifies(self):
        is_valid, _new_hash = verify_and_update_password(DECOMPOSED, _legacy_hash(DECOMPOSED))
        assert is_valid is True

    def test_a_wrong_non_ascii_password_is_rejected(self):
        is_valid, _new_hash = verify_and_update_password(WRONG_NON_ASCII, _legacy_hash(DECOMPOSED))
        assert is_valid is False

    def test_a_valid_new_style_hash_is_not_re_verified_against_the_raw_string(self):
        # Valid on the first pass: the raw-string retry would FAIL against a hash
        # of the normalised form, so running it anyway flips a correct login to False.
        hashed = get_password_hash(DECOMPOSED)
        is_valid, _new_hash = verify_and_update_password(DECOMPOSED, hashed)
        assert is_valid is True

    def test_an_ascii_password_round_trips(self):
        hashed = get_password_hash("plain-ascii")
        assert verify_and_update_password("plain-ascii", hashed)[0] is True
        assert verify_and_update_password("not-it", hashed)[0] is False
