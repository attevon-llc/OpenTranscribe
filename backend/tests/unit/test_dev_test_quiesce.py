"""The browser suite must not start while the stack is still draining the backend phase.

Natural experiment, same tree, same day, same suite:

    run-e2e.sh standalone (19:58)          323 passed,  0 failed,             551 s
    the same suite as phase 2 (20:50)      333 passed,  7 failed,             611 s
    the same suite as phase 2 (01:31)      314 passed, 13 failed + 13 errors, 919 s

Direct evidence the stack was still draining when e2e started, from the 01:31 log::

    AssertionError: the chunk index stayed unavailable across 4 attempts
                    (TransportError(503, 'search_phase_execution_exception'))

``scripts/run-dev-tests.sh`` ran a 48-worker ~25-minute backend suite and then immediately
started Playwright, while the Celery reindex work that suite dispatched was still running.
These tests pin the bounded quiesce that now sits between them — that it exists, that it runs
BEFORE the e2e dispatch, that it is bounded and non-fatal, and that each of its three legs
reports honestly rather than defaulting to "settled".
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DEV_TESTS = REPO_ROOT / "scripts" / "run-dev-tests.sh"
E2E_CONFTEST = REPO_ROOT / "backend" / "tests" / "e2e" / "conftest.py"

pytestmark = pytest.mark.skipif(
    not DEV_TESTS.is_file() or shutil.which("bash") is None,
    reason="run-dev-tests.sh or bash is not present in this checkout",
)


def _source() -> str:
    return DEV_TESTS.read_text(encoding="utf-8")


def _quiesce_body() -> str:
    source = _source()
    start = source.index("await_stack_quiesce() {")
    return source[start : source.index("\n}\n", start) + 3]


def test_the_quiesce_runs_before_the_e2e_phase_is_dispatched():
    """After the fact is not a wait — it has to be between the two phases."""
    source = _source()
    call_at = source.index("\n    await_stack_quiesce\n")
    dispatch_at = source.index('scripts/e2e/run-e2e.sh"')
    backend_at = source.index("run-integration-tests.sh" + '"')
    assert backend_at < call_at < dispatch_at, (
        "the quiesce must sit between the backend phase and the e2e phase; it is currently "
        f"at {call_at} with backend={backend_at} and e2e={dispatch_at}"
    )


def test_it_waits_for_all_three_planes_the_backend_phase_leaves_busy():
    """Backend HTTP, OpenSearch, and Celery — dropping any one re-opens the failure."""
    body = _quiesce_body()
    assert "_await_stable_backend" in body, "the backend-steadiness leg is gone"
    assert "_cluster/health" in body, "the OpenSearch leg is gone"
    assert "celery_app.control.inspect" in body, "the Celery-idleness leg is gone"


def test_the_backend_leg_reuses_the_e2e_suites_own_helper():
    """One implementation of "N consecutive 200s", not two that can drift.

    ``_await_stable_backend`` is what ``e2e_stack_preflight`` itself uses; the quiesce loads
    that function rather than carrying a bash lookalike.
    """
    body = _quiesce_body()
    assert "backend/tests/e2e/conftest.py" in body, (
        "the quiesce no longer loads the real helper by path — a second copy of the "
        "consecutive-success rule is free to drift from the one the suite enforces"
    )
    assert E2E_CONFTEST.is_file()
    assert "def _await_stable_backend(" in E2E_CONFTEST.read_text(encoding="utf-8"), (
        "the helper the quiesce imports by name no longer exists under that name"
    )


def test_the_wait_is_bounded_and_the_budget_is_overridable():
    source = _source()
    assert 'QUIESCE_BUDGET_S="${QUIESCE_BUDGET_S:-' in source, (
        "the quiesce budget is hardcoded — an unbounded wait turns a busy stack into a hang"
    )
    body = _quiesce_body()
    assert "deadline" in body and "budget" in body


def test_a_stack_that_never_settles_warns_rather_than_failing_the_gate():
    """Non-fatal by design: an unsettled stack is diagnosable, a red phase is not.

    Driven by running the REAL function with every external probe stubbed to report busy,
    and asserting it still returns 0 while SAYING so.
    """
    harness = textwrap.dedent(f"""
        set -uo pipefail
        YELLOW='' GREEN='' NC=''
        QUIESCE_BUDGET_S=1
        VENV_PY=/bin/false                      # the backend/opensearch leg cannot run
        CPU_WORKER_CONTAINER=""
        overlay_container_name() {{ echo ""; }}  # ...and no celery worker is resolvable
        {_quiesce_body()}
        await_stack_quiesce
        echo "returned=$?"
    """)
    result = subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, timeout=120, check=False
    )
    assert "returned=0" in result.stdout, (
        f"a stack that would not settle failed the run instead of warning: {result.stdout}\n"
        f"{result.stderr}"
    )
    assert "NOT fully settled" in result.stdout, (
        f"the warning that names what was still busy is missing: {result.stdout}"
    )


def test_the_celery_leg_forwards_its_program_to_the_container():
    """⚠️ ``docker exec`` WITHOUT ``-i`` discards stdin.

    Measured while writing this: ``docker exec <c> python - <<'EOF'`` runs an EMPTY program
    and exits 0, so the leg reported success having inspected nothing. Two guards, because
    the first is a one-character regression: the flag, and an empty-output check that turns a
    silent no-op into NOT MEASURED.
    """
    body = _quiesce_body()
    assert "docker exec -i " in body, (
        "the celery inspect program is no longer piped into the container — without -i, "
        "`python -` reads EOF, runs nothing, and exits 0"
    )
    assert "NOT MEASURED" in body, (
        "the empty-output guard is gone, so a probe that produces nothing reads as idle"
    )


def test_the_celery_leg_ignores_scheduled_tasks():
    """``scheduled`` holds ETA/retry work and is NOT zero on an idle stack.

    Measured: 5 tasks sitting on cpu-processor with nothing running. Requiring it to drain
    would burn the whole budget on every run and make the warning meaningless.
    """
    body = _quiesce_body()
    assert '("active", "reserved")' in body, (
        "the idleness definition changed — including `scheduled` never settles"
    )


def test_the_opensearch_leg_probes_at_least_once():
    """An earlier leg overrunning the shared deadline must not produce an unmeasured verdict.

    Observed on the first live run: leg 1 consumed the whole budget against a flapping
    backend and OpenSearch reported ``NOT SETTLED after 0s — no probe completed`` having
    never issued a request.
    """
    body = _quiesce_body()
    assert "while first or time.monotonic() < deadline:" in body, (
        "the OpenSearch leg can again report a verdict without probing"
    )
    assert "budget * 0.6" in body, (
        "the backend leg is no longer capped at a share of the budget, so it can starve the "
        "legs after it"
    )
