"""A unit test sees none of the variables an operator exports to point a shell at a stack.

``tests/unit/conftest.py`` clears ``STACK_POINTER_VARS`` for every unit test. These checks are
meaningful in exactly the shell where the bug lived: run the unit suite with
``COMPOSE_PROJECT_NAME`` or ``REDIS_PORT`` exported (as the P3.2 gate did) and, without the
fixture, they fail along with the tests that read the environment by accident.
"""

from __future__ import annotations

import os

import pytest

from tests.fixtures.stack_pointer_env import STACK_POINTER_VARS


@pytest.mark.unit
def test_no_stack_pointer_variable_reaches_a_unit_test() -> None:
    leaked = sorted(name for name in STACK_POINTER_VARS if name in os.environ)
    assert not leaked, f"the operator's stack-pointer variables reached a unit test: {leaked}"


@pytest.mark.unit
def test_a_child_process_a_unit_test_starts_inherits_none_of_them(run_in_clean_process) -> None:
    """Children (``run_in_clean_process``, ``opentr.sh`` snippets) copy ``os.environ``."""
    code = (
        "import os, sys; "
        f"print(','.join(n for n in {STACK_POINTER_VARS!r} if n in os.environ) or 'none')"
    )
    assert run_in_clean_process(code) == "none"


@pytest.mark.unit
def test_unit_phase_redis_url_points_at_a_refused_port() -> None:
    """With no REDIS_* exported the suite must not reach whatever listens on :6379."""
    from app.core.config import settings

    if any(os.environ.get(v) for v in ("REDIS_HOST", "REDIS_PORT")):
        pytest.skip("a phase exported REDIS_* on purpose (integration/GPU/e2e)")
    assert settings.REDIS_URL == "redis://127.0.0.1:1/0"
