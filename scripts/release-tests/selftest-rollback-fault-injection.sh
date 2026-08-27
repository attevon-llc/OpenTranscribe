#!/bin/bash
# Self-test for the rollback tail's own failure detection (issue #598 §9.3).
#
# WHY THIS EXISTS
#
# A leg that silently asserts nothing looks exactly like a leg that passes —
# the same failure class `scripts/audit-tests.py --selftest` exists to catch
# for the Python suite. test-upgrade.sh's phases 13-17 make a real claim
# ("this restore reproduced the pre-upgrade state exactly"), and the only way
# to trust that claim is to prove the check can ALSO fail: deliberately break
# each input the tail depends on and confirm the corresponding assertion
# actually goes red.
#
# This exercises lib/db-snapshot.sh directly against a throwaway, isolated
# Postgres container (never the live stack, never TEST_ROOT) — the same
# probe methodology issue #598's own investigation used to first measure the
# defect in `opentr.sh restore`. It runs in seconds, not the 3-5 hours a full
# Scenario B run costs, so it can be run on every guardrails/db-snapshot
# change the way selftest-cleanup.sh already is for the cleanup logic.
#
# Usage: ./scripts/release-tests/selftest-rollback-fault-injection.sh
# Exit:  0 all cases behaved · 1 a case failed

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTAINER="ot-selftest-rollback-pg"
WORKDIR="$(mktemp -d /tmp/ot-selftest-rollback.XXXXXX)"

# shellcheck source=lib/db-snapshot.sh
source "$SCRIPT_DIR/lib/db-snapshot.sh"
# shellcheck source=lib/assertions.sh
source "$SCRIPT_DIR/lib/assertions.sh"

PASS=0
FAIL=0
ok()  { echo -e "  \033[0;32mPASS\033[0m  $*"; PASS=$((PASS + 1)); }
bad() { echo -e "  \033[0;31mFAIL\033[0m  $*"; FAIL=$((FAIL + 1)); }

cleanup() {
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    rm -rf "$WORKDIR"
}
trap cleanup EXIT

echo "Rollback-tail fault-injection self-test (container: $CONTAINER, isolated --network none)"
echo

docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d --rm --name "$CONTAINER" --network none \
    -e POSTGRES_PASSWORD=selftest -e POSTGRES_USER=postgres -e POSTGRES_DB=opentranscribe \
    postgres:17.5-alpine >/dev/null

# Wait for readiness — no host network, so pg_isready must run INSIDE the
# container. The official postgres image runs a temporary server for initdb,
# stops it, then starts the real one; pg_isready can report ready during that
# temporary instance and then fail with "the database system is shutting
# down" moments later. A real query, not pg_isready, is what actually proves
# the FINAL server is up — retried across the whole window, not probed once.
deadline=$(( $(date +%s) + 60 ))
until docker exec "$CONTAINER" psql -U postgres -d opentranscribe -c 'SELECT 1;' >/dev/null 2>&1; do
    if (( $(date +%s) > deadline )); then
        echo "postgres never became ready" >&2
        exit 1
    fi
    sleep 1
done

