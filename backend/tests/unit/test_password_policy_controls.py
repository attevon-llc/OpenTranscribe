"""The password-policy controls nothing was exercising (FedRAMP IA-5).

Four separate controls in ``app/auth/password_policy.py`` could be deleted with the
whole suite still green. Each test below names the consequence it prevents.

* **Password reuse** (``check_password_history``). ``rg check_password_history
  backend/tests`` returned ZERO hits before this file: the FedRAMP IA-5 reuse control
  had no test at all. Flipping ``not self.enabled or`` to ``self.enabled or`` makes the
  function return ``True`` immediately whenever the policy is **on**, so every user may
  set their current password again — and, worse, the forced-change flow becomes a no-op
  the user can satisfy with the credential an administrator just handed them.
* **The minimum-length boundary** (``_check_character_requirements``). No test used a
  password of *exactly* ``min_length``, so ``<`` could become ``<=`` and silently
  require 13 characters where the policy says 12 — a deployment-wide rejection of
  compliant passwords, reported as "password too short" with no way to satisfy it.
* **The personal-information thresholds** (``_check_personal_info``). The
  four-character email-local-part and three-character name-part floors exist so
  ``a@x.com`` / a two-letter name cannot make every password illegal. Nothing pinned
  either side of either threshold.
* **The weak-pattern warnings** (``_check_common_patterns``). ``rg '\\.warnings'
  backend/tests`` was EMPTY: nothing asserted a warning is ever produced, so the whole
  pattern list could be deleted (or made unconditional) unnoticed.
* **``max_age_days <= 0``** (``expiry_cutoff``). ``0`` is the documented "never
  expires". Narrowing the guard to ``< 0`` makes the cutoff ``now``, so **every**
  password reads as expired and every account in the deployment is force-reset on next
  login. Only the *setting's* acceptance of 0 was tested, never the behaviour.

Everything here runs against the real policy object with its settings published through
the process-level auth cache — no database, no HTTP client. One test drives the real
``verify_password``/``get_password_hash`` pair, at bcrypt's unhardened test work factor,
because the wiring at the real call site is part of the control.
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest

from app.auth import password_history as history_service
from app.auth import password_policy as policy_module
from app.auth.password_policy import password_policy

#: A password that satisfies every default requirement and trips no weak-pattern rule.
#: Exactly 12 characters — the default ``password_min_length`` — so it doubles as the
#: on-the-boundary case.
AT_MIN_LENGTH = "Tr0ub4dor&3x"

#: The same password one character short.
BELOW_MIN_LENGTH = "Tr0ub4dor&3"


def _publish(**values: Any) -> None:
    """Make the policy see *values* without a database.

    Every requirement is a property resolved through ``get_process_auth_settings()``
    (DB ``auth_config`` > ``.env`` > coded default), so publishing the effective value
    is the supported way to stand a deployment's policy up in a test. The autouse
    ``_clear_process_auth_cache`` fixture in ``tests/conftest.py`` tears it down.

    Published explicitly rather than relying on the coded defaults, because the ambient
    ``.env`` is free to change any of them and a test that only passes at one operator's
    settings is not a control.
    """
    from app.core.auth_settings import publish_process_auth_setting

    for key, value in values.items():
        publish_process_auth_setting(key, value)


class _Verifier:
    """A real callable stand-in for ``verify_password`` that records what it was asked.

    Structural, like ``test_account_lifecycle.py``'s fakes: the policy declares
    ``Callable[[str, str], bool]`` and cares about nothing else. Recording the calls is
    the only way to assert the verifier is *consulted* — a history check that never
    calls it cannot detect reuse no matter what it returns.
    """

    def __init__(self, raises: bool = False):
        self.calls: list[tuple[str, str]] = []
        self._raises = raises

    def __call__(self, plain: str, hashed: str) -> bool:
        self.calls.append((plain, hashed))
        if self._raises:
            raise ValueError("hash written under a scheme this build cannot verify")
        return hashed == _fake_hash(plain)


def _fake_hash(plain: str) -> str:
    """The stand-in hash format ``_Verifier`` recognises."""
    return f"hash::{plain}"


def _check(plain: str, history: list[str], verifier: _Verifier) -> bool:
    """Call the policy's reuse check the way ``password_history.py`` does."""
    return password_policy.check_password_history(
        new_password_hash="",
        password_history=history,
        verify_func=verifier,
        plain_password=plain,
    )


