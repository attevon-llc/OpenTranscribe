"""Lockout counts attempts per ACCOUNT, not per spelling of the account.

``lockout._normalize_identifier`` only lowercased, and ``login.py`` handed it the
raw submitted ``username``. An account reachable both as ``person@example.com`` and
as its ``ldap_uid`` therefore had **two independent counters**, so an attacker got
``2 x ACCOUNT_LOCKOUT_THRESHOLD`` attempts against it — the control's effective
threshold was silently doubled (and would multiply further with every extra alias).

The fix resolves the account once per attempt and keys the bucket on its email.
These tests pin both halves:

  1. ``canonical_identifier`` collapses aliases and — critically — produces the SAME
     bucket for an unknown address as a real account with that address would, so the
     unresolved fallback is not an existence oracle.
  2. ``login.py`` actually passes the canonical key to both lockout entry points, and
     resolves the account exactly ONCE (a second, conditional lookup would reintroduce
     a hit/miss timing difference).
"""

from __future__ import annotations

from typing import cast
from unittest.mock import patch

import pytest

from app.api.endpoints.auth import login as login_module
from app.auth.lockout import canonical_identifier
from app.core.auth_settings import DynamicAuthSettings


@pytest.mark.unit
class TestCanonicalIdentifier:
    """The pure key-derivation half."""

    def test_aliases_of_one_account_share_a_bucket(self):
        """An ldap_uid and an email for the same account map to one key."""
        by_uid = canonical_identifier("jdoe", "person@example.com")
        by_email = canonical_identifier("person@example.com", "person@example.com")

        assert by_uid == by_email == "person@example.com"

    def test_case_and_whitespace_do_not_split_the_bucket(self):
        """Normalisation still applies to the resolved address."""
        assert canonical_identifier("x", "  Person@Example.COM ") == "person@example.com"

    def test_unresolved_submission_falls_back_to_the_submitted_string(self):
        """No account resolved -> the normalized submission is the key."""
        assert canonical_identifier("  NoSuchUser ", None) == "nosuchuser"

    def test_unknown_address_uses_the_same_bucket_a_real_one_would(self):
        """The fallback must not reveal existence through the choice of bucket.

        This is the enumeration property. For the ordinary login form the submitted
        string IS the email, so an address that exists and an address that does not
        produce byte-identical keys; nothing about the bucket distinguishes them.
        """
        existing = canonical_identifier("victim@example.com", "victim@example.com")
        unknown = canonical_identifier("victim@example.com", None)

        assert existing == unknown


@pytest.mark.unit
class TestLoginWiresTheCanonicalKey:
    """The half that actually closes the doubled-threshold hole."""

    def test_lockout_calls_use_the_canonical_key_not_the_submitted_one(self):
        """Both ``check_and_record_attempt`` and ``get_lockout_info`` get the bucket.

        Guards the exact regression: ``_handle_lockout_check`` used to pass
        ``username`` straight through to both.
        """
        with (
            patch.object(
                login_module, "check_and_record_attempt", return_value=(False, None)
            ) as record,
            patch.object(
                login_module,
                "get_lockout_info",
                return_value={"failed_attempts": 1, "lockout_count": 0},
            ) as info,
            patch.object(login_module.audit_logger, "log_login_failure"),
        ):
            login_module._handle_lockout_check(
                username="jdoe",
                lockout_identifier="person@example.com",
                auth_success=False,
                client_ip="10.0.0.1",
                user_agent="pytest",
                auth_settings=cast(DynamicAuthSettings, _FakeAuthSettings()),
            )

        assert record.call_args.args[0] == "person@example.com"
        assert info.call_args.args[0] == "person@example.com"

    def test_audit_still_records_what_the_caller_typed(self):
        """The canonical key is for counting; the audit trail keeps the submission."""
        with (
            patch.object(login_module, "check_and_record_attempt", return_value=(False, None)),
            patch.object(
                login_module,
                "get_lockout_info",
                return_value={"failed_attempts": 1, "lockout_count": 0},
            ),
            patch.object(login_module.audit_logger, "log_login_failure") as failure,
        ):
            login_module._handle_lockout_check(
                username="jdoe",
                lockout_identifier="person@example.com",
                auth_success=False,
                client_ip="10.0.0.1",
                user_agent="pytest",
                auth_settings=cast(DynamicAuthSettings, _FakeAuthSettings()),
            )

        assert failure.call_args.kwargs["username"] == "jdoe"

    def test_account_is_resolved_exactly_once_per_attempt(self):
        """One lookup feeds both the exemption check and the bucket key.

        Two lookups (or a lookup that only runs when the first one misses) would put
        an account-existence signal back into the endpoint's timing profile, which is
        what makes the unresolved fallback safe in the first place.
        """
        source = _login_source()

        assert source.count("_resolve_lockout_account(") == 2, (
            "expected exactly one definition and one call site"
        )
        assert "_is_exempt_from_lockout(account)" in source, (
            "the exemption check must consume the already-resolved account"
        )

    def test_exemption_check_no_longer_queries_by_itself(self):
        """``_is_exempt_from_lockout`` takes a row, not a session + identifier."""
        import inspect

        params = list(inspect.signature(login_module._is_exempt_from_lockout).parameters)
        assert params == ["user"], params


class _FakeAuthSettings:
    """The resolved auth config values ``_handle_lockout_check`` reports."""

    account_lockout_threshold = 5
    account_lockout_duration_minutes = 15


def _login_source() -> str:
    """Return the login module's source for the single-lookup structural check."""
    import inspect

    return inspect.getsource(login_module)
