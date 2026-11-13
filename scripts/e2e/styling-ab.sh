#!/bin/bash
#
# styling-ab.sh - before/after screenshot + computed-style comparison for the frontend
# styling layer (docs/design/frontend_styling_layer_plan.md, section 6).
#
# Builds a BEFORE and an AFTER copy of frontend/ from git refs (or the working tree), starts
# one Vite dev server per copy against ONE shared isolated backend, and drives
# backend/tests/e2e/styling_ab.py through every screen x variant (en/ar x light/dark).
#
#   scripts/e2e/styling-ab.sh --lane p0-sanity --base HEAD --after WORKTREE --mode zero \
#       --stack styling
#   scripts/e2e/styling-ab.sh --lane noise --base HEAD --selfcheck --stack styling
#   scripts/e2e/styling-ab.sh --lane noise --base HEAD --negative-control --stack styling
#
# The lead starts and stops the stack (this script never does):
#   ./opentr.sh start dev --fresh styling --port-offset 200 --seed-benchmark --with-mock-llm
#
# A tree whose package-lock.json differs from the checkout's gets its own `npm ci` (log in
# the run dir); trees with an identical lock share the checkout's node_modules.
#
# Refuses unless the stack resolves to an otfresh-<stack> compose project (stack-target
# `isolated`, `healthy`) whose backend answers /health, so it can never be pointed at the live stack. Never kills by name: the EXIT
# trap TERMs only the PIDs this script started.
#
# Exit: 0 pass, 1 differences/failures, 2 misuse, 3 precondition unmet.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
. "$(dirname "${BASH_SOURCE[0]}")/styling-ab-deps.sh"
MAIN_ROOT="$(cd "$REPO_ROOT" && dirname "$(readlink -f "$(git rev-parse --git-common-dir)")")"
NODE_BIN="${STYLING_AB_NODE_BIN:-$HOME/.nvm/versions/node/v26.8.2/bin}"
WORK_ROOT="${STYLING_AB_WORK_ROOT:-$HOME/ot-cleanup-work/styling/ab}"

LANE="" BASE="" AFTER="WORKTREE" MODE="zero" STACK="" SCREENS="all" VARIANTS=""
CONTROL="" SELFCHECK=0 NEGCTL=0

usage() { sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-2}"; }
die() { echo "styling-ab: $*" >&2; exit "${2:-2}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --lane) LANE="$2"; shift 2 ;;
    --base) BASE="$2"; shift 2 ;;
    --after) AFTER="$2"; shift 2 ;;
    --mode) MODE="$2"; shift 2 ;;
    --stack) STACK="$2"; shift 2 ;;
    --screens) SCREENS="$2"; shift 2 ;;
    --variants) VARIANTS="$2"; shift 2 ;;
    --control) CONTROL="$2"; shift 2 ;;
    --selfcheck) SELFCHECK=1; shift ;;
    --negative-control) NEGCTL=1; shift ;;
    -h|--help) usage 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[ -n "$LANE" ] || die "--lane is required (names the output directory)"
[ -n "$BASE" ] || die "--base <git ref> is required"
[ -n "$STACK" ] || die "--stack <fresh stack name> is required"
case "$MODE" in zero|intended) ;; *) die "--mode must be zero or intended" ;; esac
case "$LANE" in *[!A-Za-z0-9._-]*|"") die "--lane may contain only letters, digits . _ -" ;; esac

# --- the stack: refuse the live one ----------------------------------------------------
# Resolved through the shared library (scripts/lib/stack_target.py): the OBSERVED ports of the
# otfresh-<stack> project, not `5174 + the offset file`. `isolated` refuses any stack that is
# not a --fresh project, and exported ports belonging to another project exit 21.
. "$REPO_ROOT/scripts/lib/stack-target.sh"
st_target --fresh "$STACK"; st_rc=$?
[ "$st_rc" -eq 0 ] || die "stack otfresh-$STACK could not be resolved (stack-target exit $st_rc); start it: ./opentr.sh start dev --fresh $STACK --port-offset 200 --seed-benchmark --with-mock-llm" 3
st_require isolated healthy; st_rc=$?
[ "$st_rc" -eq 0 ] || die "stack otfresh-$STACK failed its checks (stack-target exit $st_rc)" 3
[ -n "${BACKEND_PORT:-}" ] && [ -n "${MINIO_PORT:-}" ] || die "stack otfresh-$STACK publishes no backend/minio port" 3
BACKEND="http://localhost:$BACKEND_PORT"
curl -fsS --max-time 10 "$BACKEND/health" >/dev/null 2>&1 \
  || die "backend $BACKEND/health does not answer" 3

# The two Vite servers' ports follow the backend's, which is unique per stack, so two lanes
# on two stacks cannot collide.
PORT_BEFORE=$((BACKEND_PORT + 117)); PORT_AFTER=$((BACKEND_PORT + 118))
port_bound() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
port_bound "$PORT_BEFORE" && die "port $PORT_BEFORE is already bound" 3
if [ "$SELFCHECK" = 0 ] && [ "$NEGCTL" = 0 ]; then
  port_bound "$PORT_AFTER" && die "port $PORT_AFTER is already bound" 3
