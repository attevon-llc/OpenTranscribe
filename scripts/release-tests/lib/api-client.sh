#!/bin/bash
# Thin curl wrappers around the OpenTranscribe REST API.
#
# All functions assume:
#   API_BASE              e.g. http://localhost:6174/api
#   API_TOKEN             JWT (set after ac_login)
#   API_TIMEOUT           connect timeout, default 10 s
#
# These helpers print structured progress to stderr and parsed JSON to stdout
# so they can be composed in `var=$(ac_func ...)` style.

set -euo pipefail

: "${API_TIMEOUT:=10}"

ac_log()  { echo -e "\033[0;34m[api]\033[0m $*" >&2; }
ac_warn() { echo -e "\033[1;33m[api]\033[0m $*" >&2; }
ac_die()  { echo -e "\033[0;31m[api] FATAL:\033[0m $*" >&2; exit 1; }

ac_curl() {
    # Wrapper that always sets sensible defaults and the auth header if present.
    local args=(--silent --show-error --fail-with-body --max-time 60 --connect-timeout "$API_TIMEOUT")
    if [[ -n "${API_TOKEN:-}" ]]; then
        args+=(-H "Authorization: Bearer $API_TOKEN")
    fi
    curl "${args[@]}" "$@"
}

ac_wait_for_health() {
    # Poll the backend /health endpoint until 200 or timeout (default 15 minutes).
    # /health lives at the server root (e.g. http://localhost:5174/health),
    # not under /api/, so we strip the /api suffix from API_BASE.
    local timeout="${1:-900}"
    local deadline=$(( $(date +%s) + timeout ))
    local health_url="${API_BASE%/api}/health"
    ac_log "waiting up to ${timeout}s for $health_url"
    while (( $(date +%s) < deadline )); do
        if curl -fsS --max-time 5 "$health_url" >/dev/null 2>&1; then
            ac_log "backend healthy"
            return 0
        fi
        sleep 5
    done
    ac_die "backend never reached healthy state within ${timeout}s"
}

ac_wait_for_frontend() {
    # Poll a frontend URL until it answers with ANY http status (not a
    # connection failure) or timeout (default 15 minutes, matching
    # ac_wait_for_health's ceiling). Nothing else in this harness waits for
    # the frontend container specifically before checking it (issue #618) —
    # unlike the backend, whose /health readiness is awaited via
    # ac_wait_for_health before any of its endpoints are probed.
    #
    # curl exits non-zero on a connection failure (e.g. exit 7, container not
    # listening yet) as opposed to just a non-2xx HTTP status, so this loop
    # treats "curl succeeded at all" as readiness and leaves judging the
    # actual status code to the caller's own assertion. Deliberately NOT
    # fatal on timeout (unlike ac_wait_for_health): the caller guards its own
    # curl/as_assert_http so an unready frontend is recorded as a FAIL, not a
    # script-ending crash.
    local url="$1"
    local timeout="${2:-900}"
    local deadline=$(( $(date +%s) + timeout ))
    ac_log "waiting up to ${timeout}s for $url to answer"
    while (( $(date +%s) < deadline )); do
        if curl -o /dev/null -s --max-time 5 "$url" 2>/dev/null; then
            ac_log "frontend reachable"
            return 0
        fi
        sleep 5
    done
    ac_warn "frontend never answered within ${timeout}s"
    return 1
}

ac_register_admin() {
    # First-user registration becomes super-admin. Idempotent: returns silently
    # if registration fails because the user already exists.
    local email="$1"
    local password="$2"
    local full_name="${3:-${TEST_ADMIN_FULL_NAME:-QA Bot}}"
    ac_log "registering $email"
    # UserCreate schema requires email + password; full_name is optional but
    # we send it for completeness. There is NO 'username' field — it was
    # rejected by pydantic 'extra=forbid' validation in earlier runs.
    local payload
    payload=$(printf '{"email":"%s","password":"%s","full_name":"%s"}' \
        "$email" "$password" "$full_name")
    local response code
    response=$(curl -sS -o /tmp/ac_register_resp.$$ -w '%{http_code}' \
        -X POST "$API_BASE/auth/register" \
        -H "Content-Type: application/json" \
        -d "$payload" 2>/dev/null || echo 000)
    code="$response"
    if [[ "$code" =~ ^2 ]]; then
        ac_log "register OK ($code)"
    elif [[ "$code" == "409" || "$code" == "400" ]]; then
        ac_warn "register returned $code — assuming user already exists"
    else
        ac_warn "register failed with HTTP $code:"
        cat /tmp/ac_register_resp.$$ >&2
        echo >&2
    fi
    rm -f /tmp/ac_register_resp.$$
}

