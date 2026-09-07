"""Every release-test driver must exit non-zero when an assertion FAILed (issue #620).

Three separate bugs, same root cause — `as_summary` (``scripts/release-tests/lib/
assertions.sh``) deliberately returns 1 when any assertion FAILed, but the scripts calling
it ran under ``set -euo pipefail``:

1. **Vacuous transcript-comparison assertions** (test-upgrade.sh phase 10 "transcript
   prefix preserved" and phase 16's B-7 "matches the pre-upgrade snapshot exactly"): both
   were inline ``python3 - ... <<'PY' || true`` heredocs that wrote PASS/FAIL text
   directly into the report file and printed it to stdout, but never called ``as_record``
   — so a real mismatch printed the word "FAIL" while ``as_fail`` stayed 0 and
   ``as_summary`` reported a clean run. Fixed by having the heredoc print only a diagnostic
   to stdout and ``sys.exit(0 if ok else 1)``, with the calling bash capturing that via
   ``if detail="$(python3 ...)"; then as_record PASS ...; else as_record FAIL ... "$detail";
   fi`` — never ``if local detail=$(...)``, which captures ``local``'s own exit status, not
   the command's, silently making the assertion pass unconditionally (the exact bug class
   being fixed here).
2. **B-4's vacuous pass** (test-upgrade.sh phase 16): ``mismatched`` is built by filtering
   ``docker ps`` for ``davidamacey/opentranscribe-*`` images; if ZERO app containers are
   running (issue #618's own root cause), ``mismatched`` stays empty and B-4 PASSES having
   checked nothing. Fixed by asserting ``app_containers`` is non-empty (and that backend +
   frontend are among them) BEFORE trusting an empty ``mismatched`` list.
3. **The final ``as_summary | tee -a ...`` under `set -o pipefail`** in all three drivers
   (test-upgrade.sh phase 18, test-fresh-install.sh phase 06, test-lite-mode.sh phase 07):
   a non-zero return from either stage of that pipeline trips ``set -e`` on the spot,
   silently truncating the report (no "Finished:" line) and — for test-lite-mode.sh, where
   phase 08 follows the summary — aborting the rest of the driver outright. Fixed by
   capturing the verdict into ``RELEASE_TEST_EXIT_CODE`` instead of trusting the pipeline's
   own exit code, and propagating it with an explicit
   ``exit "${RELEASE_TEST_EXIT_CODE:-0}"`` at the very end of each driver — the capture
   alone is not enough; without the trailing ``exit`` every driver exits 0 regardless of
   the captured verdict, which is WORSE than the truncation bug (silently green instead of
   noisily truncated).

Static: parses the shipped scripts and asserts the shape, the same house style as
``test_opentr_restore_safety.py``. Red-first / must-fire evidence for the actual runtime
behaviour (not just the shape) was verified manually against ``scripts/release-tests/lib/
assertions.sh`` sourced in a scratch directory — see the PR/commit description for the two
before/after transcripts; that manual proof is not repeated here as an executed test because
it would need to shell out to python + bash to reproduce the exact pipeline-under-set-e
behaviour, which the static shape assertions below already pin structurally.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TEST_UPGRADE = _REPO_ROOT / "scripts" / "release-tests" / "test-upgrade.sh"
_TEST_FRESH_INSTALL = _REPO_ROOT / "scripts" / "release-tests" / "test-fresh-install.sh"
_TEST_LITE_MODE = _REPO_ROOT / "scripts" / "release-tests" / "test-lite-mode.sh"

_ALL_DRIVERS = [_TEST_UPGRADE, _TEST_FRESH_INSTALL, _TEST_LITE_MODE]

pytestmark = pytest.mark.skipif(
    not all(p.exists() for p in _ALL_DRIVERS),
    reason="scripts/release-tests/*.sh not present in this checkout",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------------------------
# 1. Every driver captures as_summary's verdict and propagates it as its own exit code.
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("script", _ALL_DRIVERS, ids=lambda p: p.name)
def test_as_summary_is_captured_into_release_test_exit_code(script: Path) -> None:
    text = _read(script)
    assert re.search(r"as_summary \| tee -a .* \|\| RELEASE_TEST_EXIT_CODE=\$\?", text), (
        f"{script.name}: as_summary must be captured into RELEASE_TEST_EXIT_CODE, not left "
        f"to trip `set -e` on a non-zero return from either stage of the pipeline "
        f"(issue #620 item 3 / #617-#618 class)"
    )
    assert "RELEASE_TEST_EXIT_CODE=0" in text, (
        f"{script.name}: expected RELEASE_TEST_EXIT_CODE initialised to 0 before the "
        f"as_summary capture, so a resumed run that skips this phase still defaults cleanly"
    )


@pytest.mark.unit
@pytest.mark.parametrize("script", _ALL_DRIVERS, ids=lambda p: p.name)
def test_driver_ends_with_an_explicit_exit_of_the_captured_code(script: Path) -> None:
    text = _read(script)
    assert re.search(r'exit "\$\{RELEASE_TEST_EXIT_CODE:-0\}"\s*$', text.rstrip() + "\n"), (
        f'{script.name}: must end with `exit "${{RELEASE_TEST_EXIT_CODE:-0}}"` -- '
        f"capturing the verdict without propagating it makes the script exit 0 "
        f"unconditionally, which is WORSE than the truncation bug it replaces "
        f"(silently green instead of noisily truncated)"
    )


@pytest.mark.unit
def test_capture_detector_fires_when_the_pipeline_is_bare() -> None:
    """Must-fire control: the original unguarded shape."""
    bare = 'as_summary | tee -a "$TEST_REPORT_FILE"\n'
    assert not re.search(r"as_summary \| tee -a .* \|\| RELEASE_TEST_EXIT_CODE=\$\?", bare)


@pytest.mark.unit
def test_exit_detector_fires_when_the_driver_has_no_trailing_exit() -> None:
    """Must-fire control: capture present, but no propagating exit at the end."""
    captured_but_not_propagated = (
        'RELEASE_TEST_EXIT_CODE=0\nas_summary | tee -a "$f" || RELEASE_TEST_EXIT_CODE=$?\n'
        'echo "Done."\n'
    )
    assert not re.search(
        r'exit "\$\{RELEASE_TEST_EXIT_CODE:-0\}"\s*$',
        captured_but_not_propagated.rstrip() + "\n",
    )


# ---------------------------------------------------------------------------------------------
# 2. test-upgrade.sh's two transcript-comparison sites route through as_record, not a bare
#    heredoc that writes the report file itself and can never fail the gate.
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_test_upgrade_has_no_python_heredoc_writing_the_report_directly() -> None:
    text = _read(_TEST_UPGRADE)
    assert 'open(report, "a")' not in text, (
        "test-upgrade.sh has an inline python heredoc writing $TEST_REPORT_FILE directly -- "
        "that bypasses as_record, so a real mismatch is printed but never counted as a "
        "failure (issue #620 item 2)"
    )
    assert "<<'PY' || true" not in text, (
        "test-upgrade.sh has a python heredoc unconditionally forced to succeed with "
        "`|| true` -- its own PASS/FAIL text can never fail the gate"
    )


@pytest.mark.unit
def test_transcript_comparison_sites_call_as_record() -> None:
    text = _read(_TEST_UPGRADE)
    assert text.count('as_record PASS "transcript prefix preserved for file $fid"') == 1
    assert (
        text.count(
            'as_record PASS "B-7: transcript for file $fid matches the pre-upgrade '
            'snapshot exactly"'
        )
        == 1
    )


@pytest.mark.unit
def test_report_write_detector_fires_on_the_original_bare_heredoc_shape() -> None:
    """Must-fire control: the original unguarded shape (writes the report itself, `|| true`)."""
    original_shape = (
        'python3 - "$pre" "$post" "$fid" "$TEST_REPORT_FILE" <<\'PY\' || true\n'
        "...\n"
        'with open(report, "a") as f:\n'
        "    f.write(...)\n"
        "PY\n"
    )
    assert 'open(report, "a")' in original_shape
    assert "<<'PY' || true" in original_shape


# ---------------------------------------------------------------------------------------------
# 3. B-4 asserts app containers exist at all BEFORE trusting an empty mismatch list.
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_b4_asserts_app_containers_exist_before_the_tag_comparison() -> None:
    text = _read(_TEST_UPGRADE)
    non_empty_idx = text.find("B-4a: app-owned opentranscribe-* containers are running at all")
    tag_check_idx = text.find(
        "B-4: every app-owned opentranscribe-* image resolves :${FROM_VERSION}"
    )
    assert non_empty_idx != -1, "expected a B-4a non-emptiness assertion"
    assert tag_check_idx != -1, "expected the B-4 tag-comparison assertion"
    assert non_empty_idx < tag_check_idx, (
        "B-4a (containers exist at all) must be asserted BEFORE B-4 (tag comparison) -- "
        "otherwise zero running containers trivially empties `mismatched` and B-4 passes "
        "having checked nothing (issue #618's own root cause, issue #620 item 3)"
    )


@pytest.mark.unit
def test_b4_ordering_detector_fires_when_reversed() -> None:
    """Must-fire control: a synthetic body with the tag check before the emptiness check."""
    reversed_body = (
        'as_assert "B-4: every app-owned opentranscribe-* image resolves :${FROM_VERSION}" '
        "'((${#mismatched[@]} == 0))'\n"
        'as_assert "B-4a: app-owned opentranscribe-* containers are running at all" '
        "'((${#app_containers[@]} > 0))'\n"
    )
    non_empty_idx = reversed_body.find("B-4a: app-owned")
    tag_check_idx = reversed_body.find("B-4: every app-owned")
    assert not (non_empty_idx < tag_check_idx), "fixture is wrong: this must be REVERSED"
