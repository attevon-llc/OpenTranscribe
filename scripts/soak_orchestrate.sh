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
ANCHOR_UUID="77e78a8c-4e0a-4995-9aa1-c1e9e0db3e15"   # bench 0.5h synthetic (0.5h_1899s.wav)
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

# Restart the solo GPU worker (bench stack) and wait for model preload.
restart_solo(){
  docker compose $BENCH_COMPOSE up -d --force-recreate --no-deps celery-worker >>"$LOG" 2>&1
  log "worker recreated; waiting 75s for model preload"
  sleep 75
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
run_level(){
  local phase=$1 mode=$2 conc=$3 gpu=$4 profile=$5; shift 5
  local unit="${phase}:conc${conc}"
  if done_u "$unit"; then log "skip $unit (already done)"; return 0; fi
  setenv GPU_CONCURRENT_REQUESTS "$conc"
  restart_solo
  local outdir; outdir="$OUTROOT/${phase}_conc${conc}_$(date +%Y%m%d_%H%M%S)"
  log "RUN $unit  mode=$mode gpu=$gpu profile=$profile -> $outdir"
  "$PY" scripts/benchmark_parallel.py --corpus-file "$CORPUS" --profile "$profile" \
      --batches "$conc" --gpu-id "$gpu" --cooldown 0 --output "$outdir" "$@" \
      2>&1 | tee -a "$LOG"
  local stable=yes
  oom_check && { log "OOM detected at $unit"; stable=oom; }
  read -r peak rtf util < <(parse_summary "$outdir" "$conc")
  echo -e "${phase}\t${mode}\t${conc}\t${gpu}\t${peak}\t${rtf}\t${util}\t${stable}" >> "$RESULTS"
  log "result $unit  peak=${peak}MB rtf=${rtf} util=${util}% stable=${stable}"
  mark "$unit"
  [[ "$stable" == "oom" ]] && return 2 || return 0
}

run_e2e(){
  local phase=$1 gpu=$2; local unit="${phase}:e2e"
  if done_u "$unit"; then log "skip $unit"; return 0; fi
  setenv GPU_CONCURRENT_REQUESTS 1; restart_solo
  "$PY" scripts/benchmark_e2e.py --file-uuid "$ANCHOR_UUID" --iterations 3 --detailed \
      --output "$OUTROOT/e2e_${phase}_$(date +%Y%m%d).csv" 2>&1 | tee -a "$LOG" || \
      log "WARN: e2e baseline failed for $phase (anchor may be absent) — continuing"
  mark "$unit"
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

# ── Preconditions ────────────────────────────────────────────────────────────
[[ -f "$CORPUS" ]] || { log "FATAL: $CORPUS missing — run Phase 0 first."; exit 1; }
N=$(n_corpus); log "==== SOAK ORCHESTRATOR START — corpus=$N files ===="

# Always-true env
setenv ENABLE_BENCHMARK_TIMING true
setenv ENABLE_VRAM_PROFILING true
setenv WHISPER_MODEL large-v3-turbo

# ── Phase 1: A6000 solo (GPU 0) ──────────────────────────────────────────────
if ! done_u "phase1:complete"; then
  log "===== PHASE 1: A6000 solo (GPU 0) ====="
  setenv GPU_SCALE_ENABLED false; setenv GPU_SCALE_DEFAULT_WORKER 0
  setenv ENGINE_GPU_SPLIT false; setenv GPU_DEVICE_ID 0
  run_e2e phase1 0
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
  run_e2e phase2 1
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
  docker compose $BENCH_COMPOSE down --remove-orphans >>"$LOG" 2>&1
  COMPOSE_PROFILES=gpu-scale docker compose $BENCH_COMPOSE -f docker-compose.gpu-scale.yml up -d >>"$LOG" 2>&1
  sleep 80
  docker ps --format '{{.Names}}' | grep -E '^opentranscribe-celery-worker' | tee -a "$LOG"
  outdir="$OUTROOT/phase3_dual_scale_$(date +%Y%m%d_%H%M%S)"
  "$PY" scripts/benchmark_parallel.py --corpus-file "$CORPUS" --profile mixed --shuffle \
      --batches "$N" --gpu-id 0 --cooldown 0 --output "$outdir" 2>&1 | tee -a "$LOG"
  read -r peak rtf util < <(parse_summary "$outdir" "$N")
  echo -e "phase3\tdual_gpu_scale\t${N}\t0+1\t${peak}\t${rtf}\t${util}\tyes" >> "$RESULTS"
  mark "phase3:complete"
fi

# ── Phase 4: --gpu-split ─────────────────────────────────────────────────────
if ! done_u "phase4:complete"; then
  log "===== PHASE 4: --gpu-split (transcribe GPU0, diarize GPU1) ====="
  setenv GPU_SCALE_ENABLED false; setenv GPU_SCALE_DEFAULT_WORKER 0
  setenv ENGINE_GPU_SPLIT true; setenv GPU_TRANSCRIBE_DEVICE_ID 0; setenv GPU_DIARIZE_DEVICE_ID 1
  setenv GPU_CONCURRENT_REQUESTS "$TI_BEST"
  docker compose $BENCH_COMPOSE down --remove-orphans >>"$LOG" 2>&1
  COMPOSE_PROFILES=gpu-split docker compose $BENCH_COMPOSE up -d >>"$LOG" 2>&1
  sleep 80
  docker ps --format '{{.Names}}' | grep -E '^opentranscribe-celery-worker-gpu-' | tee -a "$LOG"
  outdir="$OUTROOT/phase4_gpusplit_$(date +%Y%m%d_%H%M%S)"
  "$PY" scripts/benchmark_parallel.py --corpus-file "$CORPUS" --profile mixed --shuffle \
      --batches "$N" --gpu-id 0 --cooldown 0 --output "$outdir" 2>&1 | tee -a "$LOG"
  read -r peak rtf util < <(parse_summary "$outdir" "$N")
  echo -e "phase4\tgpu_split\t${N}\t0+1\t${peak}\t${rtf}\t${util}\tyes" >> "$RESULTS"
  mark "phase4:complete"
fi

# ── Phase 5: duration curve (GPU 0 solo, conc=1 sequential) ──────────────────
if ! done_u "phase5:complete"; then
  log "===== PHASE 5: duration curve (GPU 0 solo, conc=1, sequential) ====="
  setenv ENGINE_GPU_SPLIT false; setenv GPU_SCALE_ENABLED false
  setenv GPU_SCALE_DEFAULT_WORKER 0; setenv GPU_DEVICE_ID 0; setenv GPU_CONCURRENT_REQUESTS 1
  docker compose $BENCH_COMPOSE down --remove-orphans >>"$LOG" 2>&1
  docker compose $BENCH_COMPOSE up -d >>"$LOG" 2>&1; sleep 75
  outdir="$OUTROOT/phase5_duration_curve_$(date +%Y%m%d_%H%M%S)"
  "$PY" scripts/benchmark_parallel.py --corpus-file "$CORPUS" --profile by_duration --sequential \
      --gpu-id 0 --cooldown 10 --output "$outdir" 2>&1 | tee -a "$LOG"
  mark "phase5:complete"
fi

log "==== SOAK ORCHESTRATOR COMPLETE ===="
log "A6000_BEST=$A6000_BEST  TI_BEST=$TI_BEST"
log "Results: $RESULTS"
column -t -s$'\t' "$RESULTS" | tee -a "$LOG"
