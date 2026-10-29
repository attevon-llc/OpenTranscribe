#!/usr/bin/env bash
# Smoke-test the anonymous surface of a deployed OpenTranscribe URL.
#
# Run it against a throwaway `--fresh` stack (see docs-site/docs/operations/fresh-deployments.md)
# or against a public URL with the owner's go-ahead:
#
#   ./opentr.sh start prod --build --fresh smoke --port-offset 200
#   scripts/public-smoke.sh http://127.0.0.1:5373 --allow-http
#
# Checks, in order (all unauthenticated):
#   1. scripts/check-public-headers.py <url>   headers, cookies, TLS, http->https redirect
#   2. GET  /api/auth/session                  200 for an anonymous visitor
#   3. GET  /api/auth/me                       401 without cookies
#   4. POST /api/auth/login                    NONEXISTENT account until the limiter answers 429
#                                              (never wrong passwords for a real account: they
#                                              lock it out)
#   5. GET  /api/docs, /api/redoc, /api/openapi.json
#                                              404 on a hardened deployment (200 with --expect-docs)
#
# SAFETY. The target must NOT be the live dev or production stack on this machine. Any
# address that resolves to loopback, 0.0.0.0, link-local or one of this host's own
# interfaces, on a port the stack publishes (5173-5190, 80, 443), is refused before any
# request is sent (lib/target_guard.py); `--allow-live-dev` overrides it. Prefer a
# `--fresh` stack with `--port-offset` of 100 or more. Step 4 also leaves failed-login
# audit events in the target's log and exhausts the login limiter for this client
# address (for everyone behind it when the limiter keys on a shared proxy address),
# so never aim it at a site with real users without the owner's go-ahead.
#
# `--expect-docs` is meaningful only for deployments whose /api reaches the backend:
# the frontend-only image answers 404 for these paths regardless of ENABLE_API_DOCS.
#
# Step 4 deliberately trips the login rate limit for this client address for about a minute.
#
# Exit codes: 0 all passed, 1 a check failed, 2 usage error or refused target.
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
usage: public-smoke.sh <base-url> [--allow-http] [--insecure] [--expect-docs]
                       [--http-url URL | --skip-redirect-check] [--allow-live-dev]
  --allow-http          accept an http:// base (throwaway stacks only)
  --insecure            skip certificate verification for the curl probes and header requests
  --expect-docs         expect /api/docs and /api/openapi.json to be served (ENABLE_API_DOCS=true)
  --http-url URL        plain-HTTP URL that should redirect to https
  --skip-redirect-check do not test the http->https redirect
  --allow-live-dev      permit targets that resolve to this machine on the stack's ports
USAGE
  exit 2
}

[ $# -ge 1 ] || usage
BASE="${1%/}"
shift

ALLOW_HTTP=0
INSECURE=0
EXPECT_DOCS=0
ALLOW_LIVE=0
HEADER_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --allow-http) ALLOW_HTTP=1; HEADER_ARGS+=(--allow-http) ;;
    --insecure) INSECURE=1; HEADER_ARGS+=(--insecure) ;;
    --expect-docs) EXPECT_DOCS=1 ;;
    --allow-live-dev) ALLOW_LIVE=1 ;;
    --skip-redirect-check) HEADER_ARGS+=(--skip-redirect-check) ;;
    --http-url)
      [ $# -ge 2 ] || usage
      HEADER_ARGS+=(--http-url "$2")
      shift
      ;;
    *) usage ;;
  esac
  shift
done

case "$BASE" in
  https://*) ;;
  http://*)
    [ "$ALLOW_HTTP" -eq 1 ] || { echo "refusing plain-http base without --allow-http" >&2; exit 2; }
    ;;
  *) echo "base url must start with http:// or https://" >&2; exit 2 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/refuse_live_stack.sh
source "$SCRIPT_DIR/lib/refuse_live_stack.sh"
guard_rc=0
refuse_live_stack "$BASE" "$ALLOW_LIVE" || guard_rc=$?
[ "$guard_rc" -eq 0 ] || exit 2

CURL=(curl --silent --show-error --max-time 20 --output /dev/null --write-out '%{http_code}')
[ "$INSECURE" -eq 1 ] && CURL+=(--insecure)

FAILED=0
fail() { echo "FAIL: $*"; FAILED=1; }
pass() { echo "ok:   $*"; }

# curl prints the code via --write-out even on failure ("000"), so no fallback echo here.
status_of() { "${CURL[@]}" "$@" 2>/dev/null || true; }

expect_status() { # <label> <expected> <curl args...>
  local label="$1" want="$2" got
  shift 2
  got="$(status_of "$@")"
  if [ "$got" = "$want" ]; then pass "$label -> $got"; else fail "$label -> $got (wanted $want)"; fi
}

echo "== header and TLS checks"
hdr_rc=0
python3 "$SCRIPT_DIR/check-public-headers.py" "$BASE" ${HEADER_ARGS[@]+"${HEADER_ARGS[@]}"} || hdr_rc=$?
case "$hdr_rc" in
  0) pass "check-public-headers" ;;
  1) fail "check-public-headers reported findings" ;;
  *) echo "check-public-headers could not reach $BASE (exit $hdr_rc)" >&2; exit 2 ;;
esac

echo "== anonymous surface"
expect_status "GET /api/auth/session (anonymous)" 200 "$BASE/api/auth/session"
expect_status "GET /api/auth/me (no cookies)" 401 "$BASE/api/auth/me"

echo "== login rate limit (nonexistent account)"
user="nobody-$(date +%s)-$RANDOM@invalid.example"
limited=0
for attempt in $(seq 1 40); do
  code="$(status_of -X POST --data-urlencode "username=$user" --data-urlencode "password=x" \
    "$BASE/api/auth/login")"
  case "$code" in
    429) limited=$attempt; break ;;
    400 | 401 | 422) ;;
    *) fail "login probe attempt $attempt -> unexpected $code"; break ;;
  esac
done
if [ "$limited" -gt 0 ]; then
  pass "login limiter answered 429 after $limited attempts"
else
  fail "login limiter never answered 429 in 40 attempts"
fi

echo "== API docs exposure"
if [ "$EXPECT_DOCS" -eq 1 ]; then want=200; else want=404; fi
expect_status "GET /api/docs" "$want" "$BASE/api/docs"
expect_status "GET /api/redoc" "$want" "$BASE/api/redoc"
expect_status "GET /api/openapi.json" "$want" "$BASE/api/openapi.json"

if [ "$FAILED" -ne 0 ]; then
  echo "public-smoke: FAILED"
  exit 1
fi
echo "public-smoke: all checks passed"