ac_login() {
    local email="$1"
    local password="$2"
    ac_log "logging in $email"
    local body
    body=$(curl -fsS -X POST "$API_BASE/auth/login" \
        -H "Content-Type: application/x-www-form-urlencoded" \
        -d "username=${email}&password=${password}")
    API_TOKEN=$(echo "$body" | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')
    export API_TOKEN
    [[ -n "$API_TOKEN" ]] || ac_die "login failed: no access_token in response"
    ac_log "login OK (token len $(echo -n "$API_TOKEN" | wc -c))"
}

ac_upload_file() {
    # POST /api/files (multipart/form-data) — uploads a local media file
    # for transcription. Echoes the new media_file uuid.
    #
    # The backend validates content_type, so we set an explicit MIME type from
    # the file extension instead of relying on curl's autodetection (which often
    # uses application/octet-stream and gets rejected).
    local path="$1"
    [[ -f "$path" ]] || ac_die "file not found: $path"
    local mime
    case "${path##*.}" in
        mp3|MP3)        mime="audio/mpeg" ;;
        m4a|M4A)        mime="audio/mp4" ;;
        wav|WAV)        mime="audio/wav" ;;
        flac|FLAC)      mime="audio/flac" ;;
        ogg|oga|OGG)    mime="audio/ogg" ;;
        opus|OPUS)      mime="audio/opus" ;;
        mp4|MP4)        mime="video/mp4" ;;
        m4v|M4V)        mime="video/mp4" ;;
        mov|MOV)        mime="video/quicktime" ;;
        webm|WEBM)      mime="video/webm" ;;
        mkv|MKV)        mime="video/x-matroska" ;;
        *)              mime="application/octet-stream" ;;
    esac
    ac_log "uploading file: $path (type=$mime)"
    local body
    body=$(ac_curl -X POST "$API_BASE/files" \
        -F "file=@${path};type=${mime}")
    echo "$body" | python3 -c '
import sys, json
d = json.load(sys.stdin)
if isinstance(d, dict):
    for key in ("uuid", "file_uuid", "id"):
        if key in d:
            print(d[key]); sys.exit(0)
    if "file" in d and isinstance(d["file"], dict):
        for key in ("uuid", "file_uuid", "id"):
            if key in d["file"]:
                print(d["file"][key]); sys.exit(0)
sys.exit("could not extract file uuid: " + json.dumps(d)[:200])
'
}

# Backward-compat alias for any callers still using the old name
ac_upload_from_url() { ac_upload_file "$@"; }