@pytest.fixture
def reuse_enforced():
    """A deployment that enforces the policy and remembers the last 5 passwords."""
    _publish(password_policy_enabled=True, password_history_count=5)


@pytest.fixture
def complexity_enforced():
    """The documented FedRAMP defaults, published so the ambient .env cannot move them."""
    _publish(
        password_policy_enabled=True,
        password_min_length=12,
        password_require_uppercase=True,
        password_require_lowercase=True,
        password_require_digit=True,
        password_require_special=True,
    )


# ── CONTROL 1: password reuse is refused ─────────────────────────────────────────


class TestPasswordReuseIsRefused:
    """Consequence prevented: a user (or an admin-forced change) re-setting a password
    that is already in the last N, which is how a forced reset becomes a no-op."""

    def test_a_password_still_in_history_is_refused(self, reuse_enforced):
        verifier = _Verifier()

        assert _check("Reused-Pass-9", [_fake_hash("Reused-Pass-9")], verifier) is False

    def test_a_genuinely_new_password_is_allowed(self, reuse_enforced):
        """The control that makes the refusal above mean something."""
        verifier = _Verifier()

        assert _check("Brand-New-Pass-9", [_fake_hash("Something-Else-1")], verifier) is True

    def test_the_verifier_is_consulted_for_every_stored_hash(self, reuse_enforced):
        """A check that never calls the verifier cannot detect reuse at all."""
        history = [_fake_hash(f"Old-Pass-{n}") for n in range(3)]
        verifier = _Verifier()

        _check("Brand-New-Pass-9", history, verifier)

        assert [hashed for _plain, hashed in verifier.calls] == history

    def test_it_stops_at_the_first_match(self, reuse_enforced):
        """Reuse is refused on evidence, not after exhausting the list."""
        verifier = _Verifier()

        _check("Reused-Pass-9", [_fake_hash("Reused-Pass-9"), _fake_hash("Older-1")], verifier)

        assert verifier.calls == [("Reused-Pass-9", _fake_hash("Reused-Pass-9"))]

    def test_a_disabled_policy_permits_reuse_without_consulting_the_verifier(self):
        """Turning the policy off must turn the check off — in both directions."""
        _publish(password_policy_enabled=False, password_history_count=5)
        verifier = _Verifier()

        allowed = _check("Reused-Pass-9", [_fake_hash("Reused-Pass-9")], verifier)

        assert allowed is True
        assert verifier.calls == []

    def test_a_history_count_of_zero_disables_the_check(self):
        """``0`` is the documented "remember nothing"; the policy stays on for length etc."""
        _publish(password_policy_enabled=True, password_history_count=0)
        verifier = _Verifier()

        allowed = _check("Reused-Pass-9", [_fake_hash("Reused-Pass-9")], verifier)

        assert allowed is True
        assert verifier.calls == []

    def test_an_empty_history_allows_anything(self, reuse_enforced):
        verifier = _Verifier()

        assert _check("Reused-Pass-9", [], verifier) is True

    def test_a_blank_history_entry_is_skipped_not_verified(self, reuse_enforced):
        """A NULL/empty stored hash is not evidence either way, and must not be verified."""
        verifier = _Verifier()

        allowed = _check("Brand-New-Pass-9", ["", _fake_hash("Older-1")], verifier)

        assert allowed is True
        assert verifier.calls == [("Brand-New-Pass-9", _fake_hash("Older-1"))]

    def test_the_module_level_wrapper_refuses_reuse_too(self, reuse_enforced):
        """Callers use the wrapper, whose argument ORDER differs from the method's."""
        verifier = _Verifier()

        allowed = policy_module.check_password_history(
            "Reused-Pass-9", [_fake_hash("Reused-Pass-9")], verifier
        )

        assert allowed is False