# A minimal schema shaped like the real one, just enough for
# DBS_FINGERPRINT_TABLES to have something to measure.
docker exec "$CONTAINER" psql -v ON_ERROR_STOP=1 -U postgres -d opentranscribe -c '
CREATE TABLE media_file (id INTEGER PRIMARY KEY, filename TEXT, status TEXT);
CREATE TABLE transcript_segment (id INTEGER PRIMARY KEY, media_file_id INTEGER, text TEXT);
CREATE TABLE speaker (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE "user" (id INTEGER PRIMARY KEY, email TEXT);
CREATE TABLE tag (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY);
INSERT INTO media_file VALUES (1, '"'"'alpha.mp3'"'"', '"'"'completed'"'"'), (2, '"'"'beta.mp3'"'"', '"'"'completed'"'"');
INSERT INTO alembic_version VALUES ('"'"'selftest_head_v1'"'"');
' >/dev/null

# ── Case 1: dbs_fingerprint catches a real content change (delete+insert,
# same row count) — the baseline the rest of this script trusts. ───────────
echo "1. dbs_fingerprint detects a real mutation"
dbs_fingerprint "$CONTAINER" postgres opentranscribe "$WORKDIR/fp-before" >/dev/null 2>&1
docker exec "$CONTAINER" psql -v ON_ERROR_STOP=1 -U postgres -d opentranscribe -c \
    "DELETE FROM media_file WHERE id = 2; INSERT INTO media_file VALUES (3, 'gamma.mp3', 'completed');" >/dev/null
dbs_fingerprint "$CONTAINER" postgres opentranscribe "$WORKDIR/fp-damaged" >/dev/null 2>&1

count_before="$(cat "$WORKDIR/fp-before/media_file.count")"
count_damaged="$(cat "$WORKDIR/fp-damaged/media_file.count")"
if [[ "$count_before" == "$count_damaged" ]]; then
    ok "row COUNT alone is unchanged by a delete+insert pair (2 -> 2) — this is why the tail uses digests, not counts"
else
    bad "row count changed by a delete+insert pair — the test fixture itself is wrong"
fi
if TEST_REPORT_FILE=/dev/null dbs_diff_fingerprints "$WORKDIR/fp-before" "$WORKDIR/fp-damaged" "selftest" media_file >/dev/null 2>&1; then
    bad "dbs_diff_fingerprints reported PASS across a real delete+insert mutation — it cannot tell damage from a healthy restore"
else
    ok "dbs_diff_fingerprints reports the digest mismatch a row-count check would miss"
fi

# ── Case 2: ROLLBACK_INJECT_FAULT=truncate ──────────────────────────────────
#
# MEASURED, not assumed: psql reading a plain-format dump from a FILE treats
# an unterminated `COPY ... FROM stdin` at EOF as simply ending the copy — no
# error, exit 0 — rather than the parse failure a first guess would expect.
# So a dump truncated after the LAST table's COPY header still replays
# "cleanly" and is silently missing whatever rows came after the cut. This is
# the SAME failure shape issue #598 originally measured in `opentr.sh
# restore` (reports success, changed nothing) — truncation reproduces it
# structurally instead of relying on the historical bug still being present.
# The assertion that must catch it is therefore data content (a wrong row
# count / digest), never the exit code.
echo "2. truncate: a dump cut inside a COPY block silently drops rows (exit 0, wrong data)"
dbs_dump "$CONTAINER" postgres opentranscribe "$WORKDIR/full.sql" >/dev/null 2>&1

if full_rows=$(dbs_verify_dump_restores "$CONTAINER" postgres "$WORKDIR/full.sql" ot_selftest_scratch 2>/dev/null); then
    if [[ "$full_rows" == "2" ]]; then
        ok "the UNMODIFIED dump replays with all 2 media_file rows (control)"
    else
        bad "the unmodified dump replayed with $full_rows rows, expected 2 — cannot trust the truncate case below"
    fi
else
    bad "the unmodified dump did not replay at all — cannot trust the truncate case below"
fi
dbs_scratch_drop "$CONTAINER" postgres ot_selftest_scratch

# Cut to the COPY header for media_file plus exactly ONE of its two data
# rows — a clean line-based cut (never mid-row), landing squarely inside the
# COPY block rather than hoping a byte/line fraction does.
copy_line="$(grep -n '^COPY public\.media_file ' "$WORKDIR/full.sql" | head -1 | cut -d: -f1)"
if [[ -z "$copy_line" ]]; then
    bad "could not find media_file's COPY line in the dump — fixture assumption broke"
else
    head -n "$(( copy_line + 1 ))" "$WORKDIR/full.sql" > "$WORKDIR/truncated.sql"
    truncated_rows="$(dbs_verify_dump_restores "$CONTAINER" postgres "$WORKDIR/truncated.sql" ot_selftest_scratch 2>/dev/null || echo "<replay failed>")"
    if [[ "$truncated_rows" == "2" ]]; then
        bad "a dump truncated mid-COPY still restored all 2 rows — the corruption did not land where intended"
    else
        ok "a dump truncated mid-COPY restores the WRONG row count ($truncated_rows, expected 2) — silent, not a raised error; this is why phase 15 checks content digests, never restore's exit code, under this fault"
    fi
    dbs_scratch_drop "$CONTAINER" postgres ot_selftest_scratch
fi

# ── Case 3: ROLLBACK_INJECT_FAULT=stale-oracle — comparing against the wrong
# snapshot must show a mismatch, not a coincidental match. ──────────────────
echo "3. stale-oracle: comparing against the wrong fingerprint reports FAIL"
# fp-before vs fp-damaged already differ (case 1) — that IS what "stale-oracle"
# looks like from inside dbs_diff_fingerprints: the restored state matches
# fp-before, but this check was pointed at fp-damaged (a stand-in for
# snapshots/after here) instead.
if TEST_REPORT_FILE=/dev/null dbs_diff_fingerprints "$WORKDIR/fp-damaged" "$WORKDIR/fp-before" "selftest" media_file >/dev/null 2>&1; then
    bad "diffing against the WRONG oracle still reported PASS"
else
    ok "diffing against the wrong oracle correctly reports FAIL, not a coincidental PASS"
fi

# ── Case 4: ROLLBACK_INJECT_FAULT=no-damage — fingerprinting the SAME
# unchanged state twice must be equal, so the phase 14 "damage precondition"
# assertion (as_assert_ne) would correctly fail when the damage step never ran. ─
echo "4. no-damage: fingerprinting unchanged data twice is stable (proves the precondition check is real)"
dbs_fingerprint "$CONTAINER" postgres opentranscribe "$WORKDIR/fp-repeat-a" >/dev/null 2>&1
dbs_fingerprint "$CONTAINER" postgres opentranscribe "$WORKDIR/fp-repeat-b" >/dev/null 2>&1
digest_a="$(cat "$WORKDIR/fp-repeat-a/media_file.digest")"
digest_b="$(cat "$WORKDIR/fp-repeat-b/media_file.digest")"
if [[ "$digest_a" == "$digest_b" ]]; then
    ok "an unchanged database fingerprints identically across two calls — 'damage never happened' is genuinely detectable as before==after, not a fluke"
else
    bad "the SAME unchanged database fingerprinted differently twice — dbs_fingerprint is non-deterministic"
fi

echo
echo "── $PASS passed, $FAIL failed ──"
[[ $FAIL -eq 0 ]]
