#!/bin/bash
# GPU soak orchestrator — bench-aware, checkpointed, resumable.
#
# Runs phases 1-5 of docs/SOAK_TEST_HANDOFF_PROMPT.md against the ISOLATED bench
# stack. Every work unit (each concurrency level, each phase) is checkpointed in
# benchmarks/soak_state/checkpoint.txt. Re-running this script skips completed
# units and resumes where it stopped — so an interrupted session never re-burns
# compute it already finished.
#
# Launch detached so it survives a dead Claude session:
#   setsid bash scripts/soak_orchestrate.sh >/dev/null 2>&1 &
# Resume after interruption: just run the same command again.
#
# Prerequisites (Phase 0 must already be done):
#   - bench stack up; docs/benchmark-corpus/corpus.json present
#   - backend/venv exists; .env has the GPU_* keys
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

BENCH_COMPOSE="-f docker-compose.yml -f docker-compose.gpu.yml -f docker-compose.bench.yml"
CORPUS="docs/benchmark-corpus/corpus.json"
OUTROOT="docs/engine-benchmark-results"
STATE_DIR="benchmarks/soak_state"
CKPT="$STATE_DIR/checkpoint.txt"
RESULTS="$STATE_DIR/results.tsv"
LOG="$STATE_DIR/orchestrate.log"
PY="backend/venv/bin/python"
export BENCHMARK_EMAIL="${BENCHMARK_EMAIL:-admin@example.com}"
export BENCHMARK_PASSWORD="${BENCHMARK_PASSWORD:-password}"

mkdir -p "$STATE_DIR" "$OUTROOT"
touch "$CKPT"
[[ -s "$RESULTS" ]] || echo -e "phase\tmode\tconc\tgpu\tpeak_vram_mb\tagg_rtf\tutil_pct\tstable" > "$RESULTS"

log()   { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }
done_u(){ grep -qxF "$1" "$CKPT"; }
mark()  { echo "$1" >> "$CKPT"; log "checkpoint: $1 DONE"; }
setenv(){ sed -i "s/^$1=.*/$1=$2/" .env; grep -q "^$1=" .env || echo "$1=$2" >> .env; }

n_corpus(){ "$PY" -c "import json;print(len(json.load(open('$CORPUS'))['files']))"; }

RPW=$(grep -m1 '^REDIS_PASSWORD=' .env | cut -d= -f2 | tr -d ' ')
redis_llen(){ docker exec opentranscribe-redis redis-cli -a "$RPW" --no-auth-warning LLEN "$1" 2>/dev/null | tr -d '[:space:]'; }
psql_q(){ docker exec opentranscribe-postgres psql -U postgres -d opentranscribe -t -A -c "$1" 2>/dev/null; }

# Wait until all task queues are empty (no in-flight work), up to ~timeout sec.
drain_queues(){
  local timeout=${1:-180} t=0
  while (( t < timeout )); do
    local g c; g=$(redis_llen gpu); c=$(redis_llen cpu)
    [[ "${g:-0}" == "0" && "${c:-0}" == "0" ]] && return 0
    sleep 5; t=$((t+5))
  done
  log "WARN: queues not empty after ${timeout}s (gpu=$(redis_llen gpu) cpu=$(redis_llen cpu))"
}

# Reset any orphaned task/file state left by a prior interrupted run.
reset_orphans(){
  local n; n=$(psql_q "SELECT count(*) FROM task WHERE status IN ('pending','in_progress');")
  if [[ "${n:-0}" != "0" ]]; then
    log "resetting ${n} orphaned task rows + stuck files"
    psql_q "UPDATE task SET status='failed' WHERE status IN ('pending','in_progress');
            UPDATE media_file SET status='completed' WHERE status IN ('processing','pending');
            UPDATE media_file SET active_task_id=NULL WHERE active_task_id IS NOT NULL;" >/dev/null
  fi
}

