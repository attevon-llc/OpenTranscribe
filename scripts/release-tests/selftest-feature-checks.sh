#!/bin/bash
# Self-test for lib/feature-checks.sh: proves each v0.6.0 rehearsal check can FAIL (task #14).
#
# WHY THIS EXISTS
#
# The real checks only ever run inside a multi-hour rehearsal against a live stack. A check that
# passes against a broken product is worse than none, and nobody can tell by reading it. So this
# replaces the TRANSPORT (curl, docker exec psql) with a small in-memory model of a correct
# v0.6.0 backend, runs every check against it (must report zero FAIL: the positive control), and
# then breaks that model one defect at a time (each must report at least one FAIL: the must-fire
# control). It needs no Docker, no network and no stack, and runs in about a second.
#
# It does NOT prove the real backend behaves; it proves the checks notice when it does not. The
# decision predicates have their own good/bad cases in
# backend/tests/unit/test_release_feature_checks.py.
#
# Usage: ./scripts/release-tests/selftest-feature-checks.sh
# Exit:  0 every case behaved - 1 a check failed to fire (or fired on a correct backend)

# as_pass / as_fail are the counters lib/assertions.sh maintains; they are sourced, so shellcheck
# cannot see the assignment.
# shellcheck disable=SC2154
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
export REPO_ROOT
TMP="$(mktemp -d -t ot-fc-selftest.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
export TEST_REPORT_FILE="$TMP/REPORT.md"

# shellcheck source=lib/assertions.sh
source "$SCRIPT_DIR/lib/assertions.sh"
# shellcheck source=lib/feature-checks.sh
source "$SCRIPT_DIR/lib/feature-checks.sh"

FILE=11111111-1111-4111-8111-111111111111
SPK=22222222-2222-4222-8222-222222222222
# Consumed by the sourced lib (and by the transport mocks below), hence exported.
export API_BASE="http://selftest.invalid/api" FC_PP_TIMEOUT_S=5 FC_PURGE_TIMEOUT_S=10 FC_CODE=000

# ─── the model: a correct backend, one defect switch (BREAK) at a time ──────────────────────
BREAK=""
W_VP=true; W_GENDER=true; W_SEEDED=0; W_PURGED=0; W_DOCFMT=docx

sleep() { :; }
ver_alembic_head() { echo "v449_add_speaker_identity_source"; }

_shown() {
    [[ "$W_SEEDED" == "1" && "$W_PURGED" == "0" && ( "$W_VP" == "true" || "$BREAK" == "no-hide" ) ]]
}

