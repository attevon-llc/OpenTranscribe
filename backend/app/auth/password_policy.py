"""
Password policy enforcement module (FedRAMP IA-5 compliant).

Implements NIST SP 800-63B password requirements:
- Minimum length enforcement (default: 12 characters)
- Character complexity requirements (uppercase, lowercase, digits, special)
- Password history tracking (prevent reuse of last N passwords)
- Password expiration (max age before forced reset)
- Minimum password age (how soon a password may be changed *again*)

Every setting is admin-editable at runtime (Settings -> Authentication ->
Password Policy) and resolves DB ``auth_config`` > ``.env`` > coded default, the
same rule as the rest of the auth plane. The policy can be turned off entirely
for non-FedRAMP environments with ``password_policy_enabled``.
"""

import logging
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from app.auth import password_blocklist
from app.core.auth_settings import get_process_auth_settings
from app.core.config import settings

logger = logging.getLogger(__name__)


# Special characters allowed in passwords (OWASP recommended set)
SPECIAL_CHARACTERS = r"""!@#$%^&*()_+\-=\[\]{};':"\\|,.<>\/?`~"""
SPECIAL_CHARS_DISPLAY = "!@#$%^&*()_+-=[]{}|;':\",./<>?`~"

PROFILE_NIST = "nist"
PROFILE_STIG = "stig"
PROFILE_CUSTOM = "custom"
VALID_PROFILES = (PROFILE_NIST, PROFILE_STIG, PROFILE_CUSTOM)

#: NIST SP 800-63B-4 section 3.1.1.2: 15 when the password is the only factor, 8 when it
#: is used together with MFA; verifiers SHALL accept at least 64 characters.
NIST_MIN_LENGTH = 15
NIST_MIN_LENGTH_WITH_MFA = 8
NIST_MIN_ACCEPTED_MAX_LENGTH = 64
NIST_DEFAULT_MAX_LENGTH = 128

#: Always a context word: a password built from the product's own name is guessable.
PRODUCT_CONTEXT_WORDS = ("opentranscribe",)

#: Coded default for ``password_min_age_hours`` — FedRAMP IA-5(1)(d)'s "minimum
#: lifetime restriction", whose baseline value is one day.
#:
#: Nonzero by default on purpose. With no minimum age, the bounded history is
#: self-defeating: ``_cleanup_old_history`` keeps only the newest
#: ``password_history_count`` rows, so any user can issue that many back-to-back
#: changes with throwaway passwords, flush the row holding their original, and set
#: the original again. At 24 h that attack costs ``password_history_count`` days
#: (24 by default) instead of one uninterrupted minute.
DEFAULT_PASSWORD_MIN_AGE_HOURS = 24


@dataclass
class PasswordValidationResult:
    """Result of password validation.

    Attributes:
        is_valid: Whether the password meets all requirements
        errors: List of specific validation errors
        warnings: List of warnings (non-blocking issues)
    """

    is_valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_error(self, error: str) -> None:
        """Add a validation error."""
        self.errors.append(error)
        self.is_valid = False

    def add_warning(self, warning: str) -> None:
        """Add a validation warning."""
        self.warnings.append(warning)


