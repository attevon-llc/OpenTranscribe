#!/bin/bash
# v0.6.0 feature checks for the release rehearsal (task #14 of the v0.6.0 release).
#
# Sourced by test-fresh-install.sh, test-upgrade.sh and test-lite-mode.sh AFTER api-client.sh,
# assertions.sh and versions.sh. Each scenario calls `fc_run_all <mode>` from its own phase.
#
# Split of labour: this file owns transport (curl, `docker exec psql`) and recording
# (as_record); lib/feature-checks.py owns every pass/fail DECISION about a response, so each
# decision is exercised with a good and a bad input (selftest-feature-checks.sh,
# backend/tests/unit/test_release_feature_checks.py).
#
# Rules this file follows because the harness runs under `set -euo pipefail` (scripts/CLAUDE.md,
# issues #617/#618): every check function returns 0 and RECORDS its verdict; every probe is wrapped
# so a failure is a recorded FAIL, never a bare non-zero that truncates the phases after it; no
# `| grep -q` / `| head` in a pipeline (SIGPIPE inversion).
#
# Needs: API_BASE, API_TOKEN (ac_login), REPO_ROOT, the stock `opentranscribe-postgres` container.
# Prints no secret: the bearer token is only ever an argument to curl, never echoed.

set -euo pipefail

FC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FC_PY="$FC_DIR/feature-checks.py"
: "${FC_PG_CONTAINER:=opentranscribe-postgres}"
: "${FC_PP_TIMEOUT_S:=300}"
#: Per-request cap inside a polling loop. A poll is bounded by its OWN deadline (the *_TIMEOUT_S
#: values), so one hung request must not stretch it: 60 iterations x a 60 s request was ~65 min.
: "${FC_POLL_REQUEST_TIMEOUT_S:=15}"
: "${FC_PURGE_TIMEOUT_S:=420}"
: "${FC_EXPORT_TIMEOUT_S:=240}"
#: The revision that introduced the v0.6.0 speaker columns. A FLOOR for the derived head, never the
#: expected head itself (that is always derived from the migration graph).
FC_MIN_ALEMBIC_REV=449
FC_VOICE_NAME="Rehearsal Voice Name"

#: Check lists. `fc_run_all` runs exactly these; backend/tests/unit/test_release_feature_checks.py
#: pins that every name is a defined function and that the docs list the same set.
FC_CHECKS_COMMON=(
    fc_check_schema_and_head
    fc_check_privacy_api
    fc_check_system_stats
    fc_check_org_endpoints_404
    fc_check_aws_bucket_message
    fc_check_post_processing
    fc_check_exports
)
FC_CHECKS_UPGRADE=(fc_check_upgrade_backfill)
#: Mutating, ordered last. The purge is irreversible by design (it deletes voiceprint data).
FC_CHECKS_STATEFUL=(fc_check_voiceprints_toggle fc_check_audited_purge)

# ─── transport (replaced wholesale by selftest-feature-checks.sh) ───────────────────────────

# fc_request OUTFILE METHOD PATH [JSON_BODY] -- sets FC_CODE (000 when curl itself failed).
# A global, not stdout, so a caller never needs a subshell. Always returns 0.
fc_request() {
    local out="$1" method="$2" path="$3" body="${4:-}"
    local args=(--silent --show-error --max-time "${FC_REQUEST_TIMEOUT_S:-60}"
                --connect-timeout "${API_TIMEOUT:-10}" -o "$out" -w '%{http_code}' -X "$method")
    if [[ -n "${API_TOKEN:-}" ]]; then
        args+=(-H "Authorization: Bearer $API_TOKEN")
    fi
    if [[ -n "$body" ]]; then
        args+=(-H "Content-Type: application/json" --data "$body")
    fi
    FC_CODE="$(curl "${args[@]}" "$API_BASE$path" 2>/dev/null)" || true
    FC_CODE="${FC_CODE:-000}"
    return 0
}

