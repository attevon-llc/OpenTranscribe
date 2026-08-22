"""Shared parsing for opt-in ``RUN_*`` test gates.

Available to every suite that puts ``backend/`` on ``sys.path`` — the main suite's root
``conftest.py`` does this already; ``tests/e2e/conftest.py`` does the same so the dotted form
works there too (see the comment in that file for why it didn't used to).

Every live ``RUN_*`` skipif in this repo used to spell its own
``os.environ.get(VAR, "").lower() != "true"`` check. That has two bugs, found the hard way:

1. Only the literal string ``"true"`` opens the gate. ``RUN_SCHEMA_DRIFT_TESTS=1`` produces
   3 SKIPPED — indistinguishable from a pass in a summary line.
2. An unrecognised value (a typo, e.g. ``RUN_FIPS_TESTS=ture``) silently skips forever instead
   of telling the operator their gate never opened.

This module fixes both, once, so a twelfth copy of the same two bugs can't reappear.
"""

from __future__ import annotations

import os

#: Case-insensitive truthy/falsy tokens, matching the repo-wide convention used for
#: `.env`-style booleans elsewhere (see ``RUN_MIGRATIONS_ON_STARTUP`` in ``app/core/config.py``,
#: which only ever compares against the single string ``"true"`` because it is written by
#: humans, not by a test invocation someone typed by hand).
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class InvalidGateValueError(ValueError):
    """A ``RUN_*`` gate env var holds a value that is neither truthy nor falsy.

    Raised instead of skipping so a typo (``RUN_FIPS_TESTS=ture``) fails loudly at collection
    time rather than silently skipping the suite on every run, forever.
    """


def parse_bool_env(value: str | None, *, var_name: str) -> bool | None:
    """Parse a single env var value as a case-insensitive boolean.

    Args:
        value: The raw string from ``os.environ`` (or ``None``/empty if unset).
        var_name: Name of the variable, used only to make a raised error readable.

    Returns:
        ``True``/``False`` for a recognised token; ``None`` if ``value`` is ``None`` or the
        empty string (i.e. the variable was not set — the caller decides the default).

    Raises:
        InvalidGateValueError: ``value`` is non-empty and not one of the recognised tokens.
    """
    if value is None or value == "":
        return None
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise InvalidGateValueError(
        f"{var_name}={value!r} is not a recognised boolean. Use one of "
        f"{sorted(_TRUE_VALUES)} (or {sorted(_FALSE_VALUES)} to explicitly disable) — "
        "not silently skipping, because a typo here has previously been mistaken for a pass."
    )


def gate_enabled(var_name: str, *, default: bool = False) -> bool:
    """Return whether an opt-in ``RUN_*`` test gate is enabled.

    Args:
        var_name: The environment variable name, e.g. ``"RUN_SCHEMA_DRIFT_TESTS"``.
        default: Value to use when the variable is unset or empty. Every current gate in this
            repo is opt-in (default ``False``); a suite that should run by default should not
            be behind a ``RUN_*`` gate at all (see ``test_pki_auth.py``'s history).

    Returns:
        Whether the gate is open.

    Raises:
        InvalidGateValueError: the variable is set to something that is neither a recognised
            truthy nor falsy token. Evaluated at pytest collection time (this is normally
            called directly inside a module-level ``pytest.mark.skipif(...)``), so a bad value
            surfaces as a collection error for that module — loud, not a silent skip.
    """
    parsed = parse_bool_env(os.environ.get(var_name), var_name=var_name)
    return default if parsed is None else parsed