class TestOnlyTheConfiguredWindowIsChecked:
    """Consequence prevented: ``password_history[: self.history_count]`` losing its
    bound — either checking nothing (reuse permitted) or checking forever (a user can
    run out of passwords they are allowed to choose)."""

    def test_a_hash_inside_the_window_is_refused(self):
        _publish(password_policy_enabled=True, password_history_count=2)
        history = [_fake_hash("Older-1"), _fake_hash("Reused-Pass-9"), _fake_hash("Ancient-3")]

        assert _check("Reused-Pass-9", history, _Verifier()) is False

    def test_a_hash_beyond_the_window_is_allowed_again(self):
        """The whole point of a bounded history: old enough is reusable."""
        _publish(password_policy_enabled=True, password_history_count=2)
        history = [_fake_hash("Older-1"), _fake_hash("Older-2"), _fake_hash("Reused-Pass-9")]

        assert _check("Reused-Pass-9", history, _Verifier()) is True

    def test_nothing_beyond_the_window_is_even_verified(self):
        _publish(password_policy_enabled=True, password_history_count=2)
        history = [_fake_hash("Older-1"), _fake_hash("Older-2"), _fake_hash("Ancient-3")]
        verifier = _Verifier()

        _check("Brand-New-Pass-9", history, verifier)

        assert [hashed for _plain, hashed in verifier.calls] == history[:2]


class TestAnUnverifiableEntryDoesNotBlockTheChange:
    """Deliberately NOT fail-closed — do not "fix" this into a refusal.

    Refusing the change would leave the user on their CURRENT password, which is a
    guaranteed reuse and strictly worse. The degradation is made loud instead (issue
    #324), so a history that has silently stopped being checked does not look identical
    to one that passed.
    """

    def test_an_entry_that_cannot_be_verified_permits_the_change(self, reuse_enforced):
        verifier = _Verifier(raises=True)

        assert _check("Brand-New-Pass-9", [_fake_hash("Older-1")], verifier) is True

    def test_a_completely_blind_check_is_reported_as_critical(self, reuse_enforced, caplog):
        """``checked == 0`` is "the reuse control is NOT being enforced" — alertable."""
        verifier = _Verifier(raises=True)
        with caplog.at_level(logging.CRITICAL, logger="app.auth.password_policy"):
            _check("Brand-New-Pass-9", [_fake_hash("Older-1")], verifier)

        critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
        assert len(critical) == 1

    def test_a_healthy_history_check_reports_nothing(self, reuse_enforced, caplog):
        """The negative half, and the reason the alert is worth having.

        Found as a surviving mutant: `unverifiable = 0` → `= 1` makes `if unverifiable:`
        always true, so "the reuse control is NOT being enforced" is logged at ERROR or
        CRITICAL on **every** password change. Nothing noticed, because the only assertions
        on that log covered the case where it is supposed to fire.

        An alert that fires on every success is worse than no alert: it trains whoever reads
        it to ignore exactly the message that says a security control has stopped working.
        So the silence is part of the contract, not an absence of behaviour.
        """
        verifier = _Verifier()
        history = [_fake_hash("Older-1"), _fake_hash("Older-2")]

        with caplog.at_level(logging.ERROR, logger="app.auth.password_policy"):
            assert _check("Brand-New-Pass-9", history, verifier) is True

        noisy = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert noisy == [], (
            f"a fully verifiable history logged a degradation: {[r.getMessage() for r in noisy]}"
        )


class TestTheRealCallSiteConsultsTheRealVerifier:
    """Consequence prevented: the service wrapper the endpoints actually call drifting
    away from the policy — passing the wrong hash list, or not passing a verifier at
    all. Uses the app's real bcrypt hasher (rounds=4 while unhardened, ~3 ms)."""

    def test_reuse_is_refused_through_check_password_against_history(self, reuse_enforced):
        from app.core.security import get_password_hash

        password = "Correct-Horse-Battery-9"
        db = _HistoryDB([get_password_hash(password)])

        assert history_service.check_password_against_history(cast(Any, db), 1, password) is False

    def test_a_new_password_is_allowed_through_the_same_path(self, reuse_enforced):
        from app.core.security import get_password_hash

        db = _HistoryDB([get_password_hash("Correct-Horse-Battery-9")])

        assert (
            history_service.check_password_against_history(cast(Any, db), 1, "Totally-Other-1")
            is True
        )


