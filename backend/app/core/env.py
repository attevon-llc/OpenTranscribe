"""The one reader for tuning knobs taken from the process environment.

Policy (D4): a malformed, empty or out-of-range value never aborts startup. It logs a warning
that NAMES the variable and the default in use, and the default is returned. An unset variable
is the normal case and is silent. Refusing to start over an optional knob would turn an upgrade
into an outage for self-hosted installs.

Also home of the single truthy vocabulary, :func:`is_truthy`, which is applied to DB-stored
setting strings as well as environment values.
"""

from __future__ import annotations

import logging
import math
import os

logger = logging.getLogger(__name__)

TRUTHY: tuple[str, ...] = ("true", "1", "yes", "on")
FALSY: tuple[str, ...] = ("false", "0", "no", "off")


def is_truthy(raw: str | None) -> bool:
    """Return True when ``raw`` (case/space-insensitive) is one of true/1/yes/on."""
    return raw is not None and str(raw).strip().lower() in TRUTHY


def parse_bool(raw: str | None, default: bool) -> bool:
    """Parse a DB-stored setting string: None yields ``default``, anything else is :func:`is_truthy`."""
    return default if raw is None else is_truthy(raw)


def _raw(name: str) -> str | None:
    """Stripped value, or None when the variable is unset."""
    value = os.environ.get(name)
    return None if value is None else value.strip()


def env_int(
    name: str, default: int, *, minimum: int | None = None, maximum: int | None = None
) -> int:
    """Integer value; empty, malformed or out-of-range warns and yields ``default``."""
    raw = _raw(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid integer %s=%r; using default %d", name, raw, default)
        return default
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        logger.warning(
            "%s=%d is outside [%s, %s]; using default %d", name, value, minimum, maximum, default
        )
        return default
    return value


def env_float(
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    """Float value; empty, malformed, non-finite or out-of-range warns and yields ``default``."""
    raw = _raw(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid number %s=%r; using default %s", name, raw, default)
        return default
    if not math.isfinite(value) or (
        (minimum is not None and value < minimum) or (maximum is not None and value > maximum)
    ):
        logger.warning(
            "%s=%s is outside [%s, %s]; using default %s", name, raw, minimum, maximum, default
        )
        return default
    return value


def env_bool(name: str, default: bool = False) -> bool:
    """Boolean value (true/1/yes/on or false/0/no/off); empty or unrecognised warns, uses default."""
    raw = _raw(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in TRUTHY:
        return True
    if lowered in FALSY:
        return False
    logger.warning("Invalid boolean %s=%r; using default %s", name, raw, default)
    return default