# Dump everything needed to diagnose a failed pipeline run BEFORE the caller
# dies and the harness tears the stack down.
#
# This exists because it was missing. A rehearsal failed with nothing in the
# report but "file <uuid> ended in status=error"; the actual cause — an nltk
# pathsec violation on a hardlinked model cache — lived only in a database
# column and a worker log, both destroyed by the cleanup that followed. A
# failure you cannot diagnose after the fact costs a full re-run to learn
# anything at all, so the diagnosis is captured at the moment of failure.
ac_dump_failure_diagnostics() {
    local file_uuid="$1"

    ac_warn "──────── failure diagnostics for $file_uuid ────────"

    # 1. The API's own view of the record. Print every field whose name hints
    #    at an error rather than guessing one key: the column has been renamed
    #    before (error_message -> last_error_message).
    local body
    body=$(ac_curl "$API_BASE/files/$file_uuid" 2>/dev/null || echo "")
    if [[ -n "$body" ]]; then
        echo "$body" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception as exc:  # noqa: BLE001 - diagnostics must never mask the real failure
    print("  (could not parse file record: %s)" % exc); sys.exit(0)
if isinstance(d, dict) and "file" in d and isinstance(d["file"], dict):
    d = d["file"]
interesting = [k for k in d if any(t in k.lower() for t in ("error", "status", "message", "detail"))]
for k in sorted(interesting):
    v = d.get(k)
    if v not in (None, "", []):
        print("  %-26s %s" % (k, str(v)[:700]))
' 2>/dev/null || ac_warn "  (could not read file record)"
    else
        ac_warn "  (API did not return the file record)"
    fi

    # 2. Worker logs. The pipeline spans several queues, so tail each worker
    #    that exists rather than assuming which one owns the failing step.
    local c
    for c in opentranscribe-celery-worker opentranscribe-celery-cpu-worker \
             opentranscribe-celery-nlp-worker opentranscribe-backend; do
        if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$c"; then
            ac_warn "  ── last 40 log lines: $c"
            docker logs --tail 40 "$c" 2>&1 | sed 's/^/    /' || true
        fi
    done
    ac_warn "──────── end diagnostics ────────"
}

# ac_diar_engine_verdict [WORKER_CONTAINER] [SINCE]
#
# Answers "which engine actually diarized the file(s) that just completed?" —
# a question `ac_wait_for_file_status` returning "completed" cannot answer.
# The fallback from the native sidecar to in-process PyAnnote is SILENT BY
# DESIGN (diarizer_native.py / model_manager.py log one warning line and
# carry on), so a rehearsal that only checks the upload's final status can
# pass on a stack whose diarizer is dead (issue #670).
#
# diarizer_native.py logs exactly one of two mutually exclusive lines per
# job:
#   "native diarization done in %.1fs: %d segments, %d speakers"  (served)
#   "falling back to PyAnnote"  (degraded — three call sites log this exact
#   suffix: an unreachable sidecar, a mid-job /diarize failure, or the
#   sidecar simply being absent from this deployment)
#
# Echoes exactly one of: native | fallback | unknown | absent
#   native    a "native diarization done" line was seen and no fallback line
#   fallback  at least one "falling back to PyAnnote" line was seen
#   unknown   the worker container is up but neither line appeared in the
#             window — e.g. no diarization job ran in it yet
#   absent    the named worker container is not running at all
#
# Deliberately does NOT prove GPU residency — that is
# scripts/diar-native-smoke.sh's job (device-memory residency via
# nvidia-smi), and it proves the opposite half of this question: the sidecar
# process is alive and pinned to the right GPU, not that any job was routed
# to it. Callers that want both should run both.
ac_diar_engine_verdict() {
    local worker_container="${1:-opentranscribe-celery-worker}"
    local since="${2:-30m}"

    if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$worker_container"; then
        echo "absent"
        return 0
    fi

    local logs native_hits fallback_hits
    logs=$(docker logs --since "$since" "$worker_container" 2>&1 || true)
    native_hits=$(grep -c 'native diarization done in' <<<"$logs" || true)
    fallback_hits=$(grep -c 'falling back to PyAnnote' <<<"$logs" || true)

    if (( fallback_hits > 0 )); then
        echo "fallback"
    elif (( native_hits > 0 )); then
        echo "native"
    else
        echo "unknown"
    fi
}

ac_wait_for_file_status() {
    # Poll /api/files/<uuid> until status == completed (or error/timeout).
    local file_uuid="$1"
    local timeout="${2:-1800}"
    local deadline=$(( $(date +%s) + timeout ))
    local status=""
    while (( $(date +%s) < deadline )); do
        status=$(ac_curl "$API_BASE/files/$file_uuid" 2>/dev/null \
            | python3 -c 'import sys,json; print(json.load(sys.stdin).get("status",""))' 2>/dev/null || echo "")
        case "$status" in
            completed)
                ac_log "file $file_uuid reached status=completed"
                return 0
                ;;
            error|failed)
                ac_dump_failure_diagnostics "$file_uuid"
                ac_die "file $file_uuid ended in status=$status"
                ;;
        esac
        ac_log "  file $file_uuid status=${status:-<unknown>} (waiting)"
        sleep 10
    done
    ac_dump_failure_diagnostics "$file_uuid"
    ac_die "file $file_uuid never reached completed status within ${timeout}s"
}

