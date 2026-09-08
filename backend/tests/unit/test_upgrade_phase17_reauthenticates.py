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


def test_f4_compares_row_identity_not_a_content_digest():
    """No whole-table digest oracle is correct here, and BOTH candidates were tried and failed.

    * **vs the PRE-upgrade snapshot** — fails on any migration that legitimately rewrites a
      FROM-era value. Measured 2026-09-07: ``media_file.status`` is ``COMPLETED`` at v0.3.3 and
      ``completed`` at v0.5.0, an intentional enum-case normalisation, so the v0.3.3 hop failed
      F-4 for the product doing exactly what it is supposed to do.
    * **vs the POST-upgrade snapshot** — fails because the rollback is SUPPOSED to lose data.
      Measured on the same run: post-upgrade rows are ids ``2, 3`` and recovered rows are
      ``1, 2``. File 3 was uploaded *after* the backup, so its absence is correct — R-5 asserts
      that absence deliberately. This was my own first "fix" and it was wrong.

    A digest cannot separate "a migration rewrote a value" (fine) from "a row went missing"
    (the actual failure), so F-4 compares row IDENTITY — (id, filename) — against the set the
    ROLLBACK restored. ``status`` is excluded for the enum-case reason above.
    """
    body = _phase_17_body()
    assert "snapshots/restored/media_files.txt" in body, (
        "F-4 does not compare against the RESTORED row set. Neither the pre- nor the "
        "post-upgrade snapshot is a valid oracle for the recovery loop (see this test's "
        "docstring — both were measured and both produce a confident FAIL about the product "
        "for behaviour that is correct)."
    )
    assert "snapshots/before/db-fingerprint/media_file.digest" not in body, (
        "F-4 still reads the PRE-upgrade digest, which fails on any legitimate data migration"
    )
    assert "cut -d'|' -f1,2" in body, (
        "F-4 no longer projects to (id, filename). Comparing whole rows reintroduces the "
        "status-casing false failure the identity comparison exists to avoid."
    )


def test_the_restored_row_set_is_actually_recorded():
    """Non-vacuity: an oracle nothing writes is the R-6 failure mode all over again.

    This nearly shipped — the F-4 rewrite read ``snapshots/restored/media_files.txt`` while
    nothing in the script wrote it, which would have compared against an absent file.
    """
    text = UPGRADE.read_text(encoding="utf-8")
    # `: > file` (the truncate-on-failure fallback beside it) is deliberately NOT counted:
    # it guarantees the file EXISTS, which is the opposite of guaranteeing it holds the rows.
    writes = re.findall(r'(?<!:)\s>\s*"\$TEST_ROOT/snapshots/restored/media_files\.txt"', text)
    assert len(writes) == 1, (
        "snapshots/restored/media_files.txt must be populated by exactly one real query "
        f"(found {len(writes)}). If nothing writes it, F-4's oracle never exists — exactly "
        "how R-6 passed unconditionally for its whole life."
    )
    assert "SELECT id, filename, status FROM media_file ORDER BY id" in text, (
        "the restored row set is no longer captured with the same projection the before/after "
        "snapshots use, so F-4 would compare two differently-shaped files"
    )


def test_the_restored_rows_are_not_read_through_the_stopped_api():
    """R-13 has just asserted the restore leaves the application STOPPED, on purpose.

    ``snapshot_state`` is the obvious way to write that snapshot and is the wrong one: it also
    queries the API (files.json, routes.txt, version.json), every call of which would fail at
    this point. Only the DB half is meaningful here.
    """
    text = UPGRADE.read_text(encoding="utf-8")
    marker = '"$TEST_ROOT/snapshots/restored/media_files.txt"'
    assert marker in text, (
        "there is no restored-snapshot write site to check, so this guard would pass by "
        "describing nothing — see test_the_restored_row_set_is_actually_recorded"
    )
    before_write = text.split(marker)[0]
    tail = before_write[-2000:]
    assert "snapshot_state restored" not in tail, (
        "the restored snapshot is taken with snapshot_state, which makes API calls against an "
        "application R-13 has just asserted is stopped"
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


# --------------------------------------------- R-7 must not demand a head that never existed


def _r7_block(text: str) -> str:
    """The R-7 assertion block, anchored on its verdict LABEL.

    Anchoring on the bare string "R-7" finds a passing mention in a comment ~70 lines earlier
    and silently windows the wrong region — which is how the first draft of this guard failed
    against a correct script.
    """
    label = "R-7: alembic_version restored to the FROM release's own head"
    assert label in text, "the R-7 verdict label has changed; re-point this guard"
    first = text.index(label)
    last = text.rindex(label)
    return text[max(0, first - 2000) : last + 500]


def test_r7_skips_when_the_from_release_predates_alembic():
    """v0.3.3 is bootstrapped by ``init_db.sql`` and has no ``alembic_version`` table at all.

    R-7 derived an expected head (``v020_add_system_settings``) from the FROM worktree's
    migration chain and asserted the restored database matched it — for a release that never
    wrote that table. The restore was correct; the assertion was measuring a row the FROM
    release does not create. ``snapshots/before/alembic_head.txt`` literally records
    ``(alembic_version table absent — pre-Alembic schema)``, and phase 13 already carries a
    guard saying so, so the fact was available and simply not consulted here.

    A SKIP, not a silent pass: "this release has no head to restore" and "the head restored
    correctly" are different statements and the report must not conflate them.
    """
    text = UPGRADE.read_text(encoding="utf-8")
    window = _r7_block(text)
    assert "pre-Alembic schema" in window, (
        "R-7 does not consult snapshots/before/alembic_head.txt for the pre-Alembic sentinel, "
        "so it still demands an alembic_version row from a FROM release that never creates one"
    )
    assert 'as_record SKIP "R-7' in window, (
        "R-7 does not SKIP on a pre-Alembic FROM release. Passing it silently would claim the "
        "head was verified; failing it blames the product for the harness's wrong expectation."
    )


def test_r7_still_asserts_the_head_on_an_alembic_from_release():
    """The must-still-fire half: the skip must be conditional, not a deletion.

    The v0.4.1 hop DOES have an alembic_version row and R-7 passing there is real evidence.
    """
    text = UPGRADE.read_text(encoding="utf-8")
    window = _r7_block(text)
    assert 'as_record FAIL "R-7' in window or "as_assert" in window, (
        "R-7 can no longer fail at all — the pre-Alembic skip has swallowed the assertion "
        "instead of narrowing it"
    )