class PasswordPolicy:
    """
    Password policy enforcement following FedRAMP IA-5 controls.

    This class validates passwords against configurable requirements and
    manages password history to prevent reuse.

    Every requirement below is a **property**, read through
    ``get_process_auth_settings()`` at the moment it is checked, so it resolves
    DB ``auth_config`` > ``.env`` > coded default. They used to be plain
    attributes assigned from ``settings.*`` in ``__init__``, and the module-level
    ``password_policy`` singleton is built at import — so all eight admin
    controls were frozen at the value the process started with and saving any of
    them changed nothing.

    Properties rather than a ``reload()`` because the enforcement points are
    reached without a session (``schemas/user.py`` validates inside a Pydantic
    model) and because a second cached copy here would be a second thing to
    invalidate; ``_ProcessAuthSettings`` already owns exactly one cache.

    Configuration keys (category ``password_policy``):
        password_policy_enabled, password_min_length, password_require_uppercase,
        password_require_lowercase, password_require_digit,
        password_require_special, password_history_count, password_max_age_days,
        password_min_age_hours.

    ``password_min_age_hours`` resolves through the same layered accessor but is
    **not yet registered** in ``AuthConfigService.CONFIG_TYPES`` / the
    ``PasswordPolicyConfig`` schema / the admin panel, so today it is settable only
    by writing the ``auth_config`` row directly. Registering it there is the
    remaining wiring, not a second implementation.
    """

    # Common password patterns to avoid (compiled for performance)
    _COMMON_PATTERNS = [
        r"^password",  # starts with "password"
        r"^qwerty",  # starts with "qwerty"
        r"^123456",  # starts with "123456"
        r"(.)\1{3,}",  # 4+ repeated characters
        r"(012|123|234|345|456|567|678|789|890){2,}",  # sequential numbers
        r"(abc|bcd|cde|def|efg|fgh|ghi|hij|ijk|jkl|klm|lmn|mno|nop|opq|pqr|qrs|rst|stu|tuv|uvw|vwx|wxy|xyz){2,}",  # sequential letters
    ]

    @property
    def enabled(self) -> bool:
        """Whether the policy is enforced at all."""
        return get_process_auth_settings().password_policy_enabled

    @property
    def profile(self) -> str:
        """The active rule set: ``nist``, ``stig`` or ``custom``.

        An unrecognised value (a typo) resolves to ``stig`` - the strictest rule set -
        and is logged, rather than raising: a bad setting must not turn every
        registration into a 500, and it must not silently weaken the policy either.
        """
        raw = get_process_auth_settings().password_policy_profile
        value = str(raw).strip().lower()
        if value in VALID_PROFILES:
            return value
        logger.warning(
            "Unknown PASSWORD_POLICY_PROFILE %r (expected one of %s); using %r",
            raw,
            ", ".join(VALID_PROFILES),
            PROFILE_STIG,
        )
        return PROFILE_STIG

    @property
    def is_nist(self) -> bool:
        """True under the NIST SP 800-63B-4 profile."""
        return self.profile == PROFILE_NIST

    @property
    def min_length(self) -> int:
        """Minimum accepted password length (single-factor case under ``nist``)."""
        if self.is_nist:
            return NIST_MIN_LENGTH
        return get_process_auth_settings().password_min_length

    def min_length_for(self, mfa_protected: bool = False) -> int:
        """Minimum length for a user who is (or is not) protected by MFA.

        Only the ``nist`` profile distinguishes the two cases (15 vs 8); ``stig`` and
        ``custom`` use the configured ``PASSWORD_MIN_LENGTH`` for everybody.
        """
        if self.is_nist and mfa_protected:
            return NIST_MIN_LENGTH_WITH_MFA
        return self.min_length

    @property
    def max_length(self) -> int:
        """Longest accepted password; 0 = no cap. Under ``nist`` never below 64."""
        configured = get_process_auth_settings().get_int("password_max_length", 0)
        if self.is_nist:
            return (
                max(configured, NIST_MIN_ACCEPTED_MAX_LENGTH)
                if configured > 0
                else (NIST_DEFAULT_MAX_LENGTH)
            )
        return max(configured, 0)

    @property
    def require_uppercase(self) -> bool:
        """Whether an upper-case letter is required (never under ``nist``)."""
        return not self.is_nist and get_process_auth_settings().password_require_uppercase

    @property
    def require_lowercase(self) -> bool:
        """Whether a lower-case letter is required (never under ``nist``)."""
        return not self.is_nist and get_process_auth_settings().password_require_lowercase

    @property
    def require_digit(self) -> bool:
        """Whether a digit is required (never under ``nist``)."""
        return not self.is_nist and get_process_auth_settings().password_require_digit

    @property
    def require_special(self) -> bool:
        """Whether a special character is required (never under ``nist``)."""
        return not self.is_nist and get_process_auth_settings().password_require_special

    @property
    def history_count(self) -> int:
        """How many previous passwords may not be reused. 0 disables the check."""
        if self.is_nist:
            return 0
        return get_process_auth_settings().password_history_count

    @property
    def max_age_days(self) -> int:
        """Days before a password expires. 0 disables expiry (always, under ``nist``)."""
        if self.is_nist:
            return 0
        return get_process_auth_settings().password_max_age_days

    @property
    def blocklist_enabled(self) -> bool:
        """Whether new passwords are screened against the breached/common list.

        Profile default: on for ``nist`` (required by SP 800-63B-4), off for ``stig``
        and ``custom`` (unchanged behaviour). ``PASSWORD_BLOCKLIST_ENABLED`` overrides.
        """
        override = str(get_process_auth_settings().get("password_blocklist_enabled", "")).strip()
        if override:
            return override.lower() in ("true", "1", "yes", "on")
        return self.is_nist

    @property
    def context_words(self) -> list[str]:
        """Static context-specific words (product name + operator additions)."""
        configured = str(get_process_auth_settings().get("password_context_words", "") or "")
        extra = [w.strip() for w in configured.split(",") if w.strip()]
        return [*PRODUCT_CONTEXT_WORDS, *extra]

    @property
    def min_age_hours(self) -> int:
        """Hours a password must be kept before it may be changed again. 0 disables.

        Always 0 under the ``nist`` profile (no history to protect, and a lockout on
        voluntary changes is the opposite of the "change on evidence of compromise" rule).

        The other bookend of :attr:`max_age_days`. Read through the same layered
        accessor, but with its coded default here rather than as a
        ``DynamicAuthSettings`` property, because ``get_int`` resolves an unknown
        key the same way — DB ``auth_config`` > ``.env`` > this default.
        """
        if self.is_nist:
            return 0
        return get_process_auth_settings().get_int(
            "password_min_age_hours", DEFAULT_PASSWORD_MIN_AGE_HOURS
        )

    def _check_character_requirements(
        self, password: str, mfa_protected: bool = False
    ) -> list[str]:
        """
        Check password against character complexity requirements.

        Validates length, uppercase, lowercase, digit, and special character
        requirements based on the configured policy settings.

        Args:
            password: The plaintext password to validate

        Returns:
            List of error messages for failed requirements (empty if all pass)
        """
        errors: list[str] = []

        # Length check
        min_length = self.min_length_for(mfa_protected)
        if len(password) < min_length:
            errors.append(
                f"Password must be at least {min_length} characters long "
                f"(currently {len(password)})"
            )

        max_length = self.max_length
        if max_length and len(password) > max_length:
            errors.append(f"Password must be at most {max_length} characters long")

        # Uppercase check
        if self.require_uppercase and not re.search(r"[A-Z]", password):
            errors.append("Password must contain at least one uppercase letter")

        # Lowercase check
        if self.require_lowercase and not re.search(r"[a-z]", password):
            errors.append("Password must contain at least one lowercase letter")

        # Digit check
        if self.require_digit and not re.search(r"\d", password):
            errors.append("Password must contain at least one digit")

        # Special character check
        if self.require_special and not re.search(
            f"[{re.escape(SPECIAL_CHARS_DISPLAY)}]", password
        ):
            errors.append(
                f"Password must contain at least one special character ({SPECIAL_CHARS_DISPLAY})"
            )

        return errors

    def _check_personal_info(
        self,
        password: str,
        email: str | None,
        full_name: str | None,
    ) -> list[str]:
        """
        Check that password doesn't contain personal information.

        Validates that the password doesn't contain the user's email username
        or parts of their name to prevent easily guessable passwords.

        Args:
            password: The plaintext password to validate
            email: Optional email to check password doesn't contain
            full_name: Optional full name to check password doesn't contain

        Returns:
            List of error messages for personal info found (empty if none)
        """
        errors: list[str] = []
        password_lower = password.lower()

        # Check if email username is in password (case-insensitive)
        if email:
            email_username = email.split("@")[0].lower()
            if len(email_username) >= 4 and email_username in password_lower:
                errors.append("Password cannot contain your email username")

        # Check if any name part (3+ chars) is in password
        if full_name:
            name_parts = full_name.lower().split()
            for part in name_parts:
                if len(part) >= 3 and part in password_lower:
                    errors.append("Password cannot contain parts of your name")
                    break

        return errors

    def _check_common_patterns(self, password: str) -> list[str]:
        """
        Check password against common weak patterns.

        Validates that the password doesn't match common patterns that make
        it easily guessable, such as starting with "password", "qwerty",
        sequential numbers/letters, or repeated characters.

        Args:
            password: The plaintext password to validate

        Returns:
            List of warning messages for patterns found (empty if none)
        """
        warnings: list[str] = []
        password_lower = password.lower()

        for pattern in self._COMMON_PATTERNS:
            if re.search(pattern, password_lower):
                warnings.append("Password contains common patterns that may be easily guessed")
                break

        return warnings

    def _check_blocklist(
        self, password: str, email: str | None, full_name: str | None
    ) -> list[str]:
        """Breached/common-password and context-word screening (SP 800-63B-4)."""
        errors: list[str] = []
        if password_blocklist.is_blocklisted(password, str(settings.PASSWORD_BLOCKLIST_PATH)):
            errors.append(
                "Password is too common or appears in a list of known breached passwords; "
                "choose a different one"
            )

        words = list(self.context_words)
        if email:
            words.append(email.split("@")[0])
        if full_name:
            words.extend(full_name.split())
        hit = password_blocklist.find_context_word(password, words)
        if hit is not None:
            errors.append(
                "Password cannot contain the application name or words specific to your account"
            )
        return errors

    def _check_online_breach(self, password: str) -> list[str]:
        """Optional HIBP-style k-anonymity lookup. Fails open (logged) on any error."""
        if not settings.PASSWORD_HIBP_ENABLED:
            return []
        count = password_blocklist.pwned_count(
            password,
            str(settings.PASSWORD_HIBP_URL),
            float(settings.PASSWORD_HIBP_TIMEOUT_SECONDS),
        )
        if count:
            return ["Password has appeared in a known data breach; choose a different one"]
        return []

    def validate_password(
        self,
        password: str,
        email: str | None = None,
        full_name: str | None = None,
        mfa_protected: bool = False,
    ) -> PasswordValidationResult:
        """
        Validate a password against the configured policy.

        Performs comprehensive validation including character requirements,
        personal information checks, and common pattern detection.

        Args:
            password: The plaintext password to validate
            email: Optional email to check password doesn't contain
            full_name: Optional full name to check password doesn't contain
            mfa_protected: The account is protected by MFA (enrolled, or MFA is required
                of it). Only the ``nist`` profile reads this: it lowers the minimum from
                15 to 8 characters.

        Returns:
            PasswordValidationResult with validation status and any errors
        """
        result = PasswordValidationResult(is_valid=True)

        if not self.enabled:
            return result

        if not password:
            result.add_error("Password cannot be empty")
            return result

        if self.is_nist:
            # Length is counted in code points after NFKC, and the same form is hashed
            # (core.security.get_password_hash), so what was validated is what is stored.
            password = unicodedata.normalize("NFKC", password)

        # Check character requirements (length, uppercase, lowercase, digit, special)
        for error in self._check_character_requirements(password, mfa_protected):
            result.add_error(error)

        # Check password doesn't contain user information
        for error in self._check_personal_info(password, email, full_name):
            result.add_error(error)

        if self.blocklist_enabled:
            for error in self._check_blocklist(password, email, full_name):
                result.add_error(error)

        # Last: it costs a network round trip, so only spend it on an otherwise-acceptable
        # password.
        if result.is_valid:
            for error in self._check_online_breach(password):
                result.add_error(error)

        # Check for common weak patterns
        for warning in self._check_common_patterns(password):
            result.add_warning(warning)

        return result

    def check_password_history(
        self,
        new_password_hash: str,
        password_history: list[str],
        verify_func: Callable[[str, str], bool],
        plain_password: str,
    ) -> bool:
        """
        Check if a password has been used recently.

        **Fails OPEN on an entry the verifier cannot read**, and that is deliberate —
        see the comment on the ``unverifiable`` branch below. Do not "fix" it into a
        refusal without reading it.

        Args:
            new_password_hash: The hash of the new password (unused, kept for API compatibility)
            password_history: List of previous password hashes (most recent first)
            verify_func: Function to verify password against hash (e.g., verify_password)
            plain_password: The plaintext password to check

        Returns:
            True if password is OK (not in history), False if recently used
        """
        if not self.enabled or self.history_count <= 0:
            return True

        # Check against the last N passwords
        history_to_check = password_history[: self.history_count]

        checked = 0
        unverifiable = 0

        for old_hash in history_to_check:
            if not old_hash:
                continue
            try:
                if verify_func(plain_password, old_hash):
                    logger.warning("Password reuse detected in history check")
                    return False
                checked += 1
            except Exception:
                # An entry we cannot verify is NOT evidence that the password is
                # unused — we simply do not know. Keep going, but say so out loud:
                # this used to log at debug, i.e. invisibly in production, so a
                # history that had silently stopped being checked looked identical
                # to one that passed (issue #324).
                unverifiable += 1
                logger.exception("Could not verify a password-history entry")

        if unverifiable:
            # Deliberately NOT fail-closed. Rejecting the new password would leave
            # the user on their CURRENT password — a guaranteed reuse — which is
            # worse than possibly permitting an old one. So allow the change and
            # make the degradation alertable instead. `password_history.py` narrows
            # the blast radius by refusing the CURRENT password from the live
            # `hashed_password` column separately, which does not depend on any
            # history row being readable.
            #
            # The cause is NOT FIPS_MODE, whatever this message used to say, and a
            # runbook that starts by checking FIPS_MODE will find nothing wrong.
            # `core/security._create_password_context` registers pbkdf2_sha256,
            # bcrypt_sha256 AND bcrypt in *both* branches — only the default for
            # NEW hashes differs — so flipping FIPS_MODE leaves every existing hash
            # verifiable. What actually reaches this branch is a stored value that
            # is not a readable passlib hash: the `EXTERNAL_AUTH_NO_PASSWORD`
            # sentinel, a truncated or otherwise corrupted row, a hash written by
            # some other application against a shared database, or a scheme whose
            # passlib backend is missing at runtime (e.g. an incompatible `bcrypt`
            # wheel), which raises rather than returning False.
            level = logger.critical if checked == 0 else logger.error
            level(
                "Password-history check was %s: %d of %d entries could not be "
                "verified. The reuse control is %s. Inspect those password_history "
                "rows: something is stored there that this build's password context "
                "cannot parse (corrupt/truncated row, a non-passlib sentinel, or a "
                "hashing backend that failed to load). Changing FIPS_MODE is NOT a "
                "cause — every scheme is registered for verification in both modes.",
                "completely blind" if checked == 0 else "degraded",
                unverifiable,
                unverifiable + checked,
                "NOT being enforced" if checked == 0 else "partially enforced",
            )

        return True

    def min_age_remaining(
        self,
        password_changed_at: datetime | None,
        current_time: datetime | None = None,
    ) -> timedelta | None:
        """How long until this password may be changed again (FedRAMP IA-5(1)(d)).

        Returns ``None`` when the change is permitted — which is the answer for a
        disabled policy, ``min_age_hours <= 0``, a password whose age is unknown,
        and one already old enough. A caller therefore refuses on
        ``if remaining is not None``, never on a truthiness test: the last second
        of the window is a truthy timedelta but the *zero* case means "allowed".

        ``password_changed_at is None`` deliberately permits the change rather
        than refusing it. NULL is "no recorded change" (external identities, rows
        seeded before the column was maintained), and reading absent data as a
        fresh password would lock those accounts out of the one operation that
        would populate it.

        This control is about *voluntary* changes only. Callers must exempt an
        administrator-initiated reset and a ``must_change_password`` hold — a user
        held for a forced change whose password was set moments ago by the admin
        doing the holding would otherwise be refused at both ends: unable to use
        the app, and unable to change the password that is the reason.

        Args:
            password_changed_at: When the password was last changed (UTC).
            current_time: Current time for comparison (default: now UTC).

        Returns:
            The remaining wait, or None when the change is permitted now.
        """
        if not self.enabled or self.min_age_hours <= 0 or password_changed_at is None:
            return None

        if current_time is None:
            current_time = datetime.now(UTC)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=UTC)
        if password_changed_at.tzinfo is None:
            password_changed_at = password_changed_at.replace(tzinfo=UTC)

        remaining = (password_changed_at + timedelta(hours=self.min_age_hours)) - current_time
        return remaining if remaining > timedelta(0) else None

    def expiry_cutoff(self, current_time: datetime | None = None) -> datetime | None:
        """
        The instant before which a ``password_changed_at`` counts as expired.

        Exists so the row-at-a-time check and the SQL aggregate check are the
        same rule: ``admin.py``'s account-status report re-derived this cutoff
        inline with its own ``timedelta(days=settings.PASSWORD_MAX_AGE_DAYS)``,
        which meant disabling the policy did not disable the report's notion of
        expiry.

        Args:
            current_time: Current time for comparison (default: now UTC)

        Returns:
            The cutoff timestamp (UTC), or None when expiry is not enforced.
        """
        if not self.enabled or self.max_age_days <= 0:
            return None

        if current_time is None:
            current_time = datetime.now(UTC)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=UTC)

        return current_time - timedelta(days=self.max_age_days)

    def is_password_expired(
        self,
        password_changed_at: datetime | None,
        current_time: datetime | None = None,
    ) -> bool:
        """
        Check if a password has expired based on max age policy.

        Args:
            password_changed_at: When the password was last changed (UTC)
            current_time: Current time for comparison (default: now UTC)

        Returns:
            True if password is expired, False otherwise
        """
        cutoff = self.expiry_cutoff(current_time)
        if cutoff is None:
            return False

        if password_changed_at is None:
            # No recorded change time - treat as expired for safety
            return True

        # Ensure timezone-aware comparison
        if password_changed_at.tzinfo is None:
            password_changed_at = password_changed_at.replace(tzinfo=UTC)

        return password_changed_at <= cutoff

    def get_days_until_expiration(
        self,
        password_changed_at: datetime | None,
        current_time: datetime | None = None,
    ) -> int | None:
        """
        Get the number of days until password expires.

        Args:
            password_changed_at: When the password was last changed (UTC)
            current_time: Current time for comparison (default: now UTC)

        Returns:
            Days until expiration (negative if expired), None if policy disabled
        """
        if not self.enabled or self.max_age_days <= 0:
            return None

        if password_changed_at is None:
            return -1  # Already expired (no recorded change)

        if current_time is None:
            current_time = datetime.now(UTC)

        # Ensure timezone-aware comparison
        if password_changed_at.tzinfo is None:
            password_changed_at = password_changed_at.replace(tzinfo=UTC)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=UTC)

        expiration_date = password_changed_at + timedelta(days=self.max_age_days)
        delta = expiration_date - current_time
        return delta.days

    def get_policy_requirements(self) -> dict:
        """
        Get the current password policy requirements.

        Returns:
            Dictionary describing current policy settings
        """
        return {
            "enabled": self.enabled,
            "profile": self.profile,
            "min_length": self.min_length,
            "min_length_with_mfa": self.min_length_for(True),
            "max_length": self.max_length,
            "blocklist_enabled": self.blocklist_enabled,
            "blocklist_status": password_blocklist.blocklist_status(
                str(settings.PASSWORD_BLOCKLIST_PATH)
            ),
            "require_uppercase": self.require_uppercase,
            "require_lowercase": self.require_lowercase,
            "require_digit": self.require_digit,
            "require_special": self.require_special,
            "special_characters": SPECIAL_CHARS_DISPLAY,
            "history_count": self.history_count,
            "max_age_days": self.max_age_days,
            "min_age_hours": self.min_age_hours,
        }