ac_get_file() {
    ac_curl "$API_BASE/files/$1"
}

ac_get_segments() {
    # Returns transcript segments JSON for a file (uuid).
    # IMPORTANT: /content returns the raw audio file body, NOT JSON. The
    # canonical transcript endpoint is /segments.
    ac_curl "$API_BASE/files/$1/segments"
}

# Backward-compat alias
ac_get_transcript() { ac_get_segments "$@"; }

# How many transcript segments does this file have? Echoes an integer (0 on any
# failure, so callers can compare numerically without guarding).
#
# This exists because the response shape is not obvious and each caller that
# re-derived it got it slightly wrong. The endpoint returns
# {"transcript_segments": [...]}, but a hand-written parser reaches for
# "segments" — which yields 0 for a file that has plenty, i.e. a FAILING
# assertion on a working system. That is the worst kind of test bug: it accuses
# the product. It cost a full upgrade-scenario run to diagnose.
#
# One parser, used by every scenario.
ac_segment_count() {
    ac_get_segments "$1" 2>/dev/null | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print(0); sys.exit()
if isinstance(d, list):
    print(len(d))
else:
    segs = (d.get("transcript_segments") or d.get("segments")
            or d.get("results") or [])
    print(len(segs))
' 2>/dev/null || echo 0
}

ac_list_files() {
    ac_curl "$API_BASE/files"
}

ac_list_speakers() {
    ac_curl "$API_BASE/speakers" 2>/dev/null || ac_curl "$API_BASE/speakers/"
}

ac_search() {
    # GET /api/search?q=<term>&page=1&page_size=10
    local q="$1"
    ac_curl --get "$API_BASE/search" \
        --data-urlencode "q=$q" \
        --data-urlencode "page=1" \
        --data-urlencode "page_size=10"
}

