"""Configuration of the idle-in-transaction backstop (issue #440).

The live-server proof that Postgres actually terminates an idle transaction
lives in ``tests/integration/test_idle_in_transaction_backstop.py`` — it must
sit under ``tests/integration/`` because that is where
``run-integration-tests.sh`` collects from. ``tests/unit/test_gate_phase_
coverage.py`` fails the build when an ``@pytest.mark.integration`` module sits
anywhere else, which is exactly how this file got split: the live tests were
written here first and would have been collected by nothing.

What stays here is pure and needs no database:

* ``build_libpq_options`` produces the right string.
* The shared app engine actually carries it — the gap where a correct helper is
  written and never wired is the failure this audit opened with.
* The shipped default is short enough to have caught the 48-minute leak.
"""

from __future__ import annotations

import pytest

from app.core.config import DEFAULT_DB_IDLE_IN_TRANSACTION_TIMEOUT_MS
from app.core.config import settings
from app.db.base import build_libpq_options
from app.db.base import connect_args as app_connect_args


def test_option_string_is_built_when_a_timeout_is_configured() -> None:
    assert build_libpq_options(300_000) == "-c idle_in_transaction_session_timeout=300000"


@pytest.mark.parametrize("disabled", [0, -1])
def test_no_option_string_when_disabled(disabled: int) -> None:
    """0 (and any negative) must produce None, not ``-c ...=0``.

    Postgres reads 0 as "no timeout", so both spellings happen to disable the
    control — but emitting the GUC at all would make ``connect_args`` claim a
    setting the operator turned off, and an operator reading it would conclude
    the backstop was active.
    """
    assert build_libpq_options(disabled) is None


def test_the_app_engine_actually_carries_the_option() -> None:
    """The helper is wired into the engine every request and task uses.

    Without this, `build_libpq_options` could be perfectly correct and applied
    to nothing.
    """
    configured = settings.DB_IDLE_IN_TRANSACTION_TIMEOUT_MS
    if configured <= 0:
        pytest.skip("backstop disabled in this environment (DB_IDLE_IN_TRANSACTION_TIMEOUT_MS=0)")

    assert "options" in app_connect_args, (
        "The shared engine's connect_args has no libpq options, so the "
        "idle-in-transaction backstop is not applied to any app connection."
    )
    assert f"idle_in_transaction_session_timeout={configured}" in app_connect_args["options"]


def test_the_shipped_default_is_shorter_than_the_leak_it_backstops() -> None:
    """A backstop longer than the observed pathology would not have caught it.

    The leak in issue #440 held a transaction idle for 48+ minutes. Pinning an
    upper bound means raising the default silently past that is a test failure
    rather than a config review nobody runs.

    Asserts the module CONSTANT, not ``settings.*``. An earlier version read the
    resolved setting and therefore failed whenever an operator legitimately set
    ``DB_IDLE_IN_TRANSACTION_TIMEOUT_MS=0`` — turning a supported configuration
    into a broken test suite, which is how a control ends up disabled for real.
    """
    assert 0 < DEFAULT_DB_IDLE_IN_TRANSACTION_TIMEOUT_MS <= 15 * 60 * 1000