class _HistoryQuery:
    """Chainable ``Query`` stand-in: the filters/ordering are SQL, not logic."""

    def __init__(self, rows: list[Any]):
        self._rows = rows

    def filter(self, *_a: Any, **_k: Any) -> _HistoryQuery:
        return self

    def order_by(self, *_a: Any, **_k: Any) -> _HistoryQuery:
        return self

    def limit(self, _n: int) -> _HistoryQuery:
        return self

    def all(self) -> list[Any]:
        return self._rows


class _HistoryDB:
    """Minimal ``Session`` stand-in returning canned ``PasswordHistory`` rows."""

    def __init__(self, hashes: list[str]):
        self._rows: list[Any] = [SimpleNamespace(password_hash=h) for h in hashes]

    def query(self, *_a: Any, **_k: Any) -> _HistoryQuery:
        return _HistoryQuery(self._rows)


# ── CONTROL 2: the minimum-length boundary ───────────────────────────────────────


class TestMinimumLengthBoundary:
    """Consequence prevented: ``<`` becoming ``<=``, which rejects a password of exactly
    the advertised minimum — a policy no user can satisfy at the length it states."""

    def test_a_password_of_exactly_min_length_is_accepted(self, complexity_enforced):
        result = policy_module.validate_password(AT_MIN_LENGTH)

        assert result.is_valid is True
        assert result.errors == []

    def test_one_character_short_is_refused(self, complexity_enforced):
        result = policy_module.validate_password(BELOW_MIN_LENGTH)

        assert result.is_valid is False
        assert result.errors == [
            "Password must be at least 12 characters long (currently 11)",
        ]

    def test_the_configured_minimum_is_what_is_enforced(self):
        """Raising the setting must raise the floor — not a hardcoded 12."""
        _publish(
            password_policy_enabled=True,
            password_min_length=16,
            password_require_uppercase=True,
            password_require_lowercase=True,
            password_require_digit=True,
            password_require_special=True,
        )

        assert policy_module.validate_password(AT_MIN_LENGTH).is_valid is False

    def test_an_empty_password_is_refused_with_one_error(self, complexity_enforced):
        result = policy_module.validate_password("")

        assert result.errors == ["Password cannot be empty"]

    def test_a_disabled_policy_accepts_anything(self):
        _publish(password_policy_enabled=False)

        assert policy_module.validate_password("a").is_valid is True


# ── CONTROL 3: the personal-information thresholds ───────────────────────────────


class TestPersonalInformationThresholds:
    """Consequence prevented, in both directions: losing the check (a password that is
    the user's own email name), and losing the FLOOR (a two-letter name or a
    three-letter mailbox making every password containing those letters illegal)."""

    def test_a_four_character_email_local_part_is_refused(self, complexity_enforced):
        result = policy_module.validate_password("Q-Abcd-9zmklp", email="abcd@example.com")

        assert result.errors == ["Password cannot contain your email username"]

    def test_a_three_character_email_local_part_is_below_the_floor(self, complexity_enforced):
        """``len(email_username) >= 4``: ``abc@`` must not outlaw every password with "abc"."""
        result = policy_module.validate_password("Q-Abc-9zmklp", email="abc@example.com")

        assert result.is_valid is True
        assert result.errors == []

    def test_only_the_local_part_matters_not_the_domain(self, complexity_enforced):
        """Otherwise every password containing "example" would be refused deployment-wide."""
        result = policy_module.validate_password("Q-example-91", email="person@example.com")

        assert result.is_valid is True

    def test_a_three_character_name_part_is_refused(self, complexity_enforced):
        result = policy_module.validate_password("Q-Bob-9zmklp", full_name="Bob Smith")

        assert result.errors == ["Password cannot contain parts of your name"]

    def test_a_two_character_name_part_is_below_the_floor(self, complexity_enforced):
        """``len(part) >= 3``: initials and two-letter given names outlaw nothing."""
        result = policy_module.validate_password("Q-Jo-99zmklp", full_name="Jo Nakamura")

        assert result.is_valid is True
        assert result.errors == []

    def test_the_name_check_is_case_insensitive(self, complexity_enforced):
        result = policy_module.validate_password("Q-BOB-9zmklp", full_name="bob smith")

        assert result.errors == ["Password cannot contain parts of your name"]

    def test_only_one_name_error_is_reported(self, complexity_enforced):
        """Two matching parts is still one problem; the loop breaks on the first."""
        result = policy_module.validate_password("Q-BobSmith-91", full_name="Bob Smith")

        assert result.errors == ["Password cannot contain parts of your name"]

    def test_absent_identity_details_are_not_treated_as_a_match(self, complexity_enforced):
        result = policy_module.validate_password(AT_MIN_LENGTH, email=None, full_name=None)

        assert result.is_valid is True