# fc_stream OUTFILE PATH -- an SSE GET that ends when the server closes it (or on timeout).
fc_stream() {
    local out="$1" path="$2"
    local args=(--silent --show-error -N --max-time "$FC_EXPORT_TIMEOUT_S"
                --connect-timeout "${API_TIMEOUT:-10}" -o "$out" -w '%{http_code}')
    if [[ -n "${API_TOKEN:-}" ]]; then
        args+=(-H "Authorization: Bearer $API_TOKEN")
    fi
    FC_CODE="$(curl "${args[@]}" "$API_BASE$path" 2>/dev/null)" || true
    FC_CODE="${FC_CODE:-000}"
    return 0
}

# fc_fetch_url OUTFILE URL -- a presigned download. Deliberately sends NO bearer token.
fc_fetch_url() {
    FC_CODE="$(curl --silent --show-error --max-time 60 -o "$1" -w '%{http_code}' "$2" 2>/dev/null)" || true
    FC_CODE="${FC_CODE:-000}"
    return 0
}

# fc_sql_strict QUERY -- rows as `a|b`; non-zero when the query fails.
fc_sql_strict() {
    docker exec "$FC_PG_CONTAINER" psql -U postgres -d opentranscribe -tA -F'|' \
        -v ON_ERROR_STOP=1 -c "$1" 2>/dev/null
}
# fc_sql QUERY -- the same, but an unreadable database reads as no rows (the caller's predicate
# then FAILs on the empty value instead of the phase aborting).
fc_sql() { fc_sql_strict "$1" || true; }
# fc_sql_exec STATEMENT -- a write; returns 0 whatever happens (callers verify by reading back).
fc_sql_exec() { fc_sql_strict "$1" >/dev/null || true; }

# ─── recording ──────────────────────────────────────────────────────────────────────────────

FC_CODE="000"
FC_BODY=""
FC_DETAIL=""

# fc_call METHOD PATH [BODY] -- request into FC_BODY / FC_CODE.
fc_call() {
    local tmp
    tmp="$(mktemp)"
    fc_request "$tmp" "$@"
    FC_BODY="$(<"$tmp")"
    rm -f "$tmp"
}

# fc_need LABEL EXPECTED_CODE -- records a FAIL and returns 1 unless FC_CODE matches.
fc_need() {
    if [[ "$FC_CODE" == "$2" ]]; then
        return 0
    fi
    as_record FAIL "$1" "HTTP $FC_CODE (expected $2)"
    return 1
}

# fc_eval PREDICATE [ARGS...] -- run a feature-checks.py predicate on FC_BODY; sets FC_DETAIL.
fc_eval() {
    local rc=0
    FC_DETAIL="$(python3 -I "$FC_PY" "$@" <<<"$FC_BODY")" || rc=$?
    return "$rc"
}

# Exit codes of feature-checks.py: 0 pass, 1 fail, 2 not applicable (the only SKIP). Anything
# else -- an unknown predicate (64), a crash -- is a FAIL, never a quiet skip.
fc_record() {
    case "$2" in
        0) as_record PASS "$1" ;;
        2) as_record SKIP "$1" "$FC_DETAIL" ;;
        *) as_record FAIL "$1" "${FC_DETAIL:-feature-checks.py exited $2}" ;;
    esac
}

# fc_judge LABEL PREDICATE [ARGS...] -- evaluate and record in one step.
fc_judge() {
    local label="$1" rc=0
    shift
    fc_eval "$@" || rc=$?
    fc_record "$label" "$rc"
}

# fc_field KEY -- top-level key of FC_BODY (booleans lower-cased); empty when absent.
fc_field() {
    python3 -I -c '
import json, sys
try:
    v = json.loads(sys.argv[1])[sys.argv[2]]
except Exception:
    v = ""
print(str(v).lower() if isinstance(v, bool) else v)
' "$FC_BODY" "$1"
}

