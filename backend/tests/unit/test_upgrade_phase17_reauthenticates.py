"""Phase 17 must re-authenticate before asserting anything through the API.

``phase_17_roll_forward_again`` restarts the whole application on the TO image. The last
login before it was ``B-6``'s, against the **rolled-back** stack. That session does not
survive, so ``ac_search`` came back 401, the JSON parse failed, and ``hits`` landed on ``0``.

F-5 therefore reported "hybrid search returns no hits after recovery" when what it had
actually measured was "my old session is no longer valid". Measured 2026-09-07 on the v0.3.3
hop: at the moment F-5 claimed zero, OpenSearch held ``transcript_chunks=50`` and
``transcripts=1``, with 2 rows in ``media_file``. The index had recovered fine.

Two separate defects, and fixing only the first would leave a trap:

1. **No re-login.** Phase 17 waits for health and then calls the API with a stale token.
2. **A failed call and an empty result both became ``0``.** ``... || echo 0`` cannot tell
   "the request failed" from "the search legitimately found nothing", so the assertion
   reported the wrong finding either way and would have kept doing so after the login fix.

Same principle as the voiceprint-export and R-7 fixes on this branch: a probe that cannot
run must say so, not return a value that looks like a measurement.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
UPGRADE = REPO_ROOT / "scripts" / "release-tests" / "test-upgrade.sh"

pytestmark = pytest.mark.skipif(
    not UPGRADE.is_file() or shutil.which("bash") is None,
    reason="scripts/release-tests/test-upgrade.sh is not present in this checkout",
)


def _phase_17_body() -> str:
    text = UPGRADE.read_text(encoding="utf-8")
    match = re.search(r"^phase_17_roll_forward_again\(\) \{(?P<body>.*?)^\}", text, re.S | re.M)
    assert match, "phase_17_roll_forward_again has moved or been renamed; re-point this guard"
    return match.group("body")


def test_phase_17_logs_in_again_before_using_the_api():
    body = _phase_17_body()
    assert "ac_login" in body, (
        "phase 17 restarts the app on the TO image and then calls the API with the session "
        "B-6 obtained from the ROLLED-BACK stack. That token is stale, so an API assertion "
        "here measures session validity rather than the thing it names — F-5 reported an "
        "empty search index while OpenSearch actually held 50 chunks."
    )


def test_the_login_comes_before_the_search_probe():
    """Order matters: a login after the probe fixes nothing."""
    body = _phase_17_body()
    login_at = body.index("ac_login")
    search_at = body.index("ac_search")
    assert login_at < search_at, (
        "phase 17 logs in AFTER it searches, so the search still runs on the stale session"
    )


def test_a_failed_search_call_is_not_reported_as_zero_hits():
    """The trap that would survive the login fix on its own.

    `... || echo 0` collapses "the request failed" into "the search found nothing", so the
    assertion names the wrong finding either way.
    """
    body = _phase_17_body()
    assert "|| echo 0" not in body, (
        "the search probe still falls back to `echo 0`, so a 401, a timeout and a genuinely "
        "empty index are indistinguishable — F-5 would keep reporting an index problem for "
        "an auth problem."
    )
    assert "PARSE_ERROR" in body or "parseable JSON" in body, (
        "phase 17 does not distinguish an unparseable response from a zero result; a probe "
        "that could not run must say so rather than return a number"
    )


def test_a_failed_login_is_reported_as_an_auth_failure_not_an_index_failure():
    body = _phase_17_body()
    assert re.search(r'as_record FAIL "F-5[^"]*"[^\n]*\n?[^\n]*AUTH failure', body) or (
        "AUTH failure" in body
    ), (
        "if the re-login itself fails, F-5 must say the search was never exercised — "
        "otherwise the fix just relabels an auth failure as an empty index one level up"
    )


def test_r7_reports_why_it_could_not_read_the_head():
    """R-7's sibling defect: an empty value with the reason thrown away.

    The first investigation of this failure produced `actual=''` and nothing else — an
    empty string that could mean the container was gone, the database was mid-recreate, or
    the table did not exist yet. stderr was going to /dev/null.
    """
    text = UPGRADE.read_text(encoding="utf-8")
    assert "restored_head_err" in text, (
        "R-7 still discards psql's stderr, so an unreadable alembic_version reports as "
        "actual='' with no way to tell which of several causes it was"
    )
    assert "no stderr captured" in text, (
        "R-7 does not say when it has no stderr to show — 'the reason is missing' and "
        "'there was no reason' must be distinguishable"
    )


# ------------------------------------------- F-4 must compare against the RIGHT baseline


def test_f4_compares_the_recovery_against_the_post_upgrade_state():
    """The recovery loop ends where the FIRST upgrade ended, not where the deployment began.

    F-4 compared the recovered rows against the PRE-upgrade snapshot, which asserts that the
    TO migrations rewrite no FROM-era value. That is simply untrue: measured 2026-09-07,
    ``media_file.status`` is ``COMPLETED`` at v0.3.3 and ``completed`` at v0.5.0 — an
    intentional enum-case normalisation — so the v0.3.3 hop failed F-4 for the product doing
    exactly what it is supposed to do. The v0.4.1 hop passed only because that migration
    predates v0.4.1, which is what made it look version-specific rather than wrong.

    ⚠️ No coverage is lost. "the rollback gave me my data back" is a DIFFERENT claim and is
    asserted separately by phase 15's before-vs-restored comparison (verified passing on the
    same run: `PASS restore: media_file content digest unchanged`).
    """
    body = _phase_17_body()
    assert "snapshots/after/media_file.from-cols.digest" in body, (
        "F-4 does not use the post-upgrade FROM-column digest as its oracle. Comparing the "
        "recovered state against the PRE-upgrade snapshot makes any legitimate data "
        "migration look like recovery damage."
    )
    assert "snapshots/before/db-fingerprint/media_file.digest" not in body, (
        "F-4 still reads the PRE-upgrade digest as its expected value — that is the wrong "
        "oracle for a recovery loop"
    )


def test_the_post_upgrade_from_column_digest_is_actually_recorded():
    """Non-vacuity: an oracle nothing writes is the R-6 failure mode all over again."""
    text = UPGRADE.read_text(encoding="utf-8")
    writes = re.findall(r'>\s*"\$TEST_ROOT/snapshots/after/media_file\.from-cols\.digest"', text)
    assert len(writes) == 1, (
        "snapshots/after/media_file.from-cols.digest must be written exactly once (found "
        f"{len(writes)}). If nothing writes it, F-4's oracle never exists — exactly how R-6 "
        "passed unconditionally for its whole life."
    )
    # It has to be recorded from the POST-UPGRADE database, i.e. beside the `after`
    # fingerprint. Written at any other point it would describe the wrong state.
    assert "snapshots/after/db-fingerprint" in text.split(writes[0])[0][-1200:], (
        "the post-upgrade FROM-column digest is not recorded next to the `after` "
        "fingerprint, so it may not describe the post-upgrade state at all"
    )


def test_a_missing_oracle_is_a_failure_not_a_fallback():
    """Falling back to the pre-upgrade digest would restore the original wrong verdict.

    Refusing is right here: a wrong oracle produces a confident FAIL about the PRODUCT for
    what is actually a harness defect, which is the most expensive kind of wrong.
    """
    body = _phase_17_body()
    assert 'as_record FAIL "F-4' in body, (
        "F-4 does not fail when its post-upgrade oracle is missing; it must refuse rather "
        "than silently compare against something else"
    )
    assert "NOT evidence about the recovery" in body, (
        "the missing-oracle failure does not say that it is a harness problem rather than a "
        "finding about the release"
    )