# SAFETY: refuse any destructive op unless postgres AND minio are backed by the
# bench named volumes. This makes it impossible to wipe the dev/NAS dataset even
# if the wrong stack happens to be up.
assert_bench_only(){
  local pgvol mnvol
  pgvol=$(docker inspect opentranscribe-postgres --format '{{range .Mounts}}{{.Name}} {{end}}' 2>/dev/null)
  mnvol=$(docker inspect opentranscribe-minio    --format '{{range .Mounts}}{{.Name}} {{end}}' 2>/dev/null)
  case "$pgvol" in *postgres_bench_data*) : ;; *)
    log "FATAL SAFETY: postgres not on bench volume (mounts: $pgvol). Refusing to clear data."; exit 99 ;; esac
  case "$mnvol" in *minio_bench_data*) : ;; *)
    log "FATAL SAFETY: minio not on bench volume (mounts: $mnvol). Refusing to clear data."; exit 99 ;; esac
}

# Phase 0: load the corpus ONCE into the bench deployment (like a real user
# uploading their files). If the bench DB already has completed files, just
# rebuild corpus.json from them. No per-level DB wiping — every later level
# REPROCESSES these same files (the app's normal feature), so duplicates never
# happen and nothing locks the database.
ensure_corpus(){
  local cnt; cnt=$(psql_q "SELECT count(*) FROM media_file WHERE status='completed';"); cnt=${cnt:-0}
  if [[ "$cnt" -lt 1 ]]; then
    if done_u "phase0:upload"; then
      log "Phase 0 marked done but DB empty — rerunning upload"
    fi
    log "Phase 0: bench DB empty — uploading corpus from benchmark/test_audio (one-time deployment load)"
    setenv GPU_DEVICE_ID 0; setenv GPU_CONCURRENT_REQUESTS 1; setenv ENGINE_GPU_SPLIT false
    setenv GPU_SCALE_ENABLED false; setenv GPU_SCALE_DEFAULT_WORKER 0
    restart_solo
    "$PY" scripts/soak_upload_corpus.py 2>&1 | tee -a "$LOG"
    [[ "${PIPESTATUS[0]}" -ne 0 ]] && { log "FATAL: corpus upload failed"; exit 6; }
    mark "phase0:upload"
  else
    log "Phase 0: bench DB has $cnt completed files — rebuilding corpus.json from current UUIDs"
    "$PY" scripts/soak_rebuild_corpus.py 2>&1 | tee -a "$LOG" \
      || log "WARN: corpus.json rebuild failed; using existing file"
  fi
}

# Wipe the bench deployment (only *_bench_data volumes — dev/NAS data untouched).
# Called at the very end, AFTER all metrics are saved to the host filesystem
# (docs/engine-benchmark-results + benchmarks/soak_state are NOT in bench volumes).
wipe_bench(){
  assert_bench_only
  log "Wiping bench deployment (all metrics already saved to host)"
  docker compose $BENCH_COMPOSE down --remove-orphans >>"$LOG" 2>&1
  for v in postgres minio redis opensearch flower; do
    docker volume rm "transcribe-app_${v}_bench_data" >>"$LOG" 2>&1 || true
  done
  log "Bench deployment wiped."
}

# Block until the named GPU worker container answers a celery ping.
wait_worker_ready(){
  local container=$1 t=0
  while (( t < 150 )); do
    if docker exec "$container" celery -A app.core.celery inspect ping 2>/dev/null | grep -q pong; then
      log "$container responds to celery ping"; return 0
    fi
    sleep 5; t=$((t+5))
  done
  log "FATAL: $container did not respond to celery ping within 150s"; return 1
}

# Restart the solo GPU worker cleanly: drain in-flight, reset orphans, recreate,
# wait for broker re-attach + model preload. (Force-recreate mid-task is what
# desynced the broker in the first failed run — drain first to prevent it.)
restart_solo(){
  drain_queues 120
  docker compose $BENCH_COMPOSE up -d --force-recreate --no-deps celery-worker >>"$LOG" 2>&1
  wait_worker_ready opentranscribe-celery-worker || exit 4
  log "waiting 45s for model preload"; sleep 45
  reset_orphans
  docker exec opentranscribe-celery-worker env 2>/dev/null \
    | grep -E 'GPU_DEVICE_ID|GPU_CONCURRENT_REQUESTS|ENGINE_GPU_SPLIT' | tee -a "$LOG"
}