# Global password policy instance
password_policy = PasswordPolicy()


def validate_password(
    password: str,
    email: str | None = None,
    full_name: str | None = None,
    mfa_protected: bool = False,
) -> PasswordValidationResult:
    """
    Validate a password against the configured policy.

    Convenience function that uses the global password policy instance.

    Args:
        password: The plaintext password to validate
        email: Optional email to check password doesn't contain
        full_name: Optional full name to check password doesn't contain
        mfa_protected: Account is MFA-protected (lowers the ``nist`` minimum to 8)

    Returns:
        PasswordValidationResult with validation status and any errors
    """
    return password_policy.validate_password(password, email, full_name, mfa_protected)


def check_password_history(
    plain_password: str,
    password_history: list[str],
    verify_func: Callable[[str, str], bool],
) -> bool:
    """
    Check if a password has been used recently.

    Convenience function that uses the global password policy instance.

    Args:
        plain_password: The plaintext password to check
        password_history: List of previous password hashes (most recent first)
        verify_func: Function to verify password against hash

    Returns:
        True if password is OK (not in history), False if recently used
    """
    return password_policy.check_password_history(  # nosec B106
        new_password_hash="",  # Not used - placeholder for API compatibility
        password_history=password_history,
        verify_func=verify_func,
        plain_password=plain_password,
    )


