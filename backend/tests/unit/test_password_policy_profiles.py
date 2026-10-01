"""Password policy profiles (nist | stig | custom), blocklist and admin MFA (issue #1107).

Everything runs against the real policy object with settings published through the
process-level auth cache - no database, no HTTP client. The only fake is the HTTP
transport for the optional online breached-password lookup.
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast

import httpx
import pytest

from app.auth import password_blocklist
from app.auth.mfa_policy import mfa_required_for_user
from app.auth.password_policy import password_policy
from app.auth.password_policy import validate_password
from app.core.auth_settings import get_process_auth_settings
from app.core.auth_settings import publish_process_auth_setting
from app.core.config import Settings
from app.core.config import settings
from app.core.security import get_password_hash
from app.core.security import normalize_password
from app.core.security import verify_password

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Unlisted, composition-free (all lowercase) passphrase; 24 characters.
PASSPHRASE = "marble tractor quietly up"


def _publish(**values: Any) -> None:
    for key, value in values.items():
        publish_process_auth_setting(key, value)


def _errors(password: str, **kwargs: Any) -> list[str]:
    return validate_password(password, **kwargs).errors


@pytest.fixture(autouse=True)
def _no_ambient_overrides():
    """Pin everything the .env could otherwise change for these tests."""
    _publish(
        password_policy_enabled=True,
        password_blocklist_enabled=None,
        password_max_length=0,
    )


@pytest.fixture
def nist():
    _publish(password_policy_profile="nist")


@pytest.fixture
def stig():
    _publish(password_policy_profile="stig")


# --------------------------------------------------------------------------- default


class TestDefaultProfile:
    def test_code_default_is_stig_so_existing_installs_are_unchanged(self):
        assert Settings.model_fields["PASSWORD_POLICY_PROFILE"].default == "stig"

    def test_env_example_opts_new_installs_into_nist(self):
        text = (REPO_ROOT / ".env.example").read_text()
        assert "\nPASSWORD_POLICY_PROFILE=nist" in text

    def test_unset_profile_behaves_as_stig(self, monkeypatch):
        monkeypatch.setattr(settings, "PASSWORD_POLICY_PROFILE", "stig")
        assert password_policy.profile == "stig"
        assert password_policy.min_length == 12
        assert password_policy.max_age_days == 60

    def test_unknown_profile_falls_back_to_stig_and_says_so(self, caplog):
        _publish(password_policy_profile="nsit")
        with caplog.at_level(logging.WARNING):
            assert password_policy.profile == "stig"
        assert "nsit" in caplog.text

    def test_profile_name_is_case_and_whitespace_insensitive(self):
        _publish(password_policy_profile=" NIST ")
        assert password_policy.profile == "nist"


# ---------------------------------------------------------------------------- stig


class TestStigProfileIsUnchanged:
    def test_requirements(self, stig):
        assert password_policy.min_length == 12
        assert password_policy.require_uppercase
        assert password_policy.require_lowercase
        assert password_policy.require_digit
        assert password_policy.require_special
        assert password_policy.history_count == 24
        assert password_policy.max_age_days == 60
        assert password_policy.min_age_hours == 24

    def test_composition_still_enforced(self, stig):
        errors = _errors(PASSPHRASE)
        assert any("uppercase" in e for e in errors)
        assert any("digit" in e for e in errors)

    def test_blocklist_is_off(self, stig):
        assert password_policy.blocklist_enabled is False

    def test_passwords_expire(self, stig):
        old = datetime.now(UTC) - timedelta(days=61)
        assert password_policy.is_password_expired(old)

    def test_no_length_cap(self, stig):
        assert not any("at most" in e for e in _errors("Aa1!" * 100))

    def test_individual_env_vars_still_apply(self, stig):
        _publish(password_min_length=20)
        assert password_policy.min_length == 20


# ----------------------------------------------------------------------------- nist


class TestNistLength:
    def test_min_15_rejects_14(self, nist):
        assert any("at least 15" in e for e in _errors("a" * 7 + " " + "b" * 6))

    def test_min_15_accepts_15(self, nist):
        assert _errors("marble tractor u") == []

    def test_min_is_8_when_user_is_mfa_protected(self, nist):
        assert _errors("xq3vw8zk", mfa_protected=True) == []
        assert any("at least 8" in e for e in _errors("xq3vw8z", mfa_protected=True))

    def test_8_chars_rejected_without_mfa(self, nist):
        assert any("at least 15" in e for e in _errors("xq3vw8zk"))

    def test_64_chars_accepted_not_truncated(self, nist):
        assert _errors(("marble tractor " * 5)[:64]) == []

    def test_default_max_is_128(self, nist):
        assert _errors("a1 " * 42 + "xy") == []  # 128 chars
        assert any("at most 128" in e for e in _errors("a1 " * 43))

    def test_configured_max_cannot_go_below_64(self, nist):
        _publish(password_max_length=10)
        assert password_policy.max_length == 64

    def test_configured_max_above_64_is_honoured(self, nist):
        _publish(password_max_length=200)
        assert password_policy.max_length == 200


class TestNistNoCompositionOrExpiry:
    def test_no_composition_rules(self, nist):
        assert not password_policy.require_uppercase
        assert not password_policy.require_lowercase
        assert not password_policy.require_digit
        assert not password_policy.require_special
        assert _errors(PASSPHRASE) == []

    def test_no_periodic_expiry(self, nist):
        very_old = datetime.now(UTC) - timedelta(days=5000)
        assert password_policy.max_age_days == 0
        assert password_policy.expiry_cutoff() is None
        assert not password_policy.is_password_expired(very_old)
        assert password_policy.get_days_until_expiration(very_old) is None

    def test_no_minimum_age_and_no_history(self, nist):
        assert password_policy.min_age_hours == 0
        assert password_policy.history_count == 0
        just_now = datetime.now(UTC)
        assert password_policy.min_age_remaining(just_now) is None

    def test_composition_env_vars_are_ignored_under_nist(self, nist):
        _publish(password_require_special=True, password_max_age_days=30)
        assert not password_policy.require_special
        assert password_policy.max_age_days == 0

    def test_unicode_and_whitespace_allowed(self, nist):
        assert _errors("пароль со словами и 🐴 emoji") == []
        assert _errors("tab\tseparated passphrase here") == []

    def test_requirements_report_the_profile(self, nist):
        reqs = password_policy.get_policy_requirements()
        assert reqs["profile"] == "nist"
        assert reqs["min_length"] == 15
        assert reqs["max_length"] == 128
        assert reqs["max_age_days"] == 0
        assert reqs["blocklist_enabled"] is True


class TestNistNormalisation:
    def test_length_is_counted_after_nfkc(self, nist):
        # "ﬁ" (U+FB01) is 1 code point that NFKC-expands to "fi": 14 raw -> 15 normalised.
        password = "ﬁ" + "x" * 13
        assert len(password) == 14
        assert _errors(password) == []

    def test_fullwidth_spelling_of_listed_password_is_blocked(self, nist):
        fullwidth = "".join(chr(ord(c) + 0xFEE0) for c in "password1234567")
        assert any("common" in e for e in _errors(fullwidth))

    def test_normalize_password_is_nfkc(self):
        assert normalize_password("ﬁ") == "fi"
        assert normalize_password("é") == "é"

    def test_hash_and_verify_agree_across_unicode_forms(self):
        composed, decomposed = "café au lait ok", "café au lait ok"
        hashed = get_password_hash(composed)
        assert verify_password(decomposed, hashed)

    def test_hash_made_from_unnormalised_input_still_verifies(self):
        # A hash stored before normalisation existed was computed over the raw string.
        from app.core.security import pwd_context

        raw = "café au lait ok"
        legacy = pwd_context.hash(raw)
        assert verify_password(raw, legacy)


# ------------------------------------------------------------------------ blocklist


def _bundled_entry(min_len: int = 10) -> str:
    for line in password_blocklist.BUNDLED_BLOCKLIST_PATH.read_text().splitlines():
        if len(line) >= min_len and line.isalnum() and line.isascii():
            return line
    raise AssertionError("no suitable entry in bundled list")


class TestBlocklist:
    def test_bundled_list_is_substantial(self):
        assert len(password_blocklist.load_blocklist()) > 50_000

    def test_listed_password_rejected_under_nist(self, nist):
        assert any("common" in e for e in _errors(_bundled_entry(), mfa_protected=True))

    def test_case_insensitive(self, nist):
        assert any("common" in e for e in _errors(_bundled_entry().upper(), mfa_protected=True))

    def test_unlisted_password_passes(self, nist):
        assert _errors(PASSPHRASE) == []

    def test_off_under_stig_by_default(self, stig):
        assert password_policy.blocklist_enabled is False

    def test_on_under_custom_only_when_enabled(self):
        _publish(password_policy_profile="custom")
        assert password_policy.blocklist_enabled is False
        _publish(password_blocklist_enabled=True)
        assert password_policy.blocklist_enabled is True

    def test_explicit_false_disables_it_under_nist(self, nist):
        _publish(password_blocklist_enabled=False)
        assert password_policy.blocklist_enabled is False
        assert not any("common" in e for e in _errors(_bundled_entry(), mfa_protected=True))

    def test_explicit_true_enables_it_under_stig(self, stig):
        _publish(
            password_blocklist_enabled=True,
            password_min_length=8,
            password_require_uppercase=False,
            password_require_digit=False,
            password_require_special=False,
            password_policy_profile="custom",
        )
        assert any("common" in e for e in _errors(_bundled_entry()))

    def test_operator_path_replaces_bundled(self, nist, tmp_path, monkeypatch):
        custom = tmp_path / "list.txt"
        custom.write_text("zz-unique-corp-phrase-1\n")
        monkeypatch.setattr(settings, "PASSWORD_BLOCKLIST_PATH", str(custom))
        assert any("common" in e for e in _errors("zz-unique-corp-phrase-1"))
        assert not any("common" in e for e in _errors(_bundled_entry(), mfa_protected=True))

    def test_operator_list_edits_are_picked_up(self, nist, tmp_path, monkeypatch):
        custom = tmp_path / "list.txt"
        custom.write_text("first-banned-phrase-xx\n")
        monkeypatch.setattr(settings, "PASSWORD_BLOCKLIST_PATH", str(custom))
        assert any("common" in e for e in _errors("first-banned-phrase-xx"))
        custom.write_text("second-banned-phrase-x\n")
        import os

        os.utime(custom, (1, custom.stat().st_mtime + 5))
        assert any("common" in e for e in _errors("second-banned-phrase-x"))
        assert not any("common" in e for e in _errors("first-banned-phrase-xx"))

    def test_missing_operator_path_falls_back_to_bundled_and_logs(self, nist, monkeypatch, caplog):
        monkeypatch.setattr(settings, "PASSWORD_BLOCKLIST_PATH", "/nonexistent/list.txt")
        with caplog.at_level(logging.ERROR):
            assert any("common" in e for e in _errors(_bundled_entry(), mfa_protected=True))
        assert "/nonexistent/list.txt" in caplog.text


class TestContextWords:
    def test_app_name_rejected(self, nist):
        assert any("application name" in e for e in _errors("my opentranscribe login"))

    def test_email_local_part_rejected(self, nist):
        errors = _errors("wibblesworth is my name!", email="wibblesworth@example.com")
        assert any("email" in e for e in errors)

    def test_username_and_name_parts_rejected(self, nist):
        errors = _errors("zanzibar forever and ever", full_name="Zanzibar Quill")
        assert any("name" in e for e in errors)

    def test_operator_context_words(self, nist, monkeypatch):
        monkeypatch.setattr(settings, "PASSWORD_CONTEXT_WORDS", "acme corp,initech")
        assert any("application name" in e for e in _errors("we love initech here"))

    def test_short_words_ignored(self, nist, monkeypatch):
        monkeypatch.setattr(settings, "PASSWORD_CONTEXT_WORDS", "abc")
        assert _errors("abc marble tractor") == []


# --------------------------------------------------------------------- online lookup


class _Resp:
    def __init__(self, status_code: int = 200, text: str = ""):
        self.status_code, self.text = status_code, text


@pytest.fixture
def hibp_on(monkeypatch):
    monkeypatch.setattr(settings, "PASSWORD_HIBP_ENABLED", True)
    calls: list[dict[str, Any]] = []

    def fake_get(url: str, **kw: Any) -> _Resp:
        calls.append({"url": url, **kw})
        return _Resp(text="0000000000000000000000000000000000A:0\r\n")

    monkeypatch.setattr(password_blocklist.httpx, "get", fake_get)
    return calls


class TestOnlineLookup:
    def test_default_off_makes_no_request(self, nist, monkeypatch):
        called = []
        monkeypatch.setattr(password_blocklist.httpx, "get", lambda *a, **k: called.append(1))
        assert settings.PASSWORD_HIBP_ENABLED is False
        assert _errors(PASSPHRASE) == []
        assert called == []

    def test_only_five_char_prefix_is_sent(self, nist, hibp_on):
        _errors(PASSPHRASE)
        assert len(hibp_on) == 1
        sent = hibp_on[0]["url"].rsplit("/", 1)[-1]
        assert len(sent) == 5 and sent.isalnum()
        assert PASSPHRASE not in hibp_on[0]["url"]
        assert hibp_on[0]["timeout"] == settings.PASSWORD_HIBP_TIMEOUT_SECONDS

    def test_pwned_password_rejected(self, nist, monkeypatch):
        monkeypatch.setattr(settings, "PASSWORD_HIBP_ENABLED", True)
        import hashlib

        suffix = hashlib.sha1(PASSPHRASE.encode(), usedforsecurity=False).hexdigest().upper()[5:]
        monkeypatch.setattr(
            password_blocklist.httpx,
            "get",
            lambda url, **kw: _Resp(text=f"{suffix}:4242\r\nFFFF:1"),
        )
        assert any("breach" in e for e in _errors(PASSPHRASE))

    def test_padding_entries_with_zero_count_are_not_hits(self, nist, monkeypatch):
        monkeypatch.setattr(settings, "PASSWORD_HIBP_ENABLED", True)
        import hashlib

        suffix = hashlib.sha1(PASSPHRASE.encode(), usedforsecurity=False).hexdigest().upper()[5:]
        monkeypatch.setattr(
            password_blocklist.httpx, "get", lambda url, **kw: _Resp(text=f"{suffix}:0")
        )
        assert _errors(PASSPHRASE) == []

    @pytest.mark.parametrize(
        "outcome",
        [httpx.ConnectTimeout("slow"), httpx.ConnectError("down"), _Resp(503, "")],
    )
    def test_fails_open_and_logs(self, nist, monkeypatch, caplog, outcome):
        monkeypatch.setattr(settings, "PASSWORD_HIBP_ENABLED", True)

        def fake_get(url, **kw):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(password_blocklist.httpx, "get", fake_get)
        with caplog.at_level(logging.WARNING):
            assert _errors(PASSPHRASE) == []
        assert "failing open" in caplog.text

    def test_not_queried_when_password_already_rejected(self, nist, hibp_on):
        _errors("short")
        assert hibp_on == []


# ------------------------------------------------------------------------- custom


class TestCustomProfile:
    def test_env_vars_apply(self):
        _publish(
            password_policy_profile="custom",
            password_min_length=9,
            password_require_uppercase=False,
            password_require_lowercase=False,
            password_require_special=False,
            password_require_digit=True,
            password_max_age_days=10,
            password_history_count=3,
        )
        assert password_policy.min_length == 9
        assert any("digit" in e for e in _errors("nodigitshere"))
        assert _errors("has1digit") == []
        assert password_policy.max_age_days == 10
        assert password_policy.history_count == 3

    def test_nist_mfa_floor_does_not_apply(self):
        _publish(
            password_policy_profile="custom",
            password_min_length=12,
            password_require_uppercase=False,
            password_require_digit=False,
            password_require_special=False,
        )
        assert any("at least 12" in e for e in _errors("short pw 1", mfa_protected=True))


# ------------------------------------------------------------------------- MFA admin


class TestMfaRequiredForAdmins:
    def _user(self, role: str) -> Any:
        return SimpleNamespace(role=role)

    def test_setting_defaults_to_false(self):
        assert Settings.model_fields["MFA_REQUIRED_FOR_ADMINS"].default is False

    def test_off_by_default_nobody_required(self):
        _publish(mfa_enabled=True, mfa_required=False, mfa_required_for_admins=False)
        assert not mfa_required_for_user(get_process_auth_settings(), self._user("admin"))

    def test_admin_required_when_enabled(self):
        _publish(mfa_enabled=True, mfa_required=False, mfa_required_for_admins=True)
        s = get_process_auth_settings()
        assert mfa_required_for_user(s, self._user("admin"))
        assert not mfa_required_for_user(s, self._user("user"))
        assert not mfa_required_for_user(s, self._user("manager"))

    def test_global_required_covers_everyone(self):
        _publish(mfa_enabled=True, mfa_required=True, mfa_required_for_admins=False)
        assert mfa_required_for_user(get_process_auth_settings(), self._user("user"))

    def test_nothing_required_if_mfa_feature_is_off(self):
        _publish(mfa_enabled=False, mfa_required=True, mfa_required_for_admins=True)
        monkey_enabled = settings.MFA_ENABLED
        assert not monkey_enabled  # ambient default
        assert not mfa_required_for_user(get_process_auth_settings(), self._user("admin"))

    def test_env_example_documents_it(self):
        assert "MFA_REQUIRED_FOR_ADMINS" in (REPO_ROOT / ".env.example").read_text()


class _FakeQuery:
    def __init__(self, row: Any):
        self._row = row

    def filter(self, *_a: Any, **_k: Any) -> _FakeQuery:
        return self

    def first(self) -> Any:
        return self._row


class _FakeDb:
    """Only ``db.query(UserMFA.id).filter(...).first()`` is reached."""

    def __init__(self, enrolled: bool):
        self._enrolled = enrolled

    def query(self, *_a: Any) -> _FakeQuery:
        return _FakeQuery((1,) if self._enrolled else None)


class TestMfaProtectedFloorAtRealCallSites:
    def _user(self, role: str = "user") -> Any:
        return SimpleNamespace(id=1, role=role, email="someone@example.com", full_name=None)

    def test_enrolled_user_gets_the_8_char_floor(self, nist):
        from app.auth.mfa_policy import user_is_mfa_protected

        assert user_is_mfa_protected(_FakeDb(True), self._user())
        assert not user_is_mfa_protected(_FakeDb(False), self._user())

    def test_user_forced_to_enrol_counts_as_protected(self, nist, monkeypatch):
        from app.auth.mfa_policy import user_is_mfa_protected

        monkeypatch.setattr(settings, "MFA_ENABLED", True)
        monkeypatch.setattr(settings, "MFA_REQUIRED", True)
        assert user_is_mfa_protected(_FakeDb(False), self._user())

    def test_enforce_password_policy_uses_it(self, nist):
        from fastapi import HTTPException

        from app.services.account_security_service import enforce_password_policy

        enforce_password_policy("xq3vw8zk", self._user(), cast(Any, _FakeDb(True)))  # 8 chars, MFA
        with pytest.raises(HTTPException):
            enforce_password_policy("xq3vw8zk", self._user(), cast(Any, _FakeDb(False)))
        with pytest.raises(HTTPException):
            enforce_password_policy("xq3vw8zk", self._user())
