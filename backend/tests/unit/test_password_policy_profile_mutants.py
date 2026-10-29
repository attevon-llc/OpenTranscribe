"""Pins the SP 800-63B-4 screening added to ``PasswordPolicy`` (blocklist, context words,
online lookup, MFA-aware minimum length).

Mutation testing showed the existing profile suite let these survive: the context-word
list built from the email local part / full name, the ``email`` / ``full_name`` /
``mfa_protected`` arguments actually reaching the screening helpers, the operator
blocklist path and HIBP URL reaching their consumers, and the exact user-facing error
strings the SPA renders.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from app.auth import password_blocklist
from app.auth.password_policy import password_policy
from app.auth.password_policy import validate_password
from app.core.auth_settings import publish_process_auth_setting
from app.core.config import settings

LISTED = "qwertyuiop"
PASSPHRASE = "marble tractor quietly up"

COMMON_ERROR = (
    "Password is too common or appears in a list of known breached passwords; "
    "choose a different one"
)
CONTEXT_ERROR = "Password cannot contain the application name or words specific to your account"
BREACH_ERROR = "Password has appeared in a known data breach; choose a different one"


def _publish(**values: Any) -> None:
    for key, value in values.items():
        publish_process_auth_setting(key, value)


@pytest.fixture(autouse=True)
def _standard_profile_with_a_list(tmp_path, monkeypatch):
    _publish(
        password_policy_enabled=True,
        password_policy_profile="standard",
        password_blocklist_enabled=None,
        password_max_length=0,
        password_hibp_enabled=None,
    )
    path = tmp_path / "pwned-passwords-top100k-sha1.txt"
    digest = hashlib.sha1(LISTED.encode(), usedforsecurity=False)  # noqa: S324
    path.write_text(digest.hexdigest().upper() + "\n")
    monkeypatch.setattr(password_blocklist, "default_blocklist_path", lambda: path)
    monkeypatch.setattr(settings, "PASSWORD_BLOCKLIST_PATH", "")
    monkeypatch.setattr(settings, "PASSWORD_CONTEXT_WORDS", "")
    return path


class TestBlocklistErrorText:
    def test_listed_password_gets_the_exact_common_password_message(self):
        assert COMMON_ERROR in validate_password(LISTED, mfa_protected=True).errors


class TestContextWordsFromTheAccount:
    """``email`` / ``full_name`` must reach ``_check_blocklist`` and be split correctly."""

    def test_email_local_part_is_a_context_word(self):
        errors = validate_password(
            "zebrafish are lovely pets", email="zebrafish@corp.example"
        ).errors
        assert CONTEXT_ERROR in errors

    def test_email_domain_is_not_a_context_word(self):
        errors = validate_password(
            "visit corp.example daily ok", email="zebrafish@corp.example"
        ).errors
        assert CONTEXT_ERROR not in errors

    def test_full_name_parts_are_context_words(self):
        errors = validate_password(
            "bartholomew rides a bicycle", full_name="Bartholomew Quill"
        ).errors
        assert CONTEXT_ERROR in errors

    def test_unrelated_password_gets_no_context_error(self):
        errors = validate_password(
            PASSPHRASE, email="zebrafish@corp.example", full_name="Bartholomew Quill"
        ).errors
        assert CONTEXT_ERROR not in errors
        assert errors == []


class TestMfaAwareLengthDefaults:
    """Under ``standard`` an MFA-less account needs 15, an MFA-protected one only 8."""

    ELEVEN = "quartz mule"

    def test_validate_password_defaults_to_not_mfa_protected(self):
        assert any("at least 15" in e for e in validate_password(self.ELEVEN).errors)
        # the method has its own default, separate from the module-level wrapper's
        assert any(
            "at least 15" in e for e in password_policy.validate_password(self.ELEVEN).errors
        )

    def test_explicit_mfa_protected_gets_the_shorter_minimum(self):
        assert validate_password(self.ELEVEN, mfa_protected=True).errors == []

    def test_character_requirements_default_to_not_mfa_protected(self):
        assert any(
            "at least 15" in e for e in password_policy._check_character_requirements(self.ELEVEN)
        )
        assert password_policy._check_character_requirements(self.ELEVEN, True) == []

    def test_min_length_for_defaults_to_not_mfa_protected(self):
        assert password_policy.min_length_for() == 15
        assert password_policy.min_length_for(False) == 15
        assert password_policy.min_length_for(True) == 8

    def test_requirements_report_both_minimums(self):
        reqs = password_policy.get_policy_requirements()
        assert reqs["min_length"] == 15
        assert reqs["min_length_with_mfa"] == 8


class TestConfiguredPathsReachTheirConsumers:
    def test_requirements_report_the_operator_blocklist_path(self, tmp_path, monkeypatch):
        custom = tmp_path / "operator-list.txt"
        custom.write_text(
            hashlib.sha1(b"first-banned", usedforsecurity=False).hexdigest().upper()
            + "\n"
            + hashlib.sha1(b"second-banned", usedforsecurity=False).hexdigest().upper()
            + "\n"
        )
        monkeypatch.setattr(settings, "PASSWORD_BLOCKLIST_PATH", str(custom))

        status = password_policy.get_policy_requirements()["blocklist_status"]

        assert status["source"] == "custom"
        assert status["entries"] == 2

    def test_online_lookup_uses_the_configured_url_and_message(self, monkeypatch):
        monkeypatch.setattr(settings, "PASSWORD_HIBP_ENABLED", True)
        monkeypatch.setattr(settings, "PASSWORD_HIBP_URL", "https://hibp.invalid/range")
        suffix = hashlib.sha1(PASSPHRASE.encode(), usedforsecurity=False).hexdigest().upper()[5:]
        seen: list[str] = []

        class _Resp:
            status_code = 200
            text = f"{suffix}:7"

        def fake_get(url: str, **_kw: Any) -> _Resp:
            seen.append(url)
            return _Resp()

        monkeypatch.setattr(password_blocklist.httpx, "get", fake_get)

        errors = validate_password(PASSPHRASE).errors

        assert errors == [BREACH_ERROR]
        assert len(seen) == 1
        assert seen[0].startswith("https://hibp.invalid/range/")


class TestPartialHistoryDegradationLevel:
    """The partial case of the history-check degradation report.

    ``test_password_policy_controls`` covers only the completely-blind case (CRITICAL).
    When SOME entries still verify the control is merely degraded: ERROR, worded
    "degraded" / "partially enforced". Forcing CRITICAL for both makes every partly
    corrupt history page the on-call as if the control were off entirely.
    """

    @staticmethod
    def _run(caplog) -> None:
        import logging

        _publish(password_policy_profile="custom", password_history_count=5)

        def verify(_plain: str, hashed: str) -> bool:
            if hashed == "unreadable":
                raise ValueError("cannot parse")
            return False

        with caplog.at_level(logging.DEBUG, logger="app.auth.password_policy"):
            assert (
                password_policy.check_password_history(
                    new_password_hash="",
                    password_history=["readable", "unreadable"],
                    verify_func=verify,
                    plain_password="x",
                )
                is True
            )

    def test_a_partly_unverifiable_history_is_an_error_not_critical(self, caplog):
        import logging

        self._run(caplog)

        degraded = [r for r in caplog.records if "Password-history check was" in r.getMessage()]
        assert len(degraded) == 1
        assert degraded[0].levelno == logging.ERROR

    def test_the_partial_report_says_degraded_and_partially_enforced(self, caplog):
        self._run(caplog)

        message = next(
            r.getMessage() for r in caplog.records if "Password-history check was" in r.getMessage()
        )
        assert "was degraded: 1 of 2 entries" in message
        assert "The reuse control is partially enforced." in message
