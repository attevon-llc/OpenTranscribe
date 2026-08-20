"""``tests.env_gate`` — the shared ``RUN_*`` opt-in gate parser.

Every live ``RUN_*`` skipif in this repo used to spell its own
``os.environ.get(VAR, "").lower() != "true"`` check, which had two bugs: only the literal
string ``"true"`` opened the gate (``RUN_SCHEMA_DRIFT_TESTS=1`` produced 3 SKIPPED — a green
summary line for a gate that never opened), and a typo (``RUN_FIPS_TESTS=ture``) skipped
silently forever instead of erroring.

Each test below pairs a must-fire case with a must-stay-clean control, so this file cannot
pass vacuously (the same discipline ``scripts/audit-tests.py`` enforces on the rest of the
suite).
"""

from __future__ import annotations

import pytest

from tests.env_gate import InvalidGateValueError
from tests.env_gate import gate_enabled
from tests.env_gate import parse_bool_env

# ---------------------------------------------------------------------------
# parse_bool_env
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "True", "yes", "YES", "on", "On"])
def test_parse_bool_env_accepts_truthy_variants_case_insensitively(value: str) -> None:
    assert parse_bool_env(value, var_name="RUN_X") is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "NO", "off", "Off"])
def test_parse_bool_env_accepts_falsy_variants_case_insensitively(value: str) -> None:
    assert parse_bool_env(value, var_name="RUN_X") is False


def test_parse_bool_env_returns_none_for_unset() -> None:
    """``None`` means "not set" — the caller decides the default, this is not a bool."""
    assert parse_bool_env(None, var_name="RUN_X") is None


def test_parse_bool_env_returns_none_for_empty_string() -> None:
    assert parse_bool_env("", var_name="RUN_X") is None


def test_parse_bool_env_raises_on_unrecognised_value() -> None:
    """A typo must fail loudly, not be silently treated as falsy.

    This is the exact regression this module exists to close: ``RUN_FIPS_TESTS=ture`` used to
    compare unequal to ``"true"`` and skip the suite forever with no indication the operator's
    env var was never read as intended.
    """
    with pytest.raises(InvalidGateValueError, match="RUN_FIPS_TESTS"):
        parse_bool_env("ture", var_name="RUN_FIPS_TESTS")


def test_parse_bool_env_error_names_the_bad_value() -> None:
    """The error must be actionable: it should show what was actually set."""
    with pytest.raises(InvalidGateValueError, match="banana"):
        parse_bool_env("banana", var_name="RUN_X")


# ---------------------------------------------------------------------------
# gate_enabled — the function every skipif actually calls
# ---------------------------------------------------------------------------


def test_gate_enabled_opens_on_truthy_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    """A truthy variant other than the literal string 'true' must open the gate.

    Before this fix, only ``"true"`` (lowercased) opened any of these gates — ``"1"`` produced
    a silent skip that read as a pass in a summary line.
    """
    monkeypatch.setenv("RUN_SCHEMA_DRIFT_TESTS", "1")
    assert gate_enabled("RUN_SCHEMA_DRIFT_TESTS") is True


def test_gate_enabled_control_stays_closed_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Must-stay-clean control for the case above: unset should NOT open the gate.

    Without this control, a ``gate_enabled`` that always returned ``True`` would also pass the
    truthy-variant test above.
    """
    monkeypatch.delenv("RUN_SCHEMA_DRIFT_TESTS", raising=False)
    assert gate_enabled("RUN_SCHEMA_DRIFT_TESTS") is False


def test_gate_enabled_closes_on_falsy_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUN_PKI_E2E", "no")
    assert gate_enabled("RUN_PKI_E2E") is False


def test_gate_enabled_raises_on_garbage_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """The headline behaviour: a garbage value must error, never skip.

    This is what a module-level ``pytestmark = pytest.mark.skipif(not gate_enabled(...), ...)``
    relies on — the exception propagates while pytest is collecting the module, which pytest
    reports as a collection ERROR rather than a quiet SKIPPED.
    """
    monkeypatch.setenv("RUN_FIPS_TESTS", "ture")
    with pytest.raises(InvalidGateValueError):
        gate_enabled("RUN_FIPS_TESTS")


def test_gate_enabled_control_recognised_value_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Must-stay-clean control for the case above: a real value must not raise.

    Without this control, a ``gate_enabled`` that raised unconditionally would also pass the
    garbage-value test above.
    """
    monkeypatch.setenv("RUN_FIPS_TESTS", "true")
    assert gate_enabled("RUN_FIPS_TESTS") is True
