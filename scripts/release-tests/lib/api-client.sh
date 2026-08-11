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
                ac_die "file $file_uuid ended in status=$status"
                ;;
        esac
        ac_log "  file $file_uuid status=${status:-<unknown>} (waiting)"
        sleep 10
    done
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