# Parse the newest summary CSV in $1 for the row matching conc $2.
# Echoes "peak_vram_mb agg_rtf util_pct" or "NA NA NA".
parse_summary(){
  local outdir=$1 conc=$2 csv
  csv=$(ls -t "$outdir"/benchmark_summary_*.csv 2>/dev/null | head -1)
  [[ -z "$csv" ]] && { echo "NA NA NA"; return; }
  "$PY" - "$csv" "$conc" <<'PY'
import csv, sys
path, conc = sys.argv[1], int(sys.argv[2])
peak=rtf=util="NA"
with open(path) as f:
    for row in csv.DictReader(f):
        try:
            if int(row["batch_size"]) == conc:
                peak=row.get("vram_peak_mb","NA"); rtf=row.get("throughput_audio_hrs_per_wall_hr","NA"); util=row.get("gpu_util_pct_avg","NA")
                break
        except (ValueError, KeyError):
            continue
    else:
        # fall back to last data row
        f.seek(0); rows=list(csv.DictReader(f))
        if rows:
            r=rows[-1]; peak=r.get("vram_peak_mb","NA"); rtf=r.get("throughput_audio_hrs_per_wall_hr","NA"); util=r.get("gpu_util_pct_avg","NA")
print(peak, rtf, util)
PY
}

oom_check(){
  if docker logs opentranscribe-celery-worker --tail=400 2>&1 | grep -iqE 'out of memory|cuda.*oom'; then
    return 0; fi; return 1
}

# run_level PHASE MODE CONC GPU PROFILE [extra parallel args...]
# CONC = worker GPU_CONCURRENT_REQUESTS (the independent variable). Each level
# clears bench data, uploads the FULL corpus (real user flow), and measures how
# the backend processes 58h of audio at in-flight concurrency CONC.
run_level(){
  local phase=$1 mode=$2 conc=$3 gpu=$4 profile=$5; shift 5
  local unit="${phase}:conc${conc}"
  if done_u "$unit"; then log "skip $unit (already done)"; return 0; fi
  setenv GPU_CONCURRENT_REQUESTS "$conc"
  restart_solo
  local outdir; outdir="$OUTROOT/${phase}_conc${conc}_$(date +%Y%m%d_%H%M%S)"
  log "RUN $unit  mode=$mode gpu=$gpu profile=$profile (reprocess full corpus, conc=$conc) -> $outdir"
  "$PY" scripts/benchmark_parallel.py --corpus-file "$CORPUS" --profile "$profile" \
      --batches "$N" --gpu-id "$gpu" --cooldown 0 --output "$outdir" "$@" \
      2>&1 | tee -a "$LOG"
  local rc=${PIPESTATUS[0]}
  if [[ "$rc" -ne 0 ]]; then
    log "ABORT: benchmark_parallel exited $rc at $unit — NOT checkpointing. Fix and resume."
    exit 3
  fi
  local stable=yes
  oom_check && { log "OOM detected at $unit"; stable=oom; }
  read -r peak rtf util < <(parse_summary "$outdir" "$N")
  if [[ "$rtf" == "NA" || "$peak" == "NA" ]]; then
    log "ABORT: no metrics parsed at $unit (rtf=$rtf peak=$peak) — pipeline likely not processing. NOT checkpointing."
    exit 3
  fi
  echo -e "${phase}\t${mode}\t${conc}\t${gpu}\t${peak}\t${rtf}\t${util}\t${stable}" >> "$RESULTS"
  log "result $unit  peak=${peak}MB rtf=${rtf} util=${util}% stable=${stable}"
  mark "$unit"
  [[ "$stable" == "oom" ]] && return 2 || return 0
}

best_from_results(){  # best_from_results PHASE MAX_VRAM MIN_RTF
  "$PY" - "$RESULTS" "$1" "$2" "$3" <<'PY'
import csv, sys
path, phase, maxv, minr = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4])
best_conc, best_rtf = 1, -1.0
with open(path) as f:
    for r in csv.DictReader(f, delimiter="\t"):
        if r["phase"]!=phase or r["stable"]!="yes": continue
        try: peak=float(r["peak_vram_mb"]); rtf=float(r["agg_rtf"]); conc=int(r["conc"])
        except ValueError: continue
        if peak<=maxv and rtf>=minr and rtf>best_rtf:
            best_rtf, best_conc = rtf, conc
print(best_conc)
PY
}

# ── Preconditions + Phase 0 (one-time corpus load) ───────────────────────────
# Always-true instrumentation env
setenv ENABLE_BENCHMARK_TIMING true
setenv ENABLE_VRAM_PROFILING true
setenv WHISPER_MODEL large-v3-turbo