# ── CONTROL 4: weak-pattern warnings are produced, and are non-blocking ──────────


class TestWeakPatternWarnings:
    """Consequence prevented: the pattern list producing nothing (so the advice never
    reaches the user), or producing a warning unconditionally (so it means nothing).
    Nothing in the suite asserted ``result.warnings`` at all before this."""

    WARNING = "Password contains common patterns that may be easily guessed"

    def test_a_password_starting_with_password_warns(self, complexity_enforced):
        result = policy_module.validate_password("Password-1234")

        assert result.warnings == [self.WARNING]

    def test_four_repeated_characters_warn(self, complexity_enforced):
        result = policy_module.validate_password("Xy-Zzzz-91mk")

        assert result.warnings == [self.WARNING]

    def test_sequential_digits_warn(self, complexity_enforced):
        result = policy_module.validate_password("Qk-123456-x!")

        assert result.warnings == [self.WARNING]

    def test_a_strong_password_produces_no_warning(self, complexity_enforced):
        """The control: same code path, opposite outcome."""
        result = policy_module.validate_password(AT_MIN_LENGTH)

        assert result.warnings == []

    def test_a_warning_does_not_block_the_password(self, complexity_enforced):
        """Warnings are advice. Making them blocking would refuse compliant passwords."""
        result = policy_module.validate_password("Password-1234")

        assert result.is_valid is True
        assert result.errors == []

    def test_at_most_one_warning_is_reported(self, complexity_enforced):
        """Several patterns match "password1234"; the user gets one message, not four."""
        result = policy_module.validate_password("Password-123456")

        assert len(result.warnings) == 1


# ── CONTROL 5: max_age_days == 0 means "never expires" ───────────────────────────


class TestMaxAgeZeroNeverExpires:
    """Consequence prevented: narrowing ``max_age_days <= 0`` to ``< 0`` makes the
    cutoff ``now``, so every ``password_changed_at`` in the deployment reads as expired
    and every user is forced through a reset on their next login."""

    def test_zero_disables_the_cutoff_entirely(self):
        _publish(password_policy_enabled=True, password_max_age_days=0)

        assert policy_module.password_expiry_cutoff() is None

    def test_zero_expires_nothing_however_old(self):
        _publish(password_policy_enabled=True, password_max_age_days=0)
        ancient = datetime.now(UTC) - timedelta(days=5000)

        assert policy_module.is_password_expired(ancient) is False

    def test_zero_reports_no_days_until_expiration(self):
        _publish(password_policy_enabled=True, password_max_age_days=0)
        ancient = datetime.now(UTC) - timedelta(days=5000)

        assert policy_module.get_days_until_expiration(ancient) is None

    def test_zero_does_not_expire_an_unknown_change_date(self):
        """``None`` is "treat as expired" only while expiry is actually enforced."""
        _publish(password_policy_enabled=True, password_max_age_days=0)

        assert policy_module.is_password_expired(None) is False

    def test_a_positive_max_age_still_expires(self):
        """The control: with expiry configured, the same inputs expire."""
        _publish(password_policy_enabled=True, password_max_age_days=60)
        old = datetime.now(UTC) - timedelta(days=61)

        assert policy_module.password_expiry_cutoff() is not None
        assert policy_module.is_password_expired(old) is True

    def test_a_positive_max_age_leaves_a_fresh_password_alone(self):
        _publish(password_policy_enabled=True, password_max_age_days=60)
        fresh = datetime.now(UTC) - timedelta(days=1)

        assert policy_module.is_password_expired(fresh) is False
