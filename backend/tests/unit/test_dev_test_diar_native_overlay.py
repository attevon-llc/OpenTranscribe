"""run-dev-tests.sh must manage the diar-native sidecar, conditionally, and never stop it.

Three separate claims, each of which was false before this landed and each of which fails
differently:

1. **It must be MANAGED, not exempt.** ``dev-test-overlays.sh`` carried
   ``EXEMPT_WITH_FLAGS[diar-native]="...no test selector this script drives needs it"``.
   That reason became false the moment ``run-integration-tests.sh`` grew a diar-native phase
   which **FAILS** — not skips — when the sidecar is configured but absent. So
   ``run-dev-tests.sh --full`` would fail its own backend phase on a stack it declined to
   configure, and the report would name the diarizer rather than the missing overlay.

2. **The need is CONDITIONAL.** Unlike a mock provider, starting the sidecar on a deployment
   configured for PyAnnote reserves a GPU for a service nothing will consult. So it is wanted
   exactly when ``diar_native_sidecar_expected`` — the same predicate that phase gates on —
   says the deployment should have it. The tests below drive the real bash resolver with that
   predicate stubbed both ways, because a table entry that is never consulted looks identical
   to one that is.

3. **It must survive teardown.** Every other managed overlay is stopped on exit when this run
   started it. Doing that here would leave a *running* stack whose engine is configured native
   with no sidecar to serve it — silently falling back to in-process PyAnnote, which is the
   exact failure the overlay exists to prevent. Worse than not starting it at all, because the
   stack is left degraded after the tests report green.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OVERLAY_LIB = REPO_ROOT / "scripts" / "lib" / "dev-test-overlays.sh"
PREDICATE_LIB = REPO_ROOT / "scripts" / "lib" / "diar-native-expected.sh"
INTEGRATION_SCRIPT = REPO_ROOT / "scripts" / "run-integration-tests.sh"

pytestmark = pytest.mark.skipif(
    not OVERLAY_LIB.exists(), reason="scripts/lib/dev-test-overlays.sh not in this checkout"
)


def _resolve_overlays(predicate_returns: int) -> list[str]:
    """Run the REAL resolve_needed_overlays() with the diar-native predicate stubbed.

    Sourcing the library is the whole point: a test that greps the table for a key proves the
    key is spelled right, not that anything reads it. ``resolve_needed_overlays`` is where a
    conditional-need entry is honoured or silently ignored.
    """
    bash = shutil.which("bash")
    assert bash, "bash not on PATH"
    script = textwrap.dedent(f"""
        set -uo pipefail
        REPO_ROOT={REPO_ROOT!s}
        VENV_PY=/nonexistent/python          # teardown must not need it: nothing was started
        AUTH_CONFIG_CLI=/nonexistent/cli.py
        RED='' GREEN='' YELLOW='' NC=''
        EXIT_PRECONDITION=3
        RUN_BACKEND=true RUN_E2E=false
        ALL_OVERLAYS=false NO_OVERLAYS=false WITH_GPU_SCALE=false

        source "{OVERLAY_LIB!s}"

        # Override AFTER sourcing so this wins (bash keeps the last definition). The real
        # predicate reads .env and the models dir, neither of which a unit test may depend on.
        diar_native_sidecar_expected() {{ return {predicate_returns}; }}

        resolve_needed_overlays
        printf 'RESOLVED:%s\\n' "${{OVERLAYS_NEEDED[*]:-}}"
    """)
    proc = subprocess.run(
        [bash, "-c", script], capture_output=True, text=True, timeout=120, cwd=REPO_ROOT
    )
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESOLVED:")]
    assert line, (
        f"resolver produced no RESOLVED line.\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    return line[0].removeprefix("RESOLVED:").split()


def test_diar_native_is_resolved_when_the_deployment_is_configured_native():
    """Predicate says yes -> the overlay is in the set run-dev-tests.sh will bring up."""
    resolved = _resolve_overlays(predicate_returns=0)
    assert "diar-native" in resolved, (
        f"diar-native missing from the resolved overlay set {resolved} on a deployment "
        f"configured for native diarization. run-integration-tests.sh's diar-native phase "
        f"FAILS (not skips) when the sidecar is expected and absent, so --full would fail a "
        f"phase over an overlay it declined to start."
    )


def test_diar_native_is_dropped_when_the_deployment_is_not_configured_native():
    """Predicate says no -> not started. Proves the entry is CONSULTED, not just present."""
    resolved = _resolve_overlays(predicate_returns=1)
    assert "diar-native" not in resolved, (
        f"diar-native was resolved as needed ({resolved}) on a deployment NOT configured for "
        f"native diarization — OVERLAY_NEED_PREDICATE is being ignored, so the sidecar would "
        f"reserve a GPU for a service nothing consults."
    )


def test_an_overlay_without_a_predicate_is_still_unconditionally_needed():
    """Control: the predicate machinery must not have made every overlay conditional.

    Without this, both tests above would still pass if `resolve_needed_overlays` had been
    broken into dropping everything, or into dropping nothing but diar-native by name.
    """
    for returns in (0, 1):
        resolved = _resolve_overlays(predicate_returns=returns)
        assert "mock-llm" in resolved, (
            f"mock-llm (no OVERLAY_NEED_PREDICATE entry, phase=either, tier=auto) vanished "
            f"from {resolved} — the conditional-need change altered unconditional overlays"
        )


def test_diar_native_is_managed_rather_than_exempt():
    """The stale exemption must not come back; its stated reason is no longer true."""
    text = OVERLAY_LIB.read_text(encoding="utf-8")
    exempt_block = text.split("declare -A EXEMPT_WITH_FLAGS=(", 1)
    assert len(exempt_block) == 2, "EXEMPT_WITH_FLAGS table not found — did the table shape change?"
    exempt_body = exempt_block[1].split("\n)", 1)[0]
    assert "[diar-native]" not in exempt_body, (
        "diar-native is back in EXEMPT_WITH_FLAGS. Its reason ('no test selector this script "
        "drives needs it') is false: run-integration-tests.sh's diar-native phase fails when "
        "the sidecar is configured but absent."
    )
    assert "[diar-native]=diar-native" in text, (
        "diar-native is in neither OVERLAY_SERVICE nor EXEMPT_WITH_FLAGS — "
        "test_run_dev_tests_overlay_coverage.py will fail too, but this says why"
    )


def test_diar_native_is_exempt_from_teardown_with_a_written_reason():
    """Stopping it leaves a running stack silently falling back to PyAnnote."""
    text = OVERLAY_LIB.read_text(encoding="utf-8")
    block = text.split("declare -A OVERLAY_TEARDOWN_EXEMPT=(", 1)
    assert len(block) == 2, (
        "OVERLAY_TEARDOWN_EXEMPT table is gone — diar-native would be stopped on exit, "
        "leaving the stack configured native with no sidecar"
    )
    body = block[1].split("\n)", 1)[0]
    assert "[diar-native]=" in body, (
        "diar-native has no teardown exemption. teardown_overlays() would `docker stop` it "
        "after a run that started it, leaving a running stack whose diarization silently "
        "falls back to in-process PyAnnote."
    )
    reason = body.split("[diar-native]=", 1)[1].split("\n", 1)[0]
    assert len(reason.strip(' "')) > 40, f"teardown exemption needs a real reason, got {reason!r}"


def test_the_predicate_has_exactly_one_definition():
    """Extracting it must have REMOVED the copy, not added a third.

    run-integration-tests.sh's own comment says a second copy of "is native diarization
    configured?" is how this repo's env-var drift starts. If it grows its own definition back
    while dev-test-overlays.sh sources the lib, the two answer differently the moment either
    is edited — and the symptom is an overlay started for a phase that then skips, or skipped
    for a phase that then fails.
    """
    # A DEFINITION is the name at the start of a line followed by `()`. A bare substring
    # search also matches the prose in run-integration-tests.sh explaining where the function
    # went — flagging the very comment that documents the fix as the duplicate it warns about.
    # (It did, on the first run of this test; the same shape as the arena-shrink regex that
    # matched its own overlay comment.)
    definition = re.compile(r"^\s*(function\s+)?diar_native_sidecar_expected\s*\(\)", re.MULTILINE)
    definers = [
        p
        for p in (PREDICATE_LIB, INTEGRATION_SCRIPT, OVERLAY_LIB)
        if p.exists() and definition.search(p.read_text(encoding="utf-8"))
    ]
    assert definers == [PREDICATE_LIB], (
        f"diar_native_sidecar_expected() should be defined only in {PREDICATE_LIB.name}, "
        f"found definitions in {[p.name for p in definers]}"
    )
    assert "diar-native-expected.sh" in INTEGRATION_SCRIPT.read_text(encoding="utf-8"), (
        "run-integration-tests.sh no longer sources the shared predicate — its diar-native "
        "phase and run-dev-tests.sh's overlay decision can now disagree"
    )
