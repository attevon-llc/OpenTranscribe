"""A rehearsal assertion must measure the stack UNDER TEST, and must not race it.

Two defects found on the v0.5.0 rehearsal (2026-09-13), both in
`test-fresh-install.sh`'s phase 06, both of which failed against a **working**
deployment. Neither was a product bug, and each cost a full scenario run to find.

**1. Wrong stack.** `diar-native-smoke.sh` hardcoded `ENV_FILE="$REPO_ROOT/.env"`,
so the residency assertion compared the freshly-installed TEST deployment's sidecar
against the **developer's dev-stack** `GPU_DEVICE_ID`. It reported

    diar-server is on GPU <2> but the project configured index 1

about a sidecar sitting on exactly the card the scenario had asked for. Measured A/B
against the live scenario stack: `ENV_FILE=<repo>/.env` -> `fail`, and
`ENV_FILE=<TEST_ROOT>/install/opentranscribe/.env` -> `pass` (`gpu_index: 2`,
`used_mib: 4688`). This is the repo's own `readiness-probe-target` shape — a probe
whose target is derived from a stack other than the one being tested — and it passes
only by coincidence, whenever the two configurations happen to agree.

**2. Racing the indexer.** `status=completed` means the TRANSCRIPT is written;
indexing into OpenSearch is a separate async Celery task. The hybrid-search
assertion fired one query the instant the file completed and asserted on it. It saw
**0 hits while `transcript_chunks` held 18 documents** by the time the failure was
investigated. Same class as the speaker-embedding race already fixed in
`test-lite-mode.sh`.

These are scanned structurally rather than executed: running them needs a real
installed deployment, a GPU and OpenSearch. Each check therefore carries a
must-fire control proving the scan can see the defect it describes — a scanner that
silently matches nothing reports a clean tree.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
FRESH_INSTALL = REPO_ROOT / "scripts" / "release-tests" / "test-fresh-install.sh"
DIAR_SMOKE = REPO_ROOT / "scripts" / "diar-native-smoke.sh"

#: `ENV_FILE=` assignments that are NOT overridable from the environment.
_HARDCODED_ENV_FILE = re.compile(r'^\s*ENV_FILE="\$\{?REPO_ROOT\}?/\.env"\s*$', re.MULTILINE)
#: The overridable form: a `${ENV_FILE:-...}` default.
_OVERRIDABLE_ENV_FILE = re.compile(r'^\s*ENV_FILE="\$\{ENV_FILE:-', re.MULTILINE)


def test_the_files_this_scans_exist():
    """GUARD THE GUARD: a missing path would make every scan below vacuously green."""
    assert FRESH_INSTALL.is_file(), f"missing {FRESH_INSTALL}"
    assert DIAR_SMOKE.is_file(), f"missing {DIAR_SMOKE}"


def test_the_gpu_residency_probe_reads_an_overridable_env_file():
    """THE property for defect 1. A caller must be able to aim it at another deployment."""
    source = DIAR_SMOKE.read_text(encoding="utf-8")

    assert _OVERRIDABLE_ENV_FILE.search(source), (
        "diar-native-smoke.sh's ENV_FILE is not overridable, so a caller cannot point it "
        "at the deployment under test — it will silently measure against this checkout's "
        ".env instead"
    )
    assert not _HARDCODED_ENV_FILE.search(source), (
        "ENV_FILE is pinned to the repo's own .env; see this module's docstring for the "
        "failure that produces"
    )


def test_the_hardcoded_detector_actually_fires():
    """GUARD THE GUARD: without this, a typo in the regex passes every file."""
    assert _HARDCODED_ENV_FILE.search('ENV_FILE="$REPO_ROOT/.env"\n')
    assert _HARDCODED_ENV_FILE.search('ENV_FILE="${REPO_ROOT}/.env"\n')
    # the fixed form must NOT match, or the fix could never satisfy the test above
    assert _HARDCODED_ENV_FILE.search('ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env}"\n') is None
    assert _OVERRIDABLE_ENV_FILE.search('ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env}"\n')


def test_the_scenario_aims_the_probe_at_its_own_installed_deployment():
    """The override is useless unless the caller actually passes it."""
    source = FRESH_INSTALL.read_text(encoding="utf-8")

    lines = source.splitlines()
    call_at = next(
        (i for i, line in enumerate(lines) if "diar-native-smoke.sh" in line and "--json" in line),
        None,
    )
    assert call_at is not None, "the scenario no longer invokes diar-native-smoke.sh --json"

    # The invocation is a line continuation, so ENV_FILE sits just above the script name.
    block = "\n".join(lines[max(0, call_at - 3) : call_at + 1])
    assert "ENV_FILE=" in block, (
        f"diar-native-smoke.sh is invoked without ENV_FILE:\n{block}\n"
        "It therefore falls back to the repo's .env and measures the wrong stack."
    )
    assert "TEST_ROOT" in source, "the scenario has no TEST_ROOT to derive an env file from"
    env_assignment = next(
        (line for line in lines if "diar_env_file=" in line and "TEST_ROOT" in line), None
    )
    assert env_assignment is not None, (
        "the ENV_FILE handed to the probe must be derived from TEST_ROOT — the deployment "
        "this run installed — not from the repo checkout"
    )


def test_the_hybrid_search_assertion_is_polled_not_single_shot():
    """THE property for defect 2. Indexing is async; one query right after completion races it."""
    source = FRESH_INSTALL.read_text(encoding="utf-8")

    idx = source.index('as_assert_ge "hybrid search returns hits"')
    # The loop that feeds it must sit in the ~25 lines immediately above the assertion.
    preceding = source[:idx].rsplit("\n", 25)[-1] if "\n" in source[:idx] else source[:idx]
    window = "\n".join(source[:idx].splitlines()[-25:])

    assert "ac_search" in window, "the assertion no longer has a visible search call above it"
    assert re.search(r"for\s+\w+\s+in\s+\$\(seq\b", window), (
        "the hybrid-search assertion is single-shot. `status=completed` means the transcript "
        "is written, not that OpenSearch has indexed it — this assertion has already failed "
        "with 0 hits against an index that held 18 documents. Poll, as test-lite-mode.sh does."
    )
    assert "sleep" in window, "a retry loop with no sleep spins instead of waiting"
    assert preceding is not None  # keeps the narrower slice meaningful to a reader


def test_the_poll_detector_would_notice_the_single_shot_form():
    """GUARD THE GUARD: prove the scan distinguishes polled from unpolled.

    The pre-fix body is reproduced here verbatim in shape; it must NOT satisfy the
    check above, or that check could never have failed on the code it was written for.
    """
    single_shot = """
            local hits
            hits=$(ac_search "the" | python3 -c 'print(1)')
            as_assert_ge "hybrid search returns hits" "$hits" 1
"""
    assert re.search(r"for\s+\w+\s+in\s+\$\(seq\b", single_shot) is None
    assert "sleep" not in single_shot