log "preflight: checking backend health"
curl -sf http://localhost:5174/health >/dev/null 2>&1 || { log "FATAL: backend not healthy — is the bench stack up?"; exit 1; }

ensure_corpus
[[ -f "$CORPUS" ]] || { log "FATAL: $CORPUS missing after Phase 0"; exit 1; }
N=$(n_corpus); log "==== SOAK ORCHESTRATOR START — corpus=$N files ===="
reset_orphans

# ── Phase 1: A6000 solo (GPU 0) ──────────────────────────────────────────────
if ! done_u "phase1:complete"; then
  log "===== PHASE 1: A6000 solo (GPU 0) ====="
  setenv GPU_SCALE_ENABLED false; setenv GPU_SCALE_DEFAULT_WORKER 0
  setenv ENGINE_GPU_SPLIT false; setenv GPU_DEVICE_ID 0
  for c in 1 4 8 10 12 14 16 20 24; do
    [[ "$c" -gt "$N" ]] && { log "skip conc=$c (> corpus $N)"; continue; }
    run_level phase1 a6000_solo "$c" 0 mixed --shuffle || { log "Phase 1 stop at conc=$c"; break; }
  done
  mark "phase1:complete"
fi
A6000_BEST=$(best_from_results phase1 49500 40); log "A6000_BEST=$A6000_BEST"

# ── Phase 2: 3080 Ti solo (GPU 1) ────────────────────────────────────────────
if ! done_u "phase2:complete"; then
  log "===== PHASE 2: 3080 Ti solo (GPU 1) ====="
  setenv GPU_SCALE_ENABLED false; setenv ENGINE_GPU_SPLIT false; setenv GPU_DEVICE_ID 1
  for c in 1 2 3 4; do
    run_level phase2 ti_solo "$c" 1 by_duration --cooldown 10 || { log "Phase 2 stop at conc=$c"; break; }
  done
  mark "phase2:complete"
fi
TI_BEST=$(best_from_results phase2 11500 20); log "TI_BEST=$TI_BEST"

# ── Phase 3: dual-GPU --gpu-scale ────────────────────────────────────────────
if ! done_u "phase3:complete"; then
  log "===== PHASE 3: dual-GPU --gpu-scale (A6000=$A6000_BEST, TI=$TI_BEST) ====="
  setenv GPU_SCALE_ENABLED true; setenv GPU_SCALE_DEFAULT_WORKER 1; setenv GPU_DEVICE_ID 1
  setenv GPU_CONCURRENT_REQUESTS "$TI_BEST"; setenv GPU_SCALE_DEVICE_ID 0
  setenv GPU_SCALE_WORKERS "$A6000_BEST"; setenv ENGINE_GPU_SPLIT false
  drain_queues 120
  docker compose $BENCH_COMPOSE down --remove-orphans >>"$LOG" 2>&1
  COMPOSE_PROFILES=gpu-scale docker compose $BENCH_COMPOSE -f docker-compose.gpu-scale.yml -f docker-compose.bench-gpu.yml up -d >>"$LOG" 2>&1
  sleep 10
  # The gpu-scaled service uses `scale:`, so Compose ignores container_name and
  # names it <project>-celery-worker-gpu-scaled. Resolve the real name.
  scaled_name=$(docker ps --format '{{.Names}}' | grep -m1 'celery-worker-gpu-scaled')
  log "resolved gpu-scaled container: ${scaled_name:-NOT FOUND}"
  wait_worker_ready "${scaled_name:-transcribe-app-celery-worker-gpu-scaled}" || exit 4
  wait_worker_ready opentranscribe-celery-worker || exit 4
  log "waiting 45s for model preload on both cards"; sleep 45; reset_orphans
  docker ps --format '{{.Names}}' | grep -E 'celery-worker' | tee -a "$LOG"
  outdir="$OUTROOT/phase3_dual_scale_$(date +%Y%m%d_%H%M%S)"
  "$PY" scripts/benchmark_parallel.py --corpus-file "$CORPUS" --profile mixed --shuffle \
      --batches "$N" --gpu-id 0 --cooldown 0 --output "$outdir" 2>&1 | tee -a "$LOG"
  [[ "${PIPESTATUS[0]}" -ne 0 ]] && { log "ABORT: phase3 benchmark failed"; exit 3; }
  read -r peak rtf util < <(parse_summary "$outdir" "$N")
  [[ "$rtf" == "NA" ]] && { log "ABORT: phase3 produced no metrics"; exit 3; }
  echo -e "phase3\tdual_gpu_scale\t${N}\t0+1\t${peak}\t${rtf}\t${util}\tyes" >> "$RESULTS"
  mark "phase3:complete"