def is_password_expired(
    password_changed_at: datetime | None,
    current_time: datetime | None = None,
) -> bool:
    """
    Check if a password has expired based on max age policy.

    Convenience function that uses the global password policy instance.

    Args:
        password_changed_at: When the password was last changed (UTC)
        current_time: Current time for comparison (default: now UTC)

    Returns:
        True if password is expired, False otherwise
    """
    return password_policy.is_password_expired(password_changed_at, current_time)


def get_days_until_expiration(
    password_changed_at: datetime | None,
    current_time: datetime | None = None,
) -> int | None:
    """
    Get the number of days until a password expires.

    Convenience function that uses the global password policy instance. The
    method had no module-level wrapper while its sibling
    :func:`is_password_expired` did, which is why nothing ever called it.

    Args:
        password_changed_at: When the password was last changed (UTC)
        current_time: Current time for comparison (default: now UTC)

    Returns:
        Days until expiration (negative if expired), None if policy disabled
    """
    return password_policy.get_days_until_expiration(password_changed_at, current_time)


def password_min_age_remaining(
    password_changed_at: datetime | None,
    current_time: datetime | None = None,
) -> timedelta | None:
    """How long until a password may be changed again (FedRAMP IA-5(1)(d)).

    Convenience function that uses the global password policy instance. ``None``
    means the change is permitted now — see
    :meth:`PasswordPolicy.min_age_remaining` for why ``None`` and not ``0``.

    Args:
        password_changed_at: When the password was last changed (UTC)
        current_time: Current time for comparison (default: now UTC)

    Returns:
        The remaining wait, or None when the change is permitted now.
    """
    return password_policy.min_age_remaining(password_changed_at, current_time)


def password_expiry_cutoff(current_time: datetime | None = None) -> datetime | None:
    """
    The instant before which a ``password_changed_at`` counts as expired.

    Convenience function that uses the global password policy instance. Query
    builders filter ``User.password_changed_at < cutoff``; None means expiry is
    not enforced and nothing should be reported as expired.

    Args:
        current_time: Current time for comparison (default: now UTC)

    Returns:
        The cutoff timestamp (UTC), or None when expiry is not enforced.
    """
    return password_policy.expiry_cutoff(current_time)


def get_policy_requirements() -> dict:
    """
    Get the current password policy requirements.

    Convenience function that uses the global password policy instance.

    Returns:
        Dictionary describing current policy settings
    """
    return password_policy.get_policy_requirements()