fi
[ -x "$NODE_BIN/node" ] || die "node not found at $NODE_BIN (set STYLING_AB_NODE_BIN)" 3
[ -x "$REPO_ROOT/backend/venv/bin/python" ] || die "backend/venv missing" 3
NODE_MODULES="$(readlink -f "$REPO_ROOT/frontend/node_modules")"
[ -d "$NODE_MODULES/vite" ] || die "frontend/node_modules (or its target) has no vite" 3

# --- trees -----------------------------------------------------------------------------
W="$WORK_ROOT/$LANE"
mkdir -p "$W"
PIDS=()
cleanup() { for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill -TERM "$p" 2>/dev/null; done; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

FONTS=""
for root in "$REPO_ROOT" "$MAIN_ROOT"; do
  [ -d "$root/frontend/static/fonts" ] && { FONTS="$root/frontend/static/fonts"; break; }
done
[ -n "$FONTS" ] || die "frontend/static/fonts not found (gitignored; _stabilize refuses to capture without Poppins)" 3

build_tree() { # <name> <ref|WORKTREE>
  local name="$1" ref="$2" dir="$W/$1"
  rm -rf -- "$dir"; mkdir -p "$dir"
  if [ "$ref" = "WORKTREE" ]; then
    # A snapshot, so later edits by anyone never leak in and the checkout is not touched.
    mkdir -p "$dir/frontend"
    rsync -a --exclude node_modules --exclude .svelte-kit --exclude build "$REPO_ROOT/frontend/" "$dir/frontend/" || die "rsync failed" 3
  else
    (cd "$REPO_ROOT" && git archive "$ref" frontend) | tar -x -C "$dir" || die "git archive $ref failed" 3
  fi
  mkdir -p "$dir/frontend/static"; rm -rf -- "$dir/frontend/static/fonts"; cp -a "$FONTS" "$dir/frontend/static/fonts"
}

start_vite() { # <name> <port>
  local name="$1" port="$2"
  ( cd "$W/$name/frontend" && \
    PATH="$NODE_BIN:$PATH" VITE_API_PROXY_TARGET="$BACKEND" VITE_MINIO_URL="http://localhost:$MINIO_PORT" \
    exec nice -n 19 "$NODE_BIN/node" node_modules/vite/bin/vite.js dev --port "$port" --strictPort \
    >"$W/vite-$name.log" 2>&1 ) &
  PIDS+=("$!")
  for _ in $(seq 1 90); do
    curl -fsS --max-time 3 "http://localhost:$port/" >/dev/null 2>&1 && return 0
    kill -0 "${PIDS[-1]}" 2>/dev/null || { tail -20 "$W/vite-$name.log" >&2; die "vite ($name) exited; log: $W/vite-$name.log" 3; }
    sleep 2
  done
  die "vite ($name) did not answer on :$port within 180 s; log: $W/vite-$name.log" 3
}

provision() { ab_provision_node_modules "$1" "$W/$1/frontend" "$NODE_MODULES" "$REPO_ROOT/frontend/package-lock.json" "$W" || exit 3; }

build_tree before "$BASE"; provision before
if [ "$SELFCHECK" = 1 ] || [ "$NEGCTL" = 1 ]; then
  start_vite before "$PORT_BEFORE"
  URL_BEFORE="http://localhost:$PORT_BEFORE"; URL_AFTER="$URL_BEFORE"
else
  build_tree after "$AFTER"; provision after
  start_vite before "$PORT_BEFORE"; start_vite after "$PORT_AFTER"
  URL_BEFORE="http://localhost:$PORT_BEFORE"; URL_AFTER="http://localhost:$PORT_AFTER"
fi

# --- run -------------------------------------------------------------------------------
ARGS=(--before "$URL_BEFORE" --after "$URL_AFTER" --backend "$BACKEND" --out "$W" --mode "$MODE" --screens "$SCREENS")
[ -n "$VARIANTS" ] && ARGS+=(--variants "$VARIANTS")
[ -n "$CONTROL" ] && ARGS+=(--control "$CONTROL")
[ "$SELFCHECK" = 1 ] && ARGS+=(--selfcheck)
[ "$NEGCTL" = 1 ] && ARGS+=(--negative-control)

cd "$REPO_ROOT/backend/tests/e2e" || exit 3
nice -n 19 "$REPO_ROOT/backend/venv/bin/python" -I -X utf8 -c \
  'import sys; sys.path.insert(0, "."); import styling_ab; sys.exit(styling_ab.main(sys.argv[1:]))' "${ARGS[@]}"
RC=$?
echo "styling-ab: output in $W (summary.json, changes.txt, before/ after/ diff/)"
exit "$RC"
