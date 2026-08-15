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
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from app.core.auth_settings import get_process_auth_settings

logger = logging.getLogger(__name__)


# Special characters allowed in passwords (OWASP recommended set)
SPECIAL_CHARACTERS = r"""!@#$%^&*()_+\-=\[\]{};':"\\|,.<>\/?`~"""
SPECIAL_CHARS_DISPLAY = "!@#$%^&*()_+-=[]{}|;':\",./<>?`~"

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
    def min_length(self) -> int:
        """Minimum accepted password length."""
        return get_process_auth_settings().password_min_length

    @property
    def require_uppercase(self) -> bool:
        """Whether an upper-case letter is required."""
        return get_process_auth_settings().password_require_uppercase

    @property
    def require_lowercase(self) -> bool:
        """Whether a lower-case letter is required."""
        return get_process_auth_settings().password_require_lowercase

    @property
    def require_digit(self) -> bool:
        """Whether a digit is required."""
        return get_process_auth_settings().password_require_digit

    @property
    def require_special(self) -> bool:
        """Whether a special character is required."""
        return get_process_auth_settings().password_require_special

    @property
    def history_count(self) -> int:
        """How many previous passwords may not be reused. 0 disables the check."""
        return get_process_auth_settings().password_history_count

    @property
    def max_age_days(self) -> int:
        """Days before a password expires. 0 disables expiry."""
        return get_process_auth_settings().password_max_age_days

    @property
    def min_age_hours(self) -> int:
        """Hours a password must be kept before it may be changed again. 0 disables.

        The other bookend of :attr:`max_age_days`. Read through the same layered
        accessor, but with its coded default here rather than as a
        ``DynamicAuthSettings`` property, because ``get_int`` resolves an unknown
        key the same way — DB ``auth_config`` > ``.env`` > this default.
        """
        return get_process_auth_settings().get_int(
            "password_min_age_hours", DEFAULT_PASSWORD_MIN_AGE_HOURS
        )

    def _check_character_requirements(self, password: str) -> list[str]:
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
        if len(password) < self.min_length:
            errors.append(
                f"Password must be at least {self.min_length} characters long "
                f"(currently {len(password)})"
            )

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

    def validate_password(
        self,
        password: str,
        email: str | None = None,
        full_name: str | None = None,
    ) -> PasswordValidationResult:
        """
        Validate a password against the configured policy.

        Performs comprehensive validation including character requirements,
        personal information checks, and common pattern detection.

        Args:
            password: The plaintext password to validate
            email: Optional email to check password doesn't contain
            full_name: Optional full name to check password doesn't contain

        Returns:
            PasswordValidationResult with validation status and any errors
        """
        result = PasswordValidationResult(is_valid=True)

        if not self.enabled:
            return result

        if not password:
            result.add_error("Password cannot be empty")
            return result

        # Check character requirements (length, uppercase, lowercase, digit, special)
        for error in self._check_character_requirements(password):
            result.add_error(error)

        # Check password doesn't contain user information
        for error in self._check_personal_info(password, email, full_name):
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
            "min_length": self.min_length,
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
) -> PasswordValidationResult:
    """
    Validate a password against the configured policy.

    Convenience function that uses the global password policy instance.

    Args:
        password: The plaintext password to validate
        email: Optional email to check password doesn't contain
        full_name: Optional full name to check password doesn't contain

    Returns:
        PasswordValidationResult with validation status and any errors
    """
    return password_policy.validate_password(password, email, full_name)


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
