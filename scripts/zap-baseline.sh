#!/usr/bin/env bash
# OWASP ZAP baseline scan (passive: spiders for a minute, sends no attacks) of ONE URL.
#
# SAFETY. Point this only at a throwaway stack, never at the live dev or production stack.
# `--network host` means the scan can reach anything this machine can, so a target that
# resolves to loopback, 0.0.0.0, link-local or one of this host's own addresses on a port
# the stack publishes (5173-5190, 80, 443) is refused before docker runs
# (lib/target_guard.py); `--allow-live-dev` overrides it. Use `--port-offset` of 100 or more:
#   ./opentr.sh start prod --build --fresh zap --port-offset 300     # own containers + volumes
#   scripts/zap-baseline.sh http://127.0.0.1:5473 .zap-reports/zap1
#   ./opentr.sh fresh-destroy zap
# (use `start dev --fresh zap` if the fresh path does not support prod; `--dry-run` shows
# what would start). A scan against a public URL needs the site owner's explicit go-ahead.
# The baseline is unauthenticated: it sees the login page, static assets, public API errors
# and response headers, nothing behind a session.
#
# The scan runs inside the official ZAP image on the host network so it can reach
# 127.0.0.1 ports of a fresh stack. Override the image with ZAP_IMAGE to pin a digest, e.g.
#   ZAP_IMAGE=ghcr.io/zaproxy/zaproxy@sha256:<digest>
# (the default is the moving `stable` tag; resolve and record a digest before a release).
#
# Usage: zap-baseline.sh <base-url> <out-dir> [--allow-live-dev] [--minutes N] [--fail-on-warn]
# Reports: <out-dir>/zap.XXXXXX/zap.json and zap.html (a fresh child per run)
# Rules:   .zap/rules.tsv (rule id, IGNORE|WARN|FAIL, reason in the name column)
#
# Exit codes: zap-baseline's own (0 pass, 1 FAIL alert, 2 WARN only, 3 error), except that
# warnings do not fail unless --fail-on-warn; 64 is a usage error or refused target.
set -euo pipefail

ZAP_IMAGE="${ZAP_IMAGE:-ghcr.io/zaproxy/zaproxy:stable}"

usage() {
  echo "usage: zap-baseline.sh <base-url> <out-dir> [--allow-live-dev] [--minutes N] [--fail-on-warn]" >&2
  exit 64
}

[ $# -ge 2 ] || usage
URL="${1%/}"
OUT="$2"
shift 2

ALLOW_LIVE=0
MINUTES=1
FAIL_ON_WARN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --allow-live-dev) ALLOW_LIVE=1 ;;
    --fail-on-warn) FAIL_ON_WARN=1 ;;
    --minutes)
      [ $# -ge 2 ] || usage
      MINUTES="$2"
      shift
      ;;
    *) usage ;;
  esac
  shift
done

case "$URL" in
  http://* | https://*) ;;
  *) echo "base url must start with http:// or https://" >&2; exit 64 ;;
esac
case "$MINUTES" in '' | *[!0-9]*) echo "--minutes must be a whole number" >&2; exit 64 ;; esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/refuse_live_stack.sh
source "$SCRIPT_DIR/lib/refuse_live_stack.sh"
guard_rc=0
refuse_live_stack "$URL" "$ALLOW_LIVE" || guard_rc=$?
[ "$guard_rc" -eq 0 ] || exit 64

RULES="$SCRIPT_DIR/../.zap/rules.tsv"
[ -f "$RULES" ] || { echo "missing $RULES" >&2; exit 64; }

# Never touch the directory the caller named beyond creating a fresh child in it: the
# scan works in (and mounts) <out-dir>/zap.XXXXXX, and only that child is made writable
# for the container user. An existing rules.tsv or report in <out-dir> is left alone.
mkdir -p "$OUT"
WORK="$(mktemp -d "$OUT/zap.XXXXXX")"
chmod 777 "$WORK"
cp "$RULES" "$WORK/rules.tsv"
chmod 444 "$WORK/rules.tsv"
OUT="$WORK"

ARGS=(zap-baseline.py -t "$URL" -c rules.tsv -J zap.json -r zap.html -m "$MINUTES")
[ "$FAIL_ON_WARN" -eq 1 ] || ARGS+=(-I)

echo "ZAP baseline: $URL (image $ZAP_IMAGE, reports in $OUT)"
rc=0
docker run --rm --network host -v "$OUT:/zap/wrk:rw" "$ZAP_IMAGE" "${ARGS[@]}" || rc=$?
# Docker's own failures (125-127, signals) are "other failure", not a scan verdict.
case "$rc" in 0 | 1 | 2 | 3) ;; *) echo "docker exited $rc; reporting 3" >&2; rc=3 ;; esac
echo "ZAP baseline exit code: $rc"
exit "$rc"