fc_is_uuid() { [[ "$1" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; }

# fc_pick_file -- uuid of the newest completed file that has speakers; empty when none.
fc_pick_file() {
    local u
    u="$(fc_sql "SELECT f.uuid::text FROM media_file f WHERE lower(f.status::text) = 'completed' AND EXISTS (SELECT 1 FROM speaker s WHERE s.media_file_id = f.id) ORDER BY f.id DESC LIMIT 1")"
    u="${u//[$'\r\n ']/}"
    if fc_is_uuid "$u"; then
        echo "$u"
    fi
}

# fc_snapshot_speaker_names OUTFILE -- `id|display_name` per speaker, for the upgrade comparison.
# Written as a parenthesised marker when the column cannot be read (an older FROM schema).
fc_snapshot_speaker_names() {
    local rows
    if rows="$(fc_sql_strict "SELECT id::text || '|' || coalesce(replace(display_name, E'\n', ' '), '<null>') FROM speaker ORDER BY id")"; then
        printf '%s\n' "$rows" > "$1"
    else
        echo "(speaker.display_name unreadable on this schema)" > "$1"
    fi
}

# ─── (1) schema ─────────────────────────────────────────────────────────────────────────────
fc_check_schema_and_head() {
    local db_head derived cols
    db_head="$(fc_sql "SELECT version_num FROM alembic_version")"
    derived="$(ver_alembic_head "$REPO_ROOT/backend" 2>/dev/null || true)"
    fc_judge "alembic: database is at the graph's derived head, which is >= v${FC_MIN_ALEMBIC_REV}" \
        head "$db_head" "$derived" "$FC_MIN_ALEMBIC_REV"
    cols="$(fc_sql "SELECT column_name FROM information_schema.columns WHERE table_name = 'speaker' AND column_name IN ('identity_source','identity_source_at','identity_source_user_id','identity_source_detail','suggestion_detail')")"
    FC_BODY="$cols"
    fc_judge "schema: speaker has identity_source{,_at,_user_id,_detail} and suggestion_detail" columns
}

# ─── (2) upgrade only ───────────────────────────────────────────────────────────────────────
# fc_check_upgrade_backfill PRE_NAMES_FILE POST_NAMES_FILE (fc_snapshot_speaker_names output)
fc_check_upgrade_backfill() {
    FC_BODY="$(fc_sql "SELECT value FROM system_settings WHERE key = 'privacy.identity_source_backfill'")"
    fc_judge "upgrade: system_settings privacy.identity_source_backfill recorded by v449" backfill-record
    FC_BODY=""
    fc_judge "upgrade: speakers that existed before kept their display_name" \
        display-names "${1:-/nonexistent}" "${2:-/nonexistent}"
}

# ─── (3) privacy API ────────────────────────────────────────────────────────────────────────
fc_check_privacy_api() {
    local orig flipped f
    fc_call GET /admin/privacy-policy
    if fc_need "privacy: GET /admin/privacy-policy answers 200" 200; then
        fc_judge "privacy: GET /admin/privacy-policy shape (two booleans)" privacy-policy
        orig="$(fc_field gender_inference_enabled)"
        if [[ "$orig" == "true" ]]; then flipped=false; else flipped=true; fi
        # Flip, read back, restore: restore is unconditional so a failed assertion cannot leave the
        # switch changed for the checks that follow.
        fc_call PUT /admin/privacy-policy "{\"gender_inference_enabled\": $flipped}"
        if fc_need "privacy: PUT /admin/privacy-policy answers 200" 200; then
            fc_judge "privacy: PUT response carries the new gender switch" privacy-policy "" "$flipped"
        fi
        fc_call GET /admin/privacy-policy
        fc_judge "privacy: the PUT persisted (GET round-trip)" privacy-policy "" "$flipped"
        fc_call PUT /admin/privacy-policy "{\"gender_inference_enabled\": ${orig:-true}}"
        fc_call GET /admin/privacy-policy
        fc_judge "privacy: gender switch restored to its original value" privacy-policy "" "${orig:-true}"
        fc_call PUT /admin/privacy-policy '{"gender_inference_enabled": "no"}'
        as_assert_http "privacy: PUT refuses a non-boolean switch (no coercion)" 422 "$FC_CODE"
        fc_call PUT /admin/privacy-policy '{}'
        as_assert_http "privacy: PUT refuses an empty update" 422 "$FC_CODE"
    fi
    fc_call GET /privacy/effective
    if fc_need "privacy: GET /privacy/effective answers 200" 200; then
        fc_judge "privacy: GET /privacy/effective shape (community: no organization)" effective
    fi
    f="$(fc_pick_file)"
    if [[ -n "$f" ]]; then
        fc_call GET "/privacy/effective?file_uuid=$f"
        if fc_need "privacy: GET /privacy/effective?file_uuid answers 200" 200; then
            fc_judge "privacy: per-file effective policy shape" effective
        fi
    else
        as_record SKIP "privacy: per-file effective policy" "no completed file with speakers"
    fi
}

# ─── (4) voiceprints off hides a voice-made name, on restores it ────────────────────────────
FC_SPK_UUID=""
FC_RESTORE_SQL=""

# fc_seed_voice_identity FILE_UUID -- make one speaker's name look voiceprint-made; remembers how to
# put the row back. Returns 1 when it could not be seeded and verified.
fc_seed_voice_identity() {
    local row src
    row="$(fc_sql "SELECT uuid::text || '|' || 'UPDATE speaker SET display_name=' || quote_nullable(display_name) || ', verified=' || coalesce(verified, false)::text || ', identity_source=' || quote_nullable(identity_source) || ', identity_source_at=' || quote_nullable(identity_source_at::text) || '::timestamptz WHERE uuid=' || quote_literal(uuid::text) FROM speaker WHERE media_file_id = (SELECT id FROM media_file WHERE uuid = '$1') ORDER BY id LIMIT 1")"
    FC_SPK_UUID="${row%%|*}"
    FC_RESTORE_SQL="${row#*|}"
    fc_is_uuid "$FC_SPK_UUID" || return 1
    fc_sql_exec "UPDATE speaker SET display_name='$FC_VOICE_NAME', verified=true, identity_source='voiceprint', identity_source_at=now() WHERE uuid='$FC_SPK_UUID'"
    src="$(fc_sql "SELECT identity_source FROM speaker WHERE uuid = '$FC_SPK_UUID'")"
    [[ "$src" == "voiceprint" ]]
}

# fc_set_voiceprints true|false -- the instance switch. Returns 1 unless the PUT answered 200.
fc_set_voiceprints() {
    fc_call PUT /admin/privacy-policy "{\"voiceprint_matching_enabled\": $1}"
    [[ "$FC_CODE" == "200" ]]
}

# fc_poll_speaker LABEL FILE_UUID MODE -- file detail shows (visible) / withholds (hidden) the name.
fc_poll_speaker() {
    local label="$1" f="$2" mode="$3" rc=1 deadline _try FC_REQUEST_TIMEOUT_S="$FC_POLL_REQUEST_TIMEOUT_S"
    deadline=$((SECONDS + 40))
    # Bounded twice: by attempts (the self-test stubs `sleep` away) and by wall clock.
    for _try in $(seq 1 15); do
        fc_call GET "/files/$f"
        if [[ "$FC_CODE" == "200" ]]; then
            rc=0
            fc_eval speaker-name "$FC_SPK_UUID" "$FC_VOICE_NAME" "$mode" || rc=$?
            [[ "$rc" == "0" ]] && break
        else
            FC_DETAIL="GET /files/<uuid> answered HTTP $FC_CODE"
            rc=1
        fi
        ((SECONDS < deadline)) || break
        sleep 2
    done
    fc_record "$label" "$rc"
}

fc_check_voiceprints_toggle() {
    local f orig
    f="$(fc_pick_file)"
    if [[ -z "$f" ]]; then
        as_record FAIL "voiceprints: a completed file with speakers to seed" "none found"
        return 0
    fi
    fc_call GET /admin/privacy-policy
    orig="$(fc_field voiceprint_matching_enabled)"
    if ! fc_seed_voice_identity "$f"; then
        as_record FAIL "voiceprints: seeding a voice-made speaker name" "the speaker row could not be updated and read back"
        return 0
    fi
    if fc_set_voiceprints true; then
        fc_poll_speaker "voiceprints on: the voice-made name and its provenance are shown (seed control)" "$f" visible
    else
        as_record FAIL "voiceprints: switch on" "PUT /admin/privacy-policy answered HTTP $FC_CODE"
    fi
    if fc_set_voiceprints false; then
        fc_poll_speaker "voiceprints off: the voice-made name and its provenance are hidden" "$f" hidden
    else
        as_record FAIL "voiceprints: switch off" "PUT /admin/privacy-policy answered HTTP $FC_CODE"
    fi
    if fc_set_voiceprints true; then
        fc_poll_speaker "voiceprints back on: the hidden name is restored (hide, not delete)" "$f" visible
    else
        as_record FAIL "voiceprints: switch back on" "PUT /admin/privacy-policy answered HTTP $FC_CODE"
    fi
    fc_set_voiceprints "${orig:-true}" || true
    fc_sql_exec "$FC_RESTORE_SQL"
}

# ─── (5) the audited purge ──────────────────────────────────────────────────────────────────
fc_check_audited_purge() {
    local f orig task deadline _try rc=0 FC_REQUEST_TIMEOUT_S="$FC_POLL_REQUEST_TIMEOUT_S" body_purge='{"scope":"personal","targets":["voiceprints"],"confirm":"PURGE"}'
    f="$(fc_pick_file)"
    if [[ -z "$f" ]]; then
        as_record FAIL "purge: a completed file with speakers to seed" "none found"
        return 0
    fi
    fc_call GET /admin/privacy-policy
    orig="$(fc_field voiceprint_matching_enabled)"
    if ! fc_seed_voice_identity "$f"; then
        as_record FAIL "purge: seeding a voice-made speaker name" "the speaker row could not be updated and read back"
        return 0
    fi
    fc_set_voiceprints true || true
    fc_call POST /admin/privacy-policy/purge "$body_purge"
    as_assert_http "purge: refused with 409 while voiceprints are still switched on" 409 "$FC_CODE"
    fc_judge "purge: the 409 names policy_still_enabled" policy-still-enabled

    if ! fc_set_voiceprints false; then
        as_record FAIL "purge: switch voiceprints off first" "PUT answered HTTP $FC_CODE"
        fc_sql_exec "$FC_RESTORE_SQL"
        return 0
    fi
    fc_call POST /admin/privacy-policy/purge "$body_purge"
    if fc_need "purge: POST /admin/privacy-policy/purge (personal scope) is accepted" 202; then
        task="$(fc_field task_id)"
        deadline=$((SECONDS + FC_PURGE_TIMEOUT_S))
        for _try in $(seq 1 $(( FC_PURGE_TIMEOUT_S / 10 ))); do
            fc_call GET "/privacy/purge/$task"
            rc=0
            fc_eval purge-terminal || rc=$?
            [[ "$rc" == "0" ]] && break
            ((SECONDS < deadline)) || break
            sleep 10
        done
        if fc_need "purge: GET /privacy/purge/{task} answers 200" 200; then
            fc_judge "purge: completed with per-store counts, residual_ok and an audit reference" \
                purge-status 1
        fi
        # Only PASS or the documented "audit off" SKIP end the poll; a FAIL (no event yet, or the
        # store answering with an internal error) is retried until the deadline.
        deadline=$((SECONDS + 60))
        for _try in $(seq 1 12); do
            fc_call GET "/admin/audit-logs?event_type=privacy.voiceprints.purge&limit=5"
            rc=0
            fc_eval audit-search || rc=$?
            [[ "$rc" == "0" || "$rc" == "2" ]] && break
            ((SECONDS < deadline)) || break
            sleep 5
        done
        if [[ "$FC_CODE" == "200" ]]; then
            fc_record "purge: an audit event for it is searchable in the audit store" "$rc"
        else
            as_record FAIL "purge: GET /admin/audit-logs answers 200" "HTTP $FC_CODE"
        fi
        as_assert_eq "purge: the voice-made name was reverted in Postgres" "<null>|<null>" \
            "$(fc_sql "SELECT coalesce(identity_source, '<null>') || '|' || coalesce(display_name, '<null>') FROM speaker WHERE uuid = '$FC_SPK_UUID'")"
    fi
    fc_set_voiceprints "${orig:-true}" || true
}

# ─── (6) exports ────────────────────────────────────────────────────────────────────────────
fc_check_exports() {
    local f dir fmt job _try url
    f="$(fc_pick_file)"
    if [[ -z "$f" ]]; then
        as_record FAIL "export: a completed file to export" "none found"
        return 0
    fi
    dir="$(mktemp -d)"
    for fmt in txt json csv srt vtt md; do
        # 409 means redaction detection has not produced spans yet; it clears on its own.
        for _try in 1 2 3 4 5 6 7 8 9 10 11 12; do
            fc_request "$dir/export.$fmt" GET "/files/$f/export?format=$fmt"
            [[ "$FC_CODE" != "409" ]] && break
            sleep 5
        done
        if [[ "$FC_CODE" != "200" ]]; then
            as_record FAIL "export: GET /files/{uuid}/export?format=$fmt" "HTTP $FC_CODE"
            continue
        fi
        FC_BODY=""
        fc_judge "export: $fmt is a real $fmt file" export-file "$fmt" "$dir/export.$fmt"
    done
    for fmt in docx pdf; do
        for _try in 1 2 3 4 5 6; do
            fc_call POST "/files/$f/export/document" "{\"format\": \"$fmt\"}"
            [[ "$FC_CODE" != "409" ]] && break
            sleep 5
        done
        if [[ "$FC_CODE" != "202" ]]; then
            as_record FAIL "export: POST /files/{uuid}/export/document ($fmt)" "HTTP $FC_CODE"
            continue
        fi
        job="$(fc_field job_id)"
        fc_stream "$dir/stream.$fmt" "/files/bulk-export-stream?job=$job"
        FC_BODY="$(<"$dir/stream.$fmt")"
        if ! fc_eval sse-ready; then
            as_record FAIL "export: $fmt job reached ready" "$FC_DETAIL"
            continue
        fi
        url="$FC_DETAIL"
        fc_fetch_url "$dir/doc.$fmt" "$url"
        if [[ "$FC_CODE" != "200" ]]; then
            as_record FAIL "export: download of the $fmt document" "HTTP $FC_CODE from the presigned URL"
            continue
        fi
        FC_BODY=""
        fc_judge "export: $fmt document has the right magic bytes" export-file "$fmt" "$dir/doc.$fmt"
    done
    rm -rf "$dir"
}

# ─── (7) notification post-processing ───────────────────────────────────────────────────────
fc_check_post_processing() {
    local f deadline _try rc=1 counts speakers predicted maxp FC_REQUEST_TIMEOUT_S="$FC_POLL_REQUEST_TIMEOUT_S"
    f="$(fc_pick_file)"
    if [[ -z "$f" ]]; then
        as_record FAIL "notifications: a completed file to read post-processing for" "none found"
        return 0
    fi
    deadline=$((SECONDS + FC_PP_TIMEOUT_S))
    for _try in $(seq 1 $(( FC_PP_TIMEOUT_S / 5 ))); do
        fc_call GET "/files/$f/post-processing"
        rc=0
        fc_eval pp-settled || rc=$?
        [[ "$rc" == "0" ]] && break
        ((SECONDS < deadline)) || break
        sleep 5
    done
    if ! fc_need "notifications: GET /files/{uuid}/post-processing answers 200" 200; then
        return 0
    fi
    counts="$(fc_sql "SELECT count(*)::text || '|' || count(attributes_predicted_at)::text || '|' || coalesce(extract(epoch from max(attributes_predicted_at))::text, '') FROM speaker WHERE media_file_id = (SELECT id FROM media_file WHERE uuid = '$f')")"
    IFS='|' read -r speakers predicted maxp <<<"$counts"
    fc_judge "notifications: every post-processing stage is terminal; Gender row skipped when attributes pre-exist" \
        post-processing "${speakers:-0}" "${predicted:-0}" "${maxp:-}"
}

# ─── (8) system statistics ──────────────────────────────────────────────────────────────────
fc_check_system_stats() {
    fc_call GET /system/stats
    if fc_need "stats: GET /system/stats answers 200" 200; then
        fc_judge "stats: the response carries the passage-reranker row" stats-reranker
    fi
}

# ─── (9) community install has no organizations ─────────────────────────────────────────────
fc_check_org_endpoints_404() {
    local p
    # Only routes whose capability gate has NO platform-admin bypass: this scenario logs in as the
    # super admin, who would walk through `organizations` into a 403 and read as "not 404".
    for p in /org-admin/policy /org-admin/holds /org-admin/audit-logs/export; do
        fc_call GET "$p"
        as_assert_http "community: GET /api$p is 404 (organization capability off)" 404 "$FC_CODE"
    done
}

# ─── (10) AWS Transcribe without a bucket ───────────────────────────────────────────────────
fc_check_aws_bucket_message() {
    fc_call POST /asr-settings/test \
        '{"provider":"aws","model_name":"default","region":"us-east-1","access_key_id":"rehearsal-placeholder-key-id","api_key":"rehearsal-not-a-secret"}'
    if fc_need "aws asr: POST /asr-settings/test answers 200" 200; then
        fc_judge "aws asr: with no AWS_TRANSCRIBE_BUCKET the test fails and names the variable" aws-bucket
    fi
}

# ─── runner ─────────────────────────────────────────────────────────────────────────────────
# fc_run_all MODE [PRE_NAMES POST_NAMES]
#   MODE  full     fresh install: every check, including the mutating ones
#         upgrade  full + the upgrade-only checks (PRE_NAMES/POST_NAMES from fc_snapshot_speaker_names)
#         lite     the read-only checks only (the mutating pair needs a GPU-free voiceprint corpus
#                  this scenario does not build)
fc_run_all() {
    local mode="${1:-full}" check
    local plan=("${FC_CHECKS_COMMON[@]}")
    case "$mode" in
        full) plan+=("${FC_CHECKS_STATEFUL[@]}") ;;
        upgrade) plan+=("${FC_CHECKS_UPGRADE[@]}" "${FC_CHECKS_STATEFUL[@]}") ;;
        lite) ;;
        *) as_record FAIL "feature checks: unknown mode" "$mode"; return 0 ;;
    esac
    for check in "${plan[@]}"; do
        # `if`, never bare: a check is recorded-not-fatal by design (#617/#618).
        if [[ "$check" == "fc_check_upgrade_backfill" ]]; then
            if ! "$check" "${2:-}" "${3:-}"; then as_record FAIL "feature check $check aborted"; fi
        elif ! "$check"; then
            as_record FAIL "feature check $check aborted"
        fi
    done
}
