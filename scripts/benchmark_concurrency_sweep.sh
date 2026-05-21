#!/bin/bash
# GPU concurrency sweep benchmark — OpenTranscribe
#
# Cycles through GPU_CONCURRENT_REQUESTS levels, restarts the GPU worker for
# each, runs the parallel benchmark via the fixed corpus, and saves results.
# Uses the shared-weight ModelManager architecture: model weights load once;
# per-task cost is activation buffers only (~4 GB measured, may be less at
# high concurrency due to CTranslate2 activation-pool reuse / VRAM plateau).
#
# Usage:
#   source backend/venv/bin/activate
#   bash scripts/benchmark_concurrency_sweep.sh [--gpu-id N] [--max-conc N] [--profile NAME]
#
# Prerequisites:
#   - All services running (./opentr.sh start dev)
#   - ENABLE_BENCHMARK_TIMING=true in .env
#   - ENABLE_VRAM_PROFILING=true in .env
#   - backend/venv activated
#   - docs/benchmark-corpus/corpus.json present
#
# Defaults: GPU_ID=2 (slot 2 A6000), CONCURRENCY_LEVELS="1 4 8 10 12 14 16 20 24"
# Stop criteria per docs/gpu-concurrency-soak-test-plan.md:
#   A6000: stop if VRAM > 48.5 GB (plateau broken) or RTF drops below 40x
#   3080 Ti (--gpu-id 1): stop if VRAM > 11.5 GB (95% of 12 GB)

set -uo pipefail

# ── Defaults ────────────────────────────────────────────────────────────────
GPU_ID=2
PROFILE="by_duration"
COOLDOWN=20
CORPUS="docs/benchmark-corpus/corpus.json"

# Levels to sweep. Known-stable from BENCHMARK_RESULTS.md: 1,4,8,10,12.
# New territory starts at 14.  Use --max-conc to cap early if needed.
CONCURRENCY_LEVELS="1 4 8 10 12 14 16 20 24"
MAX_CONC=24

# ── Argument parsing ─────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --gpu-id)   GPU_ID="$2";    shift 2 ;;
        --max-conc) MAX_CONC="$2";  shift 2 ;;
        --profile)  PROFILE="$2";   shift 2 ;;
        --corpus)   CORPUS="$2";    shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

# Build final level list, capped at MAX_CONC
LEVELS=()
for c in $CONCURRENCY_LEVELS; do
    [[ "$c" -le "$MAX_CONC" ]] && LEVELS+=("$c")
done

OUTPUT_BASE="benchmarks/sweep_gpu${GPU_ID}_$(date +%Y%m%d_%H%M%S)"
LOG="$OUTPUT_BASE/sweep.log"

mkdir -p "$OUTPUT_BASE"

echo "================================================================" | tee "$LOG"
echo "GPU CONCURRENCY SWEEP — OpenTranscribe" | tee -a "$LOG"
echo "Started:  $(date)" | tee -a "$LOG"
echo "GPU:      $GPU_ID  (CUDA_VISIBLE_DEVICES=$GPU_ID)" | tee -a "$LOG"
echo "Profile:  $PROFILE" | tee -a "$LOG"
echo "Corpus:   $CORPUS" | tee -a "$LOG"
echo "Levels:   ${LEVELS[*]}" | tee -a "$LOG"
echo "Output:   $OUTPUT_BASE" | tee -a "$LOG"
echo "================================================================" | tee -a "$LOG"

# ── Helpers ──────────────────────────────────────────────────────────────────

gpu_vram_mb() {
    nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits \
        --id="$GPU_ID" 2>/dev/null || echo "?"
}

gpu_vram_total_mb() {
    nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits \
        --id="$GPU_ID" 2>/dev/null || echo "?"
}

set_concurrency() {
    local conc=$1
    echo "" | tee -a "$LOG"
    echo "[$(date +%H:%M:%S)] Setting GPU_CONCURRENT_REQUESTS=$conc ..." | tee -a "$LOG"

    sed -i "s/^GPU_CONCURRENT_REQUESTS=.*/GPU_CONCURRENT_REQUESTS=$conc/" .env

    # Restart all services so the worker picks up the new env var.
    # opentr.sh start dev waits for healthy containers.
    ./opentr.sh stop > /dev/null 2>&1
    sleep 3
    ./opentr.sh start dev > /dev/null 2>&1

    echo "[$(date +%H:%M:%S)] Waiting 20s for model preload ..." | tee -a "$LOG"
    sleep 20

    local actual
    actual=$(docker exec opentranscribe-celery-worker env 2>/dev/null \
        | grep GPU_CONCURRENT_REQUESTS | cut -d= -f2 || echo "?")
    echo "[$(date +%H:%M:%S)] Worker ready: GPU_CONCURRENT_REQUESTS=$actual" | tee -a "$LOG"

    local vram
    vram=$(gpu_vram_mb)
    echo "[$(date +%H:%M:%S)] VRAM baseline after model load: ${vram} MiB" | tee -a "$LOG"
}

