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
