"""Every rehearsal scenario must be torn down, including the last one.

``scripts/release/65-rehearse.sh`` runs three scenarios and tore down two. Scenario C (lite)
ran last and its stack was left up, because each scenario script ends with *"Stack left running
for inspection"* — correct for a human running one by hand, wrong for the orchestrator.

The cost is not leaked resources. The scenarios deliberately bind the standard 5173-5180 ports
under the stock ``opentranscribe-*`` names, so the run exercises what a real install produces,
and ``scripts/release-tests/lib/guardrails.sh`` **refuses to start when any such container
exists — running or merely stopped**. So C's surviving stack makes the *next* ``rehearse``
fail its preconditions with **exit 3**: recorded as ``not-measured``, which reads as an unmet
precondition of the release when it is really residue from the previous run. The operator's
next move looks like "investigate the release" instead of "clean up the last rehearsal".

⚠️ **A teardown failure must not rewrite the scenario's verdict.** Whether the lite deployment
works is a fact about the release; whether this host then released its ports is a fact about
the host. Folding the second into ``lite_rc`` would report a passing lite deployment as failed,
which is why the teardown's status is captured in its own variable.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
REHEARSE = REPO_ROOT / "scripts" / "release" / "65-rehearse.sh"

SCENARIO_SCRIPTS = {
    "A": "test-fresh-install.sh",
    "B": "test-upgrade.sh",
    "C": "test-lite-mode.sh",
}

pytestmark = pytest.mark.skipif(
    not REHEARSE.is_file(), reason="scripts/release/65-rehearse.sh is not present"
)


def _text() -> str:
    return REHEARSE.read_text(encoding="utf-8")


def _teardown_targets() -> set[str]:
    """Scenario scripts handed to ``teardown_scenario``."""
    return {Path(m).name for m in re.findall(r"teardown_scenario\s+\"[^\"]*\"\s+(\S+)", _text())}


def test_the_teardown_helper_is_still_the_mechanism():
    """Guard the guard: if the helper were renamed, the check below would find nothing."""
    text = _text()
    assert "teardown_scenario()" in text, "teardown_scenario is gone; re-point this test"
    assert _teardown_targets(), (
        "no teardown_scenario call sites parsed — the regex no longer matches the call shape, "
        "so this module would pass over a rehearsal that tears down nothing at all"
    )


@pytest.mark.parametrize(("label", "script"), sorted(SCENARIO_SCRIPTS.items()))
def test_every_scenario_is_torn_down(label: str, script: str):
    targets = _teardown_targets()
    assert script in targets, (
        f"Scenario {label} ({script}) is never torn down. Its stack binds the standard ports "
        "under the stock opentranscribe-* names, and lib/guardrails.sh refuses to start while "
        "any such container exists — so the NEXT rehearsal fails its preconditions (exit 3) "
        f"and records not-measured. Torn down today: {sorted(targets)}"
    )


def test_the_last_scenarios_teardown_does_not_overwrite_its_verdict():
    """The teardown status must land somewhere other than ``lite_rc``.

    ``|| lite_rc=$?`` on the teardown would turn a clean lite deployment whose host failed to
    release a port into a reported lite-mode FAILURE — a fact about the machine masquerading
    as a fact about the release.
    """
    text = _text()
    match = re.search(r"teardown_scenario\s+\"Scenario C\"[^\n]*", text)
    assert match, "no teardown for Scenario C to inspect"
    line = match.group(0)
    assert "lite_rc=$?" not in line, (
        "Scenario C's teardown writes into lite_rc, so a teardown problem would be recorded "
        f"as the lite-mode scenario failing:\n  {line}"
    )
    assert re.search(r"\|\|\s*\w+=\$\?", line), (
        f"Scenario C's teardown discards its status entirely — a silent failed cleanup is what "
        f"blocks the next run with no explanation:\n  {line}"
    )


def test_a_failed_final_teardown_tells_the_operator_what_to_run():
    """Blocking the next run is only acceptable if the message says how to unblock it."""
    text = _text()
    idx = text.find('teardown_scenario "Scenario C"')
    assert idx != -1, "no Scenario C teardown found"
    window = text[idx : idx + 1200]
    assert "--cleanup" in window, (
        "a failed final teardown does not print the manual cleanup command; the operator is "
        "left with a rehearsal that refuses to start and no stated cause"
    )


def test_the_teardown_is_inside_the_non_waived_path():
    """A ``--patch`` waiver runs no scenarios, so it must not try to tear one down.

    The same mistake was made once already on this stage with the branch-under-test check,
    which refused a waived rehearsal outright.
    """
    text = _text()
    scenario_c_run = text.find("./scripts/release-tests/test-lite-mode.sh --yes")
    teardown_c = text.find('teardown_scenario "Scenario C"')
    assert scenario_c_run != -1 and teardown_c != -1
    assert teardown_c > scenario_c_run, (
        "Scenario C is torn down before it runs; the teardown must follow the scenario"
    )
    # `record lite-mode` marks the end of the guarded block — the teardown belongs before it.
    record_lite = text.find("record lite-mode")
    assert record_lite != -1, "the lite-mode criterion is no longer recorded"
    assert teardown_c < record_lite, (
        "Scenario C's teardown runs after its criterion is recorded, so a stack still coming "
        "down would not be reflected anywhere the operator reads"
    )
