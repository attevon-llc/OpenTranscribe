"""The rehearsal must install the branch being released, not whatever ``master`` holds.

``scripts/release/65-rehearse.sh`` is *"the stage that proves the release"*. Two of its three
scenarios install through ``setup-opentranscribe.sh``, which downloads every
``release-manifest.txt`` file — ``docker-compose.yml``, ``docker-compose.lite.yml``,
``opentranscribe.sh`` — from ``OPENTRANSCRIBE_BRANCH``. Both scenario scripts defaulted that
to ``master`` and the orchestrator never overrode it.

So rehearsing an unreleased version from a feature branch exercised **master's** deployment
files. Every compose or installer change in the release went unrehearsed: the stage that
exists to prove the release proved the previous one.

Measured 2026-09-08, and it caught me out directly. A ``/ml-models`` mount was added for
``celery-cpu-worker``, committed and pushed; the lite scenario still came up without it::

    working tree docker-compose.yml : 7 occurrences of 'ml-models'
    origin/master                   : 4
    staged install tree             : 2
    docker exec …-celery-cpu-worker ls /ml-models → No such file or directory

⚠️ **It also invalidated a verification I had already claimed.** A standalone lite run passed
17/17 *after* that fix and I credited the fix. It had not been installed at all — the run
passed because the backend's startup one-shot happened to register the model that time. A
green run you cannot attribute to your change is not evidence, which is exactly what
``CLAUDE.md`` says about attributing passes.

⚠️ **Do not over-correct.** ``setup-opentranscribe.sh`` itself coming from the DEFAULT branch
is deliberate and documented (root ``CLAUDE.md``, install-path gate): a real
``curl … | bash`` user gets the installer from the default branch and the artifacts from the
resolved release ref, and ``verify-install-paths.sh`` exists precisely because those two
sources differ. What is wrong is only that the MANIFEST FILES came from ``master`` while
rehearsing something not yet on master.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
REHEARSE = REPO_ROOT / "scripts" / "release" / "65-rehearse.sh"
SCENARIOS = {
    "fresh-install": REPO_ROOT / "scripts" / "release-tests" / "test-fresh-install.sh",
    "lite-mode": REPO_ROOT / "scripts" / "release-tests" / "test-lite-mode.sh",
}

pytestmark = pytest.mark.skipif(
    not REHEARSE.is_file() or not all(p.is_file() for p in SCENARIOS.values()),
    reason="the release-test scripts are not present in this checkout",
)


def test_the_orchestrator_pins_the_branch_under_test():
    """65-rehearse.sh must decide the branch; leaving it to the default is the defect."""
    text = REHEARSE.read_text(encoding="utf-8")
    assert "TO_BRANCH" in text, (
        "65-rehearse.sh does not set TO_BRANCH, so both install-based scenarios fall back "
        "to master and the rehearsal validates the PREVIOUS release's deployment files "
        "rather than the one being cut."
    )


def test_the_branch_is_derived_from_the_checkout_not_hardcoded():
    text = REHEARSE.read_text(encoding="utf-8")
    assert re.search(r"rev-parse\s+--abbrev-ref\s+HEAD", text), (
        "the branch is not derived from the checkout. Hardcoding any name (including "
        "'master') reintroduces the defect for every release cut from somewhere else."
    )


def test_an_unpushed_branch_fails_loudly_rather_than_falling_back():
    """A silent fallback to master is the same bug wearing a different hat.

    ``setup-opentranscribe.sh`` DOWNLOADS from the branch, so an unpushed branch cannot be
    installed. Quietly using master instead would produce a green rehearsal for code that
    was never exercised — which is precisely the failure this module documents.
    """
    text = REHEARSE.read_text(encoding="utf-8")
    assert "ls-remote" in text, (
        "the orchestrator does not verify the branch exists on the remote before handing "
        "it to an installer that downloads from it"
    )
    # The refusal has to actually stop the stage.
    window_start = text.find("ls-remote")
    window = text[window_start : window_start + 900]
    assert re.search(r"\bexit\s+\d|die\b|fail_out", window), (
        "an unpushed branch does not stop the stage; it must refuse rather than rehearse "
        f"the wrong tree:\n{window[:400]}"
    )


@pytest.mark.parametrize(("name", "path"), sorted(SCENARIOS.items()))
def test_each_scenario_still_honours_an_explicit_override(name: str, path: Path):
    """The scripts stay independently runnable with an explicit TO_BRANCH.

    They are documented as usable on their own, and the release-tests README/`--help` both
    advertise the variable. The orchestrator supplying a value must not remove the knob.
    """
    text = path.read_text(encoding="utf-8")
    assert re.search(r'TO_BRANCH="\$\{TO_BRANCH:-', text), (
        f"{name} no longer honours an inherited TO_BRANCH, so the orchestrator could not "
        "pin the branch under test even if it wanted to"
    )
