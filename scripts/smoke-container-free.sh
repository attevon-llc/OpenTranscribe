#!/usr/bin/env bash
# Container-free smoke check: the cheap structural checks that must stay green across
# moves and splits, without starting any container.
#
# Steps (one PASS/FAIL line each, exit non-zero if any FAIL):
#   1. bash -n on every shell file in release-manifest.txt plus opentr.sh
#   2. the backend app imports
#   3. route-table dump (index, methods, path, name) written to $OUT/routes.txt
#   4. OpenAPI snapshot is current
#   5. pipeline docs tables are current
#   6. `opentr.sh start dev --dry-run` prints a compose chain (it starts nothing)
#
# Environment:
#   SMOKE_ROOT    repo root to check (default: the repo containing this script)
#   SMOKE_PYTHON  interpreter (default: $SMOKE_ROOT/backend/venv/bin/python, else python3)
#   OUT           output directory for routes.txt and per-step logs (default: a new temp dir)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${SMOKE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
PY="${SMOKE_PYTHON:-$ROOT/backend/venv/bin/python}"
[ -x "$PY" ] || PY="python3"
OUT="${OUT:-$(mktemp -d -t ot-smoke.XXXXXX)}"
WORK="$(mktemp -d -t ot-smoke-work.XXXXXX)"
mkdir -p "$OUT" "$WORK/data" "$WORK/models" "$WORK/temp"
trap 'rm -rf "$WORK"' EXIT

# Never let the smoke touch a GPU.
export CUDA_VISIBLE_DEVICES=""

PASS=0
FAIL=0

record() { # record <rc> <label> <logfile>
  local rc="$1" label="$2" log="$3"
  if [ "$rc" -eq 0 ]; then
    PASS=$((PASS + 1))
    echo "PASS  $label"
  else
    FAIL=$((FAIL + 1))
    echo "FAIL  $label (exit $rc; log: $log)"
    tail -n 15 "$log" | sed 's/^/        /'
  fi
}

# --- 1. bash -n over the manifest's shell files plus opentr.sh ---------------------------
step_bash_syntax() {
  local manifest="$ROOT/release-manifest.txt" path bad=0 count=0
  local -a files=()
  if [ ! -f "$manifest" ]; then
    echo "missing $manifest"
    return 1
  fi
  while IFS=$'\t ' read -r path _; do
    case "$path" in '' | '#'*) continue ;; esac
    case "$path" in *.sh) files+=("$path") ;; esac
  done <"$manifest"
  files+=("opentr.sh")
  for path in "${files[@]}"; do
    count=$((count + 1))
    if [ ! -f "$ROOT/$path" ]; then
      echo "missing file: $path"
      bad=1
    elif ! bash -n "$ROOT/$path"; then
      echo "syntax error: $path"
      bad=1
    fi
  done
  echo "checked $count shell files"
  return "$bad"
}

# --- 2/3. import and route dump ----------------------------------------------------------
app_env() {
  TESTING=True DATA_DIR="$WORK/data" MODELS_DIR="$WORK/models" TEMP_DIR="$WORK/temp" "$@"
}

step_import() {
  (cd "$ROOT/backend" && app_env "$PY" -c "import app.main")
}

step_routes() {
  (cd "$ROOT/backend" && app_env "$PY" -c "from app.main import app
for i, r in enumerate(app.routes):
    print(i, ','.join(sorted(getattr(r, 'methods', None) or [])), r.path, getattr(r, 'name', ''))" \
    >"$OUT/routes.txt") && [ -s "$OUT/routes.txt" ] \
    && echo "routes: $(wc -l <"$OUT/routes.txt") -> $OUT/routes.txt"
}

# --- 4/5. generated artifacts ------------------------------------------------------------
step_openapi() {
  (cd "$ROOT" && TESTING=True "$PY" scripts/generate-openapi.py --check)
}

step_pipeline_docs() {
  (cd "$ROOT" && TESTING=True "$PY" scripts/generate-pipeline-docs.py --check)
}

# --- 6. compose chain preview (dry run reaches no `docker compose up`) --------------------
step_dry_run() {
  local log="$OUT/opentr-dry-run.txt"
  (cd "$ROOT" && ./opentr.sh start dev --dry-run) >"$log" 2>&1 || {
    cat "$log"
    return 1
  }
  cat "$log"
  grep -q 'Compose files:' "$log" && grep -q 'DRY RUN' "$log"&& grep -q 'DRY RUN' "$log" grep -q "DRY RUN" "$log"
}

run_step() { # run_step <label> <function>
  local label="$1" fn="$2" log rc=0
  log="$OUT/$(echo "$label" | tr -c 'A-Za-z0-9\n' '_').log"
  # `|| rc=$?` keeps a failing step from aborting the report under `set -e`.
  "$fn" >"$log" 2>&1 || rc=$?
  record "$rc" "$label" "$log"
}

run_step "bash -n: release-manifest shell files + opentr.sh" step_bash_syntax
run_step "import app.main" step_import
run_step "route table dump" step_routes
run_step "generate-openapi.py --check" step_openapi
run_step "generate-pipeline-docs.py --check" step_pipeline_docs
run_step "opentr.sh start dev --dry-run" step_dry_run

echo "----"
echo "smoke: $PASS passed, $FAIL failed (output: $OUT)"
[ "$FAIL" -eq 0 ]