fc_request() {
    local out="$1" method="$2" path="$3" body="${4:-}" code=200 resp='{}'
    case "$method $path" in
        "GET /admin/privacy-policy" | "PUT /admin/privacy-policy")
            if [[ "$method" == "PUT" ]]; then
                if [[ "$body" == "{}" || "$body" == *'"no"'* ]]; then
                    code=422
                elif [[ "$BREAK" != "put-noop" ]]; then
                    case "$body" in
                        *gender_inference_enabled*true*) W_GENDER=true ;;
                        *gender_inference_enabled*false*) W_GENDER=false ;;
                    esac
                    case "$body" in
                        *voiceprint_matching_enabled*true*) W_VP=true ;;
                        *voiceprint_matching_enabled*false*) W_VP=false ;;
                    esac
                fi
            fi
            resp="{\"voiceprint_matching_enabled\": $W_VP, \"gender_inference_enabled\": $W_GENDER}" ;;
        "GET /privacy/effective"*)
            local org=null
            [[ "$BREAK" == "effective" ]] && org='"33333333-3333-4333-8333-333333333333"'
            resp="{\"organization_uuid\": $org, \"voiceprint_matching\": {\"enabled\": true, \"locked_by\": null}, \"gender_inference\": {\"enabled\": true, \"locked_by\": null}}" ;;
        "GET /files/$FILE")
            if _shown; then
                resp="{\"speakers\": [{\"uuid\": \"$SPK\", \"display_name\": \"$FC_VOICE_NAME\", \"identity\": {\"source\": \"voiceprint\"}}]}"
            else
                resp="{\"speakers\": [{\"uuid\": \"$SPK\", \"display_name\": null, \"identity\": null}]}"
            fi ;;
        "POST /admin/privacy-policy/purge")
            if [[ "$W_VP" == "true" && "$BREAK" != "purge-allowed-while-on" ]]; then
                code=409; resp='{"detail": {"code": "policy_still_enabled", "targets": ["voiceprints"]}}'
            else
                code=202; resp='{"task_id": "t1"}'
                [[ "$BREAK" == "no-revert" ]] || W_PURGED=1
            fi ;;
        "GET /privacy/purge/t1")
            local ok=true
            [[ "$BREAK" == "purge-residual" ]] && ok=false
            resp="{\"task_id\": \"t1\", \"status\": \"completed\", \"counts\": {\"postgres\": {\"speaker_identities_reverted\": 1}, \"opensearch\": {}}, \"residual\": {}, \"residual_ok\": $ok, \"residual_unverified\": [], \"audit\": [{\"event_type\": \"privacy.voiceprints.purge\", \"task_id\": \"t1\"}]}" ;;
        "GET /admin/audit-logs"*)
            local total=1
            [[ "$BREAK" == "audit-none" ]] && total=0
            resp="{\"logs\": [], \"total\": $total}" ;;
        "GET /files/$FILE/export?format="*)
            local fmt="${path##*=}"
            case "$fmt" in
                txt) printf 'hello world\n' > "$out" ;;
                json) printf '{"segments": []}' > "$out" ;;
                csv) printf 'Start Time,End Time,Speaker,Text\n0,1,A,hi\n' > "$out" ;;
                srt) printf '1\n00:00:00,000 --> 00:00:01,000\nhi\n' > "$out" ;;
                vtt) printf 'WEBVTT\n\n00:00.000 --> 00:01.000\nhi\n' > "$out" ;;
                md) if [[ "$BREAK" == "export-md" ]]; then printf 'no heading here\n' > "$out"; else printf '# Title\n\nhi\n' > "$out"; fi ;;
            esac
            FC_CODE=200; return 0 ;;
        "POST /files/$FILE/export/document")
            case "$body" in *pdf*) W_DOCFMT=pdf ;; *) W_DOCFMT=docx ;; esac
            code=202; resp='{"status": "processing", "job_id": "abc"}' ;;
        "GET /files/$FILE/post-processing")
            local attrs='{"state": "skipped", "at": 10.0}' search='{"state": "done", "at": 9.0, "started_at": 8.0}'
            [[ "$BREAK" == "pp-gender" ]] && attrs='{"state": "done", "at": 3000.0, "started_at": 2000.0}'
            [[ "$BREAK" == "pp-running" ]] && search='{"state": "started", "at": 9.0, "started_at": 8.0}'
            resp="{\"file_id\": \"$FILE\", \"run_at\": 7.0, \"tasks\": {\"search_indexing\": $search, \"speaker_attributes\": $attrs}}" ;;
        "GET /system/stats")
            if [[ "$BREAK" == "stats" ]]; then
                resp='{"system": {"version": "x"}}'
            else
                resp='{"system": {"chat_reranker_threads": {"threads": 4, "source": "auto", "setting": "auto"}}}'
            fi ;;
        "GET /org-admin/"*)
            code=404; [[ "$BREAK" == "org" ]] && code=200 ;;
        "POST /asr-settings/test")
            if [[ "$BREAK" == "aws" ]]; then
                resp='{"success": false, "message": "connection refused"}'
            else
                resp='{"success": false, "message": "AWS_TRANSCRIBE_BUCKET is not set. Create a private bucket"}'
            fi ;;
        *) code=404 ;;
    esac
    [[ "$path" == "/files/$FILE/export?format="* ]] || printf '%s' "$resp" > "$out"
    FC_CODE="$code"
}

fc_stream() {
    if [[ "$BREAK" == "sse-error" ]]; then
        printf 'event: error\ndata: {"code": "render_failed"}\n\n' > "$1"
    else
        printf 'event: progress\ndata: {}\n\nevent: ready\ndata: {"url": "http://minio.invalid/doc"}\n\n' > "$1"
    fi
    FC_CODE=200
}

fc_fetch_url() {
    if [[ "$W_DOCFMT" == "pdf" ]]; then
        if [[ "$BREAK" == "pdf-bad" ]]; then printf '<html>denied</html>' > "$1"; else printf '%%PDF-1.7 body' > "$1"; fi
    else
        printf 'PK\003\004 body' > "$1"
    fi
    FC_CODE=200
}

fc_sql_strict() {
    case "$1" in
        *"FROM alembic_version"*) if [[ "$BREAK" == "head" ]]; then echo v300_old; else echo v449_add_speaker_identity_source; fi ;;
        *"information_schema.columns"*)
            printf 'identity_source\nidentity_source_at\nidentity_source_user_id\nidentity_source_detail\n'
            [[ "$BREAK" == "cols" ]] || echo suggestion_detail ;;
        *"identity_source_backfill"*)
            [[ "$BREAK" == "backfill" ]] || echo '{"revision": "v449", "stamped": 3, "shape_left_unmarked": 0, "recorded_at": "2026-10-28"}' ;;
        *"SELECT f.uuid::text"*) echo "$FILE" ;;
        *"quote_nullable(display_name)"*)
            echo "$SPK|UPDATE speaker SET display_name=NULL, verified=false, identity_source=NULL, identity_source_at=NULL::timestamptz WHERE uuid='$SPK'" ;;
        "UPDATE speaker SET display_name='$FC_VOICE_NAME'"*) W_SEEDED=1; W_PURGED=0 ;;
        "UPDATE speaker SET display_name=NULL"*) W_SEEDED=0 ;;
        "SELECT identity_source FROM speaker"*) [[ "$W_SEEDED" == "1" ]] && echo voiceprint ;;
        *"coalesce(identity_source, '<null>')"*)
            if [[ "$W_PURGED" == "1" ]]; then echo '<null>|<null>'; else echo "voiceprint|$FC_VOICE_NAME"; fi ;;
        *"count(attributes_predicted_at)"*) echo '2|2|1000.5' ;;
        *) return 1 ;;
    esac
}