run_benchmark() {
    local conc=$1
    local out_dir="$OUTPUT_BASE/conc${conc}"

    echo "" | tee -a "$LOG"
    echo "================================================================" | tee -a "$LOG"
    echo "[$(date +%H:%M:%S)] BENCHMARK: concurrent=$conc  profile=$PROFILE" | tee -a "$LOG"
    echo "================================================================" | tee -a "$LOG"

    BENCHMARK_EMAIL="${BENCHMARK_EMAIL:-admin@example.com}" \
    BENCHMARK_PASSWORD="${BENCHMARK_PASSWORD:-password}" \
    python scripts/benchmark_parallel.py \
        --corpus-file "$CORPUS" \
        --profile "$PROFILE" \
        --shuffle \
        --batches "$conc" \
        --gpu-id "$GPU_ID" \
        --output "$out_dir" \
        --cooldown 10 \
        2>&1 | tee -a "$LOG"

    local vram_after
    vram_after=$(gpu_vram_mb)
    echo "[$(date +%H:%M:%S)] VRAM after batch: ${vram_after} MiB" | tee -a "$LOG"

    # Plateau check: warn if VRAM jumped more than 1 GB above the known plateau
    if [[ "$vram_after" =~ ^[0-9]+$ ]] && [[ "$vram_after" -gt 49500 ]]; then
        echo "[$(date +%H:%M:%S)] ⚠ VRAM exceeded 49.5 GB — plateau may be broken." \
            "Review before continuing." | tee -a "$LOG"
    fi
}

run_single_baseline() {
    echo "" | tee -a "$LOG"
    echo "================================================================" | tee -a "$LOG"
    echo "[$(date +%H:%M:%S)] BASELINE: single file, 3 iterations (benchmark_e2e.py)" | tee -a "$LOG"
    echo "================================================================" | tee -a "$LOG"

    # Use the synthetic 0.5h anchor file from the corpus (tier 2, index 4)
    local anchor_uuid="ce471b5a-b4ae-45e5-8905-af7420d50f79"

    BENCHMARK_EMAIL="${BENCHMARK_EMAIL:-admin@example.com}" \
    BENCHMARK_PASSWORD="${BENCHMARK_PASSWORD:-password}" \
    python scripts/benchmark_e2e.py \
        --file-uuid "$anchor_uuid" \
        --iterations 3 \
        --detailed \
        --output "$OUTPUT_BASE/e2e_baseline.csv" \
        2>&1 | tee -a "$LOG"

    local vram
    vram=$(gpu_vram_mb)
    echo "[$(date +%H:%M:%S)] VRAM after baseline: ${vram} MiB" | tee -a "$LOG"
}

# ── Main ─────────────────────────────────────────────────────────────────────

echo "" | tee -a "$LOG"
echo "[$(date +%H:%M:%S)] GPU $GPU_ID total VRAM: $(gpu_vram_total_mb) MiB" | tee -a "$LOG"
echo "[$(date +%H:%M:%S)] GPU $GPU_ID idle VRAM usage: $(gpu_vram_mb) MiB" | tee -a "$LOG"

# Phase 1: single-file e2e baseline at conc=1
set_concurrency 1
run_single_baseline

echo "" | tee -a "$LOG"
echo "[$(date +%H:%M:%S)] Cooling down ${COOLDOWN}s before concurrency sweep ..." | tee -a "$LOG"
sleep "$COOLDOWN"

# Phase 2: concurrency sweep
for conc in "${LEVELS[@]}"; do
    set_concurrency "$conc"
    sleep 5
    run_benchmark "$conc"

    echo "" | tee -a "$LOG"
    echo "[$(date +%H:%M:%S)] Cooling down ${COOLDOWN}s ..." | tee -a "$LOG"
    sleep "$COOLDOWN"
done

# Phase 3: duration curve at conc=1 (sequential, all 16 corpus files)
echo "" | tee -a "$LOG"
echo "================================================================" | tee -a "$LOG"
echo "[$(date +%H:%M:%S)] DURATION CURVE: all 16 corpus files, sequential" | tee -a "$LOG"
echo "================================================================" | tee -a "$LOG"

set_concurrency 1
sleep 5

BENCHMARK_EMAIL="${BENCHMARK_EMAIL:-admin@example.com}" \
BENCHMARK_PASSWORD="${BENCHMARK_PASSWORD:-password}" \
python scripts/benchmark_parallel.py \
    --corpus-file "$CORPUS" \
    --profile by_duration \
    --sequential \
    --gpu-id "$GPU_ID" \
    --output "$OUTPUT_BASE/duration_curve" \
    --cooldown 10 \
    2>&1 | tee -a "$LOG"

# ── Summary ──────────────────────────────────────────────────────────────────
echo "" | tee -a "$LOG"
echo "================================================================" | tee -a "$LOG"
echo "SWEEP COMPLETE" | tee -a "$LOG"
echo "Finished: $(date)" | tee -a "$LOG"
echo "Results:  $OUTPUT_BASE/" | tee -a "$LOG"
echo "================================================================" | tee -a "$LOG"

echo "" | tee -a "$LOG"
echo "CONCURRENCY SUMMARY" | tee -a "$LOG"
echo "-------------------" | tee -a "$LOG"
for conc in "${LEVELS[@]}"; do
    dir="$OUTPUT_BASE/conc${conc}"
    for csv_file in "$dir"/benchmark_summary_*.csv; do
        if [[ -f "$csv_file" ]]; then
            echo "concurrent=$conc:" | tee -a "$LOG"
            tee -a "$LOG" < "$csv_file"
            echo "" | tee -a "$LOG"
            break
        fi
    done
done

echo "" | tee -a "$LOG"
echo "Next steps:" | tee -a "$LOG"
echo "  1. Fill in result tables in docs/gpu-concurrency-soak-test-plan.md" | tee -a "$LOG"
echo "  2. Update _auto_concurrent() formula+cap in backend/app/transcription/config.py" | tee -a "$LOG"
echo "  3. Append new rows to docs/BENCHMARK_RESULTS.md concurrency table" | tee -a "$LOG"
