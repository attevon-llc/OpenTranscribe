"""
Password policy enforcement module (FedRAMP IA-5 compliant).

Implements NIST SP 800-63B password requirements:
- Minimum length enforcement (default: 12 characters)
- Character complexity requirements (uppercase, lowercase, digits, special)
- Password history tracking (prevent reuse of last N passwords)
- Password expiration (max age before forced reset)

All settings are configurable via environment variables and can be disabled
for non-FedRAMP environments by setting PASSWORD_POLICY_ENABLED=false.
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from app.core.config import settings

logger = logging.getLogger(__name__)


# Special characters allowed in passwords (OWASP recommended set)
SPECIAL_CHARACTERS = r"""!@#$%^&*()_+\-=\[\]{};':"\\|,.<>\/?`~"""
SPECIAL_CHARS_DISPLAY = "!@#$%^&*()_+-=[]{}|;':\",./<>?`~"


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

    Configuration (via environment variables):
        PASSWORD_POLICY_ENABLED: Enable/disable policy enforcement (default: true)
        PASSWORD_MIN_LENGTH: Minimum password length (default: 12)
        PASSWORD_REQUIRE_UPPERCASE: Require uppercase letters (default: true)
        PASSWORD_REQUIRE_LOWERCASE: Require lowercase letters (default: true)
        PASSWORD_REQUIRE_DIGIT: Require numeric digits (default: true)
        PASSWORD_REQUIRE_SPECIAL: Require special characters (default: true)
        PASSWORD_HISTORY_COUNT: Number of previous passwords to check (default: 24)
        PASSWORD_MAX_AGE_DAYS: Days before password expires (default: 60)
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

    def __init__(self):
        """Initialize password policy with current settings."""
        self.enabled = settings.PASSWORD_POLICY_ENABLED
        self.min_length = settings.PASSWORD_MIN_LENGTH
        self.require_uppercase = settings.PASSWORD_REQUIRE_UPPERCASE
        self.require_lowercase = settings.PASSWORD_REQUIRE_LOWERCASE
        self.require_digit = settings.PASSWORD_REQUIRE_DIGIT
        self.require_special = settings.PASSWORD_REQUIRE_SPECIAL
        self.history_count = settings.PASSWORD_HISTORY_COUNT
        self.max_age_days = settings.PASSWORD_MAX_AGE_DAYS

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
            # make the degradation alertable instead.
            level = logger.critical if checked == 0 else logger.error
            level(
                "Password-history check was %s: %d of %d entries could not be "
                "verified. The reuse control is %s. This usually means the stored "
                "hashes were written under a different scheme (see FIPS_MODE / "
                "FIPS_VERSION) and can no longer be verified.",
                "completely blind" if checked == 0 else "degraded",
                unverifiable,
                unverifiable + checked,
                "NOT being enforced" if checked == 0 else "partially enforced",
            )

        return True

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
        if not self.enabled or self.max_age_days <= 0:
            return False

        if password_changed_at is None:
            # No recorded change time - treat as expired for safety
            return True

        if current_time is None:
            current_time = datetime.now(UTC)

        # Ensure timezone-aware comparison
        if password_changed_at.tzinfo is None:
            password_changed_at = password_changed_at.replace(tzinfo=UTC)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=UTC)

        expiration_date = password_changed_at + timedelta(days=self.max_age_days)
        return current_time >= expiration_date

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


def get_policy_requirements() -> dict:
    """
    Get the current password policy requirements.

    Convenience function that uses the global password policy instance.

    Returns:
        Dictionary describing current policy settings
    """
    return password_policy.get_policy_requirements()