fi

# ── Phase 4: --gpu-split ─────────────────────────────────────────────────────
if ! done_u "phase4:complete"; then
  log "===== PHASE 4: --gpu-split (transcribe GPU0, diarize GPU1) ====="
  setenv GPU_SCALE_ENABLED false; setenv GPU_SCALE_DEFAULT_WORKER 0
  setenv ENGINE_GPU_SPLIT true; setenv GPU_TRANSCRIBE_DEVICE_ID 0; setenv GPU_DIARIZE_DEVICE_ID 1
  setenv GPU_CONCURRENT_REQUESTS "$TI_BEST"
  drain_queues 120
  docker compose $BENCH_COMPOSE down --remove-orphans >>"$LOG" 2>&1
  COMPOSE_PROFILES=gpu-split docker compose $BENCH_COMPOSE -f docker-compose.bench-gpu.yml up -d >>"$LOG" 2>&1
  wait_worker_ready opentranscribe-celery-worker-gpu-transcribe || exit 4
  wait_worker_ready opentranscribe-celery-worker-gpu-diarize || exit 4
  log "waiting 45s for model preload"; sleep 45; reset_orphans
  docker ps --format '{{.Names}}' | grep -E '^opentranscribe-celery-worker-gpu-' | tee -a "$LOG"
  outdir="$OUTROOT/phase4_gpusplit_$(date +%Y%m%d_%H%M%S)"
  "$PY" scripts/benchmark_parallel.py --corpus-file "$CORPUS" --profile mixed --shuffle \
      --batches "$N" --gpu-id 0 --cooldown 0 --output "$outdir" 2>&1 | tee -a "$LOG"
  [[ "${PIPESTATUS[0]}" -ne 0 ]] && { log "ABORT: phase4 benchmark failed"; exit 3; }
  read -r peak rtf util < <(parse_summary "$outdir" "$N")
  [[ "$rtf" == "NA" ]] && { log "ABORT: phase4 produced no metrics"; exit 3; }
  echo -e "phase4\tgpu_split\t${N}\t0+1\t${peak}\t${rtf}\t${util}\tyes" >> "$RESULTS"
  mark "phase4:complete"
fi

# ── Phase 5: duration curve (GPU 0 solo, conc=1 sequential) ──────────────────
if ! done_u "phase5:complete"; then
  log "===== PHASE 5: duration curve (GPU 0 solo, conc=1, sequential) ====="
  setenv ENGINE_GPU_SPLIT false; setenv GPU_SCALE_ENABLED false
  setenv GPU_SCALE_DEFAULT_WORKER 0; setenv GPU_DEVICE_ID 0; setenv GPU_CONCURRENT_REQUESTS 1
  drain_queues 120
  docker compose $BENCH_COMPOSE down --remove-orphans >>"$LOG" 2>&1
  docker compose $BENCH_COMPOSE up -d >>"$LOG" 2>&1
  wait_worker_ready opentranscribe-celery-worker || exit 4
  log "waiting 45s for model preload"; sleep 45; reset_orphans
  outdir="$OUTROOT/phase5_duration_curve_$(date +%Y%m%d_%H%M%S)"
  "$PY" scripts/benchmark_parallel.py --corpus-file "$CORPUS" --profile by_duration --sequential \
      --gpu-id 0 --cooldown 10 --output "$outdir" 2>&1 | tee -a "$LOG"
  [[ "${PIPESTATUS[0]}" -ne 0 ]] && { log "ABORT: phase5 benchmark failed"; exit 3; }
  mark "phase5:complete"
fi

log "==== ALL PHASES COMPLETE — metrics captured ===="
log "A6000_BEST=$A6000_BEST  TI_BEST=$TI_BEST"
log "Results: $RESULTS"
log "Raw CSVs: $OUTROOT/"
column -t -s$'\t' "$RESULTS" | tee -a "$LOG"

# Final step: wipe the bench deployment now that every metric is on the host.
wipe_bench
log "==== SOAK ORCHESTRATOR DONE ===="