# ─── harness ────────────────────────────────────────────────────────────────────────────────
BAD=0
reset_world() { BREAK="${1:-}"; W_VP=true; W_GENDER=true; W_SEEDED=0; W_PURGED=0; W_DOCFMT=docx; }

# expect_clean CHECK [ARGS]: a correct backend produces no FAIL and at least one PASS.
expect_clean() {
    reset_world ""
    local f0=$as_fail p0=$as_pass
    "$@" >/dev/null
    if (( as_fail == f0 && as_pass > p0 )); then
        echo -e "  \033[0;32mPASS\033[0m  control: $1 is clean on a correct backend ($((as_pass - p0)) assertions)"
    else
        echo -e "  \033[0;31mFAIL\033[0m  control: $1 on a correct backend: $((as_fail - f0)) FAIL, $((as_pass - p0)) PASS"
        BAD=$((BAD + 1))
    fi
}

# expect_fires DEFECT CHECK [ARGS]: with one defect injected the check reports a FAIL.
expect_fires() {
    local defect="$1"
    shift
    reset_world "$defect"
    local f0=$as_fail
    "$@" >/dev/null
    if (( as_fail > f0 )); then
        echo -e "  \033[0;32mPASS\033[0m  must-fire: defect '$defect' is caught by $1"
    else
        echo -e "  \033[0;31mFAIL\033[0m  must-fire: defect '$defect' went UNNOTICED by $1"
        BAD=$((BAD + 1))
    fi
}

echo "Release feature-checks self-test (in-memory backend model)"
echo

printf '1|Alice\n2|<null>\n' > "$TMP/pre.txt"
cp "$TMP/pre.txt" "$TMP/post_ok.txt"
printf '1|Bob\n2|<null>\n' > "$TMP/post_changed.txt"
printf '1|Alice\n' > "$TMP/post_lost.txt"

expect_clean fc_check_schema_and_head
expect_fires head fc_check_schema_and_head
expect_fires cols fc_check_schema_and_head

expect_clean fc_check_upgrade_backfill "$TMP/pre.txt" "$TMP/post_ok.txt"
expect_fires backfill fc_check_upgrade_backfill "$TMP/pre.txt" "$TMP/post_ok.txt"
expect_fires names-changed fc_check_upgrade_backfill "$TMP/pre.txt" "$TMP/post_changed.txt"
expect_fires names-lost fc_check_upgrade_backfill "$TMP/pre.txt" "$TMP/post_lost.txt"

expect_clean fc_check_privacy_api
expect_fires put-noop fc_check_privacy_api
expect_fires effective fc_check_privacy_api

expect_clean fc_check_voiceprints_toggle
expect_fires no-hide fc_check_voiceprints_toggle

expect_clean fc_check_audited_purge
expect_fires purge-allowed-while-on fc_check_audited_purge
expect_fires purge-residual fc_check_audited_purge
expect_fires audit-none fc_check_audited_purge
expect_fires no-revert fc_check_audited_purge

expect_clean fc_check_exports
expect_fires export-md fc_check_exports
expect_fires pdf-bad fc_check_exports
expect_fires sse-error fc_check_exports

expect_clean fc_check_post_processing
expect_fires pp-running fc_check_post_processing
expect_fires pp-gender fc_check_post_processing

expect_clean fc_check_system_stats
expect_fires stats fc_check_system_stats

expect_clean fc_check_org_endpoints_404
expect_fires org fc_check_org_endpoints_404

expect_clean fc_check_aws_bucket_message
expect_fires aws fc_check_aws_bucket_message

# The runner must reach every check in its plan, in every mode, with the model healthy.
for mode in full upgrade lite; do
    reset_world ""
    f0=$as_fail
    fc_run_all "$mode" "$TMP/pre.txt" "$TMP/post_ok.txt" >/dev/null
    if (( as_fail == f0 )); then
        echo -e "  \033[0;32mPASS\033[0m  fc_run_all $mode is clean on a correct backend"
    else
        echo -e "  \033[0;31mFAIL\033[0m  fc_run_all $mode reported $((as_fail - f0)) FAIL on a correct backend"
        BAD=$((BAD + 1))
    fi
done

echo
if (( BAD == 0 )); then
    echo -e "\033[0;32mfeature-checks self-test: every control behaved\033[0m"
    exit 0
fi
echo -e "\033[0;31mfeature-checks self-test: $BAD case(s) misbehaved\033[0m"
exit 1
