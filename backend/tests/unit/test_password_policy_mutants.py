"""Password-policy behaviour that no test asserted (issue #446, mutation survivors).

Written from the 86 surviving mutants of ``app/auth/password_policy.py``. Every
test below names a mutation it kills, i.e. an edit to the module that the whole
suite used to accept in silence. ``test_password_policy_controls.py`` covers the
seven *controls*; this file covers the **expiry/clock plane** those controls are
built on, plus the module-level wrappers, which nothing was calling with both
arguments.

Triage of the 86 (measured 2026-08-13, coverage 94%):

* **40 real** — a predicate, a constant, an argument or a user-visible string a
  caller can observe. Those are what this file targets.
* **43 noise** — every one inside ``check_password_history``'s
  degradation report: the ``logger.warning``/``logger.exception`` strings, the
  seven fragments of the operator-facing ``%s`` message, and the ``checked`` /
  ``unverifiable`` counters, which exist *only* to fill in that message's ``%d``
  slots. The ``critical`` vs ``error`` LEVEL split — the part an alert keys off —
  is already pinned by ``test_password_policy_controls.py``; the numbers inside
  the sentence are not, and asserting them would be a test that breaks on every
  reword.
* **3 equivalent** — cannot change behaviour, so no test can kill them:

  - ``check_password_history``'s module-level wrapper passing
    ``new_password_hash=""`` -> ``None`` / ``"XXXX"``. The method's signature
    accepts that parameter and its body never reads it (it is kept for API
    compatibility, as its docstring says), so no value of it is observable.
  - ``if not self.enabled or self.history_count <= 0`` -> ``< 0``. The only value
    where the two predicates disagree is exactly ``0``, and there the mutant
    falls through to ``password_history[:0]`` — an empty slice, so the loop body
    never runs, ``unverifiable`` stays 0 and the function returns ``True``: the
    same answer the early return gives, by a longer route.

Two more are real but only killable outside UTC, and are marked in place:
``expiry_cutoff``/``get_days_until_expiration``'s ``datetime.now(UTC)`` ->
``datetime.now(None)``. ``now(None)`` is naive *local* time, which the next line
relabels as UTC; under ``TZ=UTC`` that is byte-identical and the mutant is
equivalent, so the tests that target it assert the real contract (the default
clock is UTC now) rather than a tolerance.

Everything here runs against the real policy object with its settings published
through the process-level auth cache — no database, no HTTP client. Fixed
datetimes far in the past are used deliberately: a mutation that drops the
caller-supplied ``current_time`` falls back to the real clock, which is only
detectable if the supplied one is nowhere near it.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

import pytest

from app.auth import password_policy as policy_module

#: A password-change timestamp far enough from the real clock that any mutation
#: silently substituting ``datetime.now(UTC)`` lands nowhere near the expected answer.
CHANGED_AT = datetime(2020, 1, 1, 12, 0, tzinfo=UTC)

#: Exactly 30 days after :data:`CHANGED_AT`, used as an explicit "as of" clock.
THIRTY_DAYS_LATER = datetime(2020, 1, 31, 12, 0, tzinfo=UTC)


def _publish(**values: Any) -> None:
    """Make the policy see *values* without a database.

    Every requirement is a property resolved through ``get_process_auth_settings()``
    (DB ``auth_config`` > ``.env`` > coded default). The autouse
    ``_clear_process_auth_cache`` fixture in ``tests/conftest.py`` tears it down.
    """
    from app.core.auth_settings import publish_process_auth_setting

    for key, value in values.items():
        publish_process_auth_setting(key, value)


@pytest.fixture
def expiry_after_90_days():
    """A deployment that expires passwords after 90 days (FedRAMP IA-5's baseline)."""
    _publish(password_policy_enabled=True, password_max_age_days=90)


@pytest.fixture
def complexity_enforced():
    """The documented defaults, published so the ambient ``.env`` cannot move them."""
    _publish(
        password_policy_enabled=True,
        password_min_length=12,
        password_require_uppercase=True,
        password_require_lowercase=True,
        password_require_digit=True,
        password_require_special=True,
    )


# ── The module-level wrappers forward BOTH arguments, in order ───────────────────


class TestTheWrappersForwardTheCallersClock:
    """Consequence prevented: a wrapper that quietly answers "as of now".

    ``api/endpoints/admin.py`` and the account-status report ask these questions
    about a *supplied* instant. Every existing test called the wrappers with one
    argument, so dropping the second — or passing ``None`` for either — was
    invisible: the answer stayed plausible because it was computed against the
    real clock instead of the caller's.
    """

    def test_get_days_until_expiration_counts_from_the_supplied_clock(self, expiry_after_90_days):
        """Kills the wrapper mutants that pass ``None`` for, or drop, either argument.

        30 days into a 90-day policy leaves exactly 60. Substituting the real
        clock for either operand puts the answer thousands of days away.
        """
        assert policy_module.get_days_until_expiration(CHANGED_AT, THIRTY_DAYS_LATER) == 60

    def test_is_password_expired_judges_against_the_supplied_clock(self, expiry_after_90_days):
        """Both verdicts, from the same change date — the clock is what separates them.

        The ``False`` half is the load-bearing one: with the caller's clock
        dropped, a 2020 password is judged against today and every answer becomes
        ``True``.
        """
        assert policy_module.is_password_expired(CHANGED_AT, THIRTY_DAYS_LATER) is False
        assert (
            policy_module.is_password_expired(CHANGED_AT, CHANGED_AT + timedelta(days=91)) is True
        )

    def test_password_expiry_cutoff_is_derived_from_the_supplied_clock(self, expiry_after_90_days):
        """The SQL-side half of the same rule; query builders filter on this value."""
        assert policy_module.password_expiry_cutoff(THIRTY_DAYS_LATER) == datetime(
            2019, 11, 2, 12, 0, tzinfo=UTC
        )


# ── expiry_cutoff: the guard, the normalisation, and the default clock ───────────


class TestExpiryCutoff:
    """Consequence prevented: the cutoff being ``None`` (nothing ever expires) or
    naive (the comparison against a tz-aware column raises, turning a policy
    question into a 500)."""

    def test_the_smallest_nonzero_max_age_still_produces_a_cutoff(self):
        """``max_age_days <= 0`` is the off switch — ``<= 1`` would make 1 day mean "off".

        Every other case in the suite configures 60 or 90 days, all of which
        survive a guard widened by one. A one-day maximum age is the only value
        that separates "0 disables" from "anything under two days disables".
        """
        _publish(password_policy_enabled=True, password_max_age_days=1)

        assert policy_module.password_expiry_cutoff(THIRTY_DAYS_LATER) == datetime(
            2020, 1, 30, 12, 0, tzinfo=UTC
        )

    def test_a_naive_clock_is_read_as_utc_and_the_answer_stays_aware(self, expiry_after_90_days):
        """Kills all three normalisation mutants at once.

        Dropping the ``tzinfo is None`` branch, or replacing the normalised value
        with ``None``/a naive one, either returns a naive datetime (which compares
        unequal to the aware answer and raises against the tz-aware
        ``password_changed_at`` column) or raises ``TypeError`` outright.
        """
        naive_now = datetime(2020, 1, 31, 12, 0)  # deliberately no tzinfo

        cutoff = policy_module.password_expiry_cutoff(naive_now)

        assert cutoff == datetime(2019, 11, 2, 12, 0, tzinfo=UTC)
        assert cutoff.tzinfo is not None, "a naive cutoff raises against the aware column"

    def test_the_default_clock_is_utc_now(self, expiry_after_90_days):
        """``datetime.now(UTC)``, not ``datetime.now()``.

        The next line relabels a naive value as UTC, so a local-time clock is not
        a crash — it is a cutoff wrong by the host's UTC offset, which silently
        expires (or spares) every password within that window. Bracketing the call
        with real reads of the clock is exact, not a tolerance.

        Under ``TZ=UTC`` the two are identical and this cannot fail; it is the
        contract that matters, and it is the only assertion on the default path.
        """
        before = datetime.now(UTC)
        cutoff = policy_module.password_expiry_cutoff()
        after = datetime.now(UTC)

        assert cutoff is not None, "a 90-day policy must produce a cutoff"
        assert before - timedelta(days=90) <= cutoff <= after - timedelta(days=90)


# ── is_password_expired: the boundary, the unknown date, the naive column ────────


class TestIsPasswordExpired:
    """Consequence prevented: the expiry verdict flipping at the boundary, on an
    unknown change date, or on a naive timestamp."""

    def test_an_unknown_change_date_is_treated_as_expired(self, expiry_after_90_days):
        """``None`` is "no recorded change", and the safe reading is *expired*.

        Only the disabled-policy half of this was asserted, where the answer is
        ``False`` for a different reason entirely — so ``return True`` could become
        ``return False`` and every account with a NULL ``password_changed_at``
        would quietly stop being asked to rotate.
        """
        assert policy_module.is_password_expired(None) is True

    def test_a_password_exactly_at_the_cutoff_is_expired(self, expiry_after_90_days):
        """``<=``, not ``<``: 90 days old under a 90-day policy has expired.

        One second younger has not. Without both halves the comparison can be
        loosened by an instant that, at day granularity, is a whole extra day of
        validity.
        """
        now = THIRTY_DAYS_LATER
        exactly_90 = now - timedelta(days=90)

        assert policy_module.is_password_expired(exactly_90, current_time=now) is True
        assert (
            policy_module.is_password_expired(exactly_90 + timedelta(seconds=1), current_time=now)
            is False
        )

    def test_a_naive_change_date_is_read_as_utc(self, expiry_after_90_days):
        """Kills the three normalisation mutants: each raises ``TypeError`` here.

        Rows written by anything that did not set a timezone come back naive.
        Skipping the normalisation (or nulling the value) makes the comparison
        against the aware cutoff raise, which is a 500 on the login path rather
        than a policy answer.
        """
        now = THIRTY_DAYS_LATER
        naive_old = datetime(2019, 1, 1, 12, 0)
        naive_fresh = datetime(2020, 1, 30, 12, 0)

        assert policy_module.is_password_expired(naive_old, current_time=now) is True
        assert policy_module.is_password_expired(naive_fresh, current_time=now) is False


# ── get_days_until_expiration: the countdown the UI renders ──────────────────────


class TestGetDaysUntilExpiration:
    """Consequence prevented: the "your password expires in N days" countdown being
    wrong — which is the only warning a user gets before being locked into a reset."""

    def test_an_unknown_change_date_reports_minus_one(self, expiry_after_90_days):
        """The documented "already expired" sentinel, pinned to its exact value.

        A caller renders this number. ``+1`` reads as "one day left" — the exact
        opposite of the state it encodes — and ``-2`` is a different day count on
        the same screen.
        """
        assert policy_module.get_days_until_expiration(None) == -1

    def test_the_smallest_nonzero_max_age_still_reports_a_countdown(self):
        """``max_age_days <= 0`` is the off switch here too, and 1 is not 0."""
        _publish(password_policy_enabled=True, password_max_age_days=1)

        assert policy_module.get_days_until_expiration(CHANGED_AT, CHANGED_AT) == 1

    def test_a_naive_change_date_is_read_as_utc(self, expiry_after_90_days):
        """Kills the three ``password_changed_at`` normalisation mutants (TypeError)."""
        naive_changed_at = datetime(2020, 1, 1, 12, 0)

        assert policy_module.get_days_until_expiration(naive_changed_at, THIRTY_DAYS_LATER) == 60

    def test_a_naive_clock_is_read_as_utc(self, expiry_after_90_days):
        """The other side of the subtraction, with its own normalisation branch."""
        naive_now = datetime(2020, 1, 31, 12, 0)

        assert policy_module.get_days_until_expiration(CHANGED_AT, naive_now) == 60

    def test_a_password_hours_from_expiry_reports_zero_whole_days(self, expiry_after_90_days):
        """Whole days, floored — and the only assertion on the default clock.

        22 hours of validity left is ``0`` days, not ``1``: the countdown must not
        round a password's last day up into another one. This is also where
        ``datetime.now(UTC)`` -> ``datetime.now()`` becomes visible, because the
        host's UTC offset is enough to push a fractional day over the boundary
        (it cannot be, and is not, asserted as an offset — under ``TZ=UTC`` the
        two clocks agree and only the floor is being pinned).
        """
        changed_at = datetime.now(UTC) - timedelta(days=90) + timedelta(hours=22)

        assert policy_module.get_days_until_expiration(changed_at) == 0


# ── The reuse window of one ──────────────────────────────────────────────────────


class TestAHistoryWindowOfOne:
    """Consequence prevented: ``history_count <= 0`` widening to ``<= 1``, which turns
    the smallest configurable window into a second off switch — a deployment that
    asked to remember one password would remember none, and the immediately
    previous password could be set again."""

    def test_one_remembered_password_is_still_refused(self):
        calls: list[str] = []

        def verify(plain: str, hashed: str) -> bool:
            calls.append(hashed)
            return hashed == f"hash::{plain}"

        _publish(password_policy_enabled=True, password_history_count=1)

        allowed = policy_module.check_password_history(
            "Reused-Pass-9", ["hash::Reused-Pass-9"], verify
        )

        assert allowed is False
        assert calls == ["hash::Reused-Pass-9"], "the stored hash was never verified"


# ── The complexity errors the user actually reads ────────────────────────────────


class TestEachMissingCharacterClassIsNamedExactly:
    """Consequence prevented: the wrong requirement being named.

    These strings are not log output — ``validate_password`` returns them in
    ``PasswordValidationResult.errors``, which is what the registration and
    password-change endpoints put in the response body and the SPA renders
    verbatim. Every existing assertion was a substring or an ``in`` check, so the
    message could be mangled (case-flipped, wrapped in marker text) and still
    pass while telling the user something they cannot act on.

    Each password below satisfies every requirement except one, so the expected
    list is exactly one error — which also pins that the *other* four checks did
    not fire.
    """

    @pytest.mark.parametrize(
        ("password", "expected_error"),
        [
            ("kv-mzp7qtx@bn", "Password must contain at least one uppercase letter"),
            ("KV-MZP7QTX@BN", "Password must contain at least one lowercase letter"),
            ("Kv-mzpqtx@bnw", "Password must contain at least one digit"),
        ],
        ids=["no-uppercase", "no-lowercase", "no-digit"],
    )
    def test_the_error_names_the_requirement_that_failed(
        self, complexity_enforced, password: str, expected_error: str
    ):
        result = policy_module.validate_password(password)

        assert result.errors == [expected_error]
        assert result.is_valid is False

    def test_a_password_meeting_every_requirement_reports_no_error(self, complexity_enforced):
        """The control: the same code path, with nothing to complain about."""
        result = policy_module.validate_password("Kv-mzp7qtx@bn")

        assert result.errors == []
        assert result.is_valid is True
