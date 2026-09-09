"""The release ``test`` stage must run the gate WITH overlay orchestration.

``scripts/release/60-test.sh`` called ``run-integration-tests.sh`` directly. That script owns
the test phases; it does **not** start the mock provider containers. On a plain dev stack all
six tests in ``tests/integration/test_lite_mode_mocked_providers.py`` therefore skip with
*"mock-asr and/or mock-llm containers not running"*.

Measured 2026-09-08, the first time this stage was ever run: the integration phase reported
**167 passed, 14 skipped** against a ceiling of **7**, so it declined to be counted and the
stage exited 4 — ``⊘ NOT MEASURED  integration-gate``. The release gate could not be counted
at all, on any machine that did not happen to have those overlays already up.

The 14 broke down as:

===========================================  =====  ==============================
skip                                         count  verdict
===========================================  =====  ==============================
``test_lite_mode_mocked_providers``            6    overlays absent — THE DEFECT
``test_fusion_strategy_switch``                4    corpus-dependent, documented
``test_rag_eval_harness``                      3    corpus-dependent, documented
``test_speaker_label_index_drift`` (audit)     1    should have been DESELECTED
===========================================  =====  ==============================

so removing the 6 and the 1 lands on exactly 7 — at the ceiling, which passes.

⚠️ **Starting the overlays inside this stage would be worse than delegating.**
``opentr.sh start dev --with-X`` recreates the app services, and an overlay's compose file is
only in that chain if its flag is passed — so omitting any OTHER active overlay's flag
recreates those services without its env, silently un-configuring a container that keeps
running and keeps reporting healthy (the measured ``GLADIA_API_BASE_URL`` case in
``scripts/lib/dev-test-overlays.sh``). That library owns the full set. A second copy of that
knowledge in a release stage is exactly the drift ``60-test.sh``'s own header warns about.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
STAGE = REPO_ROOT / "scripts" / "release" / "60-test.sh"
DEV_TESTS = REPO_ROOT / "scripts" / "run-dev-tests.sh"

pytestmark = pytest.mark.skipif(
    not STAGE.is_file() or not DEV_TESTS.is_file(),
    reason="the release/gate scripts are not present in this checkout",
)


def _stage() -> str:
    return STAGE.read_text(encoding="utf-8")


def test_the_stage_goes_through_the_overlay_orchestrator():
    text = _stage()
    invocations = re.findall(r"^\s*\./scripts/run-[a-z-]+\.sh[^\n]*", text, re.M)
    assert invocations, "the stage no longer invokes a gate script at all"
    assert any("run-dev-tests.sh" in line for line in invocations), (
        "the release test stage does not delegate to run-dev-tests.sh, so it runs the gate "
        "without mock-asr/mock-llm and the integration phase skips itself past its ceiling "
        f"(NOT MEASURED). Invocations found: {invocations}"
    )
    assert not any("run-integration-tests.sh" in line for line in invocations), (
        "the stage calls run-integration-tests.sh directly again — that is the bypass that "
        f"lost the overlays: {invocations}"
    )


def test_it_still_asks_for_the_export_capability_check():
    """A release must not ship on the strength of a test that never ran."""
    text = _stage()
    assert "--export-capability" in text, (
        "the release gate no longer requests the diar-native export check; that phase is "
        "opt-in precisely because it is too heavy for the dev loop, so nothing else runs it"
    )


def test_the_orchestrator_can_actually_pass_that_flag_through():
    """Guard the other half: asking for a flag the wrapper drops proves nothing."""
    text = DEV_TESTS.read_text(encoding="utf-8")
    assert "--export-capability) EXPORT_CAPABILITY=true" in text, (
        "run-dev-tests.sh does not parse --export-capability, so the release stage's request "
        "is silently swallowed and the export check never runs"
    )
    assert re.search(r"\$EXPORT_CAPABILITY && backend_flags=\(--export-capability", text), (
        "run-dev-tests.sh parses --export-capability but never forwards it to the gate — a "
        "flag that is accepted and dropped is worse than one that is rejected"
    )


def test_the_not_measured_code_is_translated_not_swallowed():
    """The two scripts use DIFFERENT codes for 'verified nothing', on purpose.

    ``run-dev-tests.sh`` exits **5**; ``run-integration-tests.sh`` exits 4. In the repo-wide
    release contract 4 already means *operator abort*, which is why they differ. Delegating
    without translating would record an honest "a phase verified nothing" as a plain
    failure — the opposite of the distinction this stage exists to preserve.
    """
    text = _stage()
    assert re.search(r"rc == 5", text), (
        "the stage does not handle run-dev-tests.sh's NOT MEASURED exit (5), so a phase "
        "that declined to be counted would be recorded as a failure"
    )
    assert "rc == 4" in text, (
        "the stage no longer records not-measured at all; NOT MEASURED must stay distinct "
        "from both pass and fail (it is blocking, but it is not a failure)"
    )