# ac_create_asr_config PROVIDER BASE_URL API_KEY [NAME]
#
# POST /api/... registers a cloud ASR provider config, then activates it via
# POST /asr-settings/set-active (config_uuid) — mirrors the two-step sequence
# backend/tests/fixtures/mock_asr.py's register_mock_gladia_asr_config uses
# against the real API (see backend/tests/integration/
# test_lite_mode_mocked_providers.py, Phase 3). model_name is a required
# field on UserASRSettingsCreate (backend/app/schemas/asr_settings.py) —
# omitting it 422s. Echoes the new config's uuid.
ac_create_asr_config() {
    local provider="$1" base_url="$2" api_key="$3"
    local name="${4:-release-test-${provider}-$(date +%s)}"
    ac_log "creating ASR config: provider=$provider base_url=$base_url"
    # Idempotent: a same-named config from a prior (interrupted or --force'd)
    # run of this phase 409s on create otherwise, since name is unique per
    # user. Delete-if-exists first so a re-run never needs manual cleanup.
    local existing_uuid
    existing_uuid=$(ac_curl "$API_BASE/asr-settings" 2>/dev/null \
        | python3 -c "
import sys, json
name = sys.argv[1]
for c in json.load(sys.stdin).get('configs', []):
    if c.get('name') == name:
        print(c['uuid']); break
" "$name" 2>/dev/null || true)
    if [[ -n "$existing_uuid" ]]; then
        ac_log "deleting pre-existing ASR config '$name' ($existing_uuid) before recreating"
        ac_curl -X DELETE "$API_BASE/asr-settings/config/$existing_uuid" >/dev/null 2>&1 || true
    fi
    local payload
    payload=$(python3 -c '
import json, sys
print(json.dumps({
    "name": sys.argv[1],
    "provider": sys.argv[2],
    "model_name": "default",
    "base_url": sys.argv[3],
    "api_key": sys.argv[4],
}))
' "$name" "$provider" "$base_url" "$api_key")
    local body config_uuid
    body=$(ac_curl -X POST "$API_BASE/asr-settings" \
        -H "Content-Type: application/json" \
        -d "$payload") || ac_die "ASR config creation failed"
    config_uuid=$(echo "$body" | python3 -c 'import sys,json; print(json.load(sys.stdin)["uuid"])')
    ac_curl -X POST "$API_BASE/asr-settings/set-active" \
        -H "Content-Type: application/json" \
        -d "{\"config_uuid\": \"$config_uuid\"}" >/dev/null || ac_die "ASR config activation failed"
    echo "$config_uuid"
}

# ac_create_llm_config PROVIDER MODEL_NAME BASE_URL API_KEY [NAME]
#
# Mirrors the POST /api/llm-settings payload validated in
# test_lite_mode_mocked_providers.py's TestMockedAsrPlusMockedLlm — provider
# "custom" pointed at the mock-llm OpenAI-compatible server. Then activates
# it via POST /llm-settings/set-active (configuration_id — this endpoint's
# field name, per schemas/llm_settings.py's SetActiveConfigRequest, differs
# from the ASR endpoint's config_uuid). Echoes the new config's uuid.
ac_create_llm_config() {
    local provider="$1" model_name="$2" base_url="$3" api_key="$4"
    local name="${5:-release-test-${provider}-$(date +%s)}"
    ac_log "creating LLM config: provider=$provider model=$model_name base_url=$base_url"
    # Idempotent, same reason as ac_create_asr_config: a same-named config
    # from a prior run 409s on create otherwise.
    local existing_uuid
    existing_uuid=$(ac_curl "$API_BASE/llm-settings" 2>/dev/null \
        | python3 -c "
import sys, json
name = sys.argv[1]
for c in json.load(sys.stdin).get('configurations', []):
    if c.get('name') == name:
        print(c['uuid']); break
" "$name" 2>/dev/null || true)
    if [[ -n "$existing_uuid" ]]; then
        ac_log "deleting pre-existing LLM config '$name' ($existing_uuid) before recreating"
        ac_curl -X DELETE "$API_BASE/llm-settings/config/$existing_uuid" >/dev/null 2>&1 || true
    fi
    local payload
    payload=$(python3 -c '
import json, sys
print(json.dumps({
    "name": sys.argv[1],
    "provider": sys.argv[2],
    "model_name": sys.argv[3],
    "base_url": sys.argv[4],
    "api_key": sys.argv[5],
}))
' "$name" "$provider" "$model_name" "$base_url" "$api_key")
    local body config_uuid
    body=$(ac_curl -X POST "$API_BASE/llm-settings" \
        -H "Content-Type: application/json" \
        -d "$payload") || ac_die "LLM config creation failed"
    config_uuid=$(echo "$body" | python3 -c 'import sys,json; print(json.load(sys.stdin)["uuid"])')
    ac_curl -X POST "$API_BASE/llm-settings/set-active" \
        -H "Content-Type: application/json" \
        -d "{\"configuration_id\": \"$config_uuid\"}" >/dev/null || ac_die "LLM config activation failed"
    echo "$config_uuid"
}

# ac_chat_completion LLM_CONFIG_UUID FILE_UUID QUESTION
#
# Creates a scoped conversation pinned to llm_config_uuid, sends one message,
# and parses the SSE response stream via lib/parse-chat-sse.py. Mirrors the
# parsing done inline in test_lite_mode_mocked_providers.py's
# test_chat_summary_has_non_empty_grounded_content: `event: delta` frames
# carry {"content"|"text": ...} chunks concatenated into the answer,
# `event: sources` carries {"citations": [...]}.
#
# GH #595: an `event: error` frame (the LLM call was blocked — e.g. by the SSRF
# guard refusing a private-network endpoint — or otherwise failed) used to be
# silently dropped by this parser, so a *correctly refused* request and a
# genuinely empty answer were indistinguishable. The parser captures `error`
# frames too, so a blocked call reads as "blocked", not "empty".
#
# GH #611: this used to print three newline-separated records (answer,
# citation count, error code) and rely on the caller reading them back with
# `sed -n '1p'/'2p'/'3p'`. The answer is markdown from an LLM and routinely
# spans several lines (see mock-llm-server.py's REPLY_TEMPLATE), so a
# positional read of "line 3" landed inside the answer text itself and was
# misreported as an error frame — a newline-delimited "record" scheme cannot
# represent multi-line content. `parse-chat-sse.py` instead emits ONE line of
# JSON (`{"answer", "citations", "error"}`); json.dumps escapes embedded
# newlines, so the answer cannot forge a record boundary no matter its shape.
#
# Deletes the conversation on return (best-effort). Prints ONE line of JSON
# to stdout: `{"answer": "...", "citations": <int>, "error": "..."}`. `error`
# is the empty string when no `event: error` frame arrived. Use `ac_json_field`
# to read a key out of it.
ac_chat_completion() {
    local llm_config_uuid="$1" file_uuid="$2" question="$3"
    local csrf_token="${API_CSRF_TOKEN:-}"
    [[ -n "$csrf_token" ]] || ac_die "ac_chat_completion requires API_CSRF_TOKEN (cookie-session CSRF header) to be set"

    local conv_payload conv_body conversation_uuid
    conv_payload=$(python3 -c '
import json, sys
print(json.dumps({
    "title": "release-test chat",
    "llm_config_uuid": sys.argv[1],
    "scope": {"file_uuids": [sys.argv[2]], "collection_uuids": [], "tag_names": [], "speakers": []},
}))
' "$llm_config_uuid" "$file_uuid")
    conv_body=$(ac_curl -X POST "$API_BASE/chat/conversations" \
        -H "Content-Type: application/json" \
        -H "X-CSRF-Token: $csrf_token" \
        -d "$conv_payload") || ac_die "chat conversation creation failed"
    conversation_uuid=$(echo "$conv_body" | python3 -c 'import sys,json; print(json.load(sys.stdin)["uuid"])')

    local msg_payload
    msg_payload=$(python3 -c 'import json,sys; print(json.dumps({"content": sys.argv[1]}))' "$question")

    # requests-style SSE parsing: `event:` lines set the frame type, `data:`
    # lines carry its JSON payload, a blank line ends the frame. Relocated to
    # a standalone, unit-tested script (GH #611) — see parse-chat-sse.py's
    # module docstring for why a heredoc parser shipped this bug untested.
    curl -sS --no-buffer -X POST "$API_BASE/chat/conversations/$conversation_uuid/messages" \
        -H "Content-Type: application/json" \
        -H "X-CSRF-Token: $csrf_token" \
        -H "Accept: text/event-stream" \
        -H "Authorization: Bearer ${API_TOKEN:-}" \
        -d "$msg_payload" \
    | python3 "$(dirname "${BASH_SOURCE[0]}")/parse-chat-sse.py"

    ac_curl -X DELETE "$API_BASE/chat/conversations/$conversation_uuid" \
        -H "X-CSRF-Token: $csrf_token" >/dev/null 2>&1 || true
}

# ac_json_field JSON KEY
#
# Prints the raw value of KEY from a single JSON object on stdin-less input
# (JSON passed as $1). Empty string when the key is absent, JSON is empty, or
# the input does not parse — never a python traceback into the caller's
# variable. Deliberately stdlib python3, not jq: nothing else in
# release-tests/ requires jq and the rehearsal must run on a bare host.
ac_json_field() {
    local json="$1" key="$2"
    python3 -c '
import json, sys
raw = sys.argv[1]
key = sys.argv[2]
try:
    data = json.loads(raw) if raw.strip() else {}
except json.JSONDecodeError:
    data = {}
value = data.get(key, "") if isinstance(data, dict) else ""
print(value if value is not None else "")
' "$json" "$key"
}
