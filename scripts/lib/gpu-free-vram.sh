#!/usr/bin/env bash
# Sourced by scripts/e2e/update-visual-baselines.sh: refuse a GPU stack start when the card is full.
# Needs the caller's log(), colour vars, DRY_RUN and EXIT_PRECONDITION.

# The capture stack transcribes the seed corpus on a real GPU. 2026-11-14: another idle
# compose stack held 7.3 GB of the 12 GB card, every seed file died with CUDA OOM, and the
# wrapper only noticed ~15 minutes later when it asserted the corpus. Refuse before start.
# Need = the measured peaks in backend/app/transcription/vram_budget.py (#369 calibration,
# RTX 3080 Ti): resident Whisper+PyAnnote 1090 MB + ASR at batch 16 (100+150*16 = 2500 MB)
# + diarization 1200 MB = 4790 MB, rounded up. Override: OT_VISUAL_MIN_FREE_VRAM_MB.
MIN_FREE_VRAM_MB="${OT_VISUAL_MIN_FREE_VRAM_MB:-5000}"

# Best effort: names the compose projects whose processes hold memory on the card.
_gpu_holder_projects() {
    local idx="$1" pid mem cid proj out=""
    while IFS=', ' read -r pid mem; do
        [[ "$pid" =~ ^[0-9]+$ ]] || continue
        cid="$(grep -oE '[0-9a-f]{64}' "/proc/${pid}/cgroup" 2>/dev/null | head -n1 || true)"
        [[ -n "$cid" ]] || continue
        proj="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$cid" 2>/dev/null || true)"
        out+="${proj:-container ${cid:0:12}} (${mem} MiB); "
    done < <(nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader,nounits -i "$idx" 2>/dev/null || true)
    printf '%s' "$out"
}

# assert_gpu_has_free_vram IDX  ->  0 enough or unmeasurable, 3 too little (dry run: reports only)
assert_gpu_has_free_vram() {
    local idx="$1" free holders
    if ! command -v nvidia-smi >/dev/null 2>&1; then
        log "${YELLOW}(GPU free-memory check skipped: nvidia-smi not on PATH)${NC}"
        return 0
    fi
    free="$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits -i "$idx" 2>/dev/null | tr -d ' \n' || true)"
    if ! [[ "$free" =~ ^[0-9]+$ ]]; then
        log "${YELLOW}(GPU ${idx} free memory unreadable; check skipped)${NC}"
        return 0
    fi
    log "GPU ${idx}: ${free} MiB free, capture stack needs ${MIN_FREE_VRAM_MB} MiB"
    (( free >= MIN_FREE_VRAM_MB )) && return 0
    holders="$(_gpu_holder_projects "$idx")"
    log "${RED}❌ GPU ${idx} has only ${free} MiB free (need ${MIN_FREE_VRAM_MB}). Held by: ${holders:-unknown}${NC}"
    log "   Stop that stack yourself (this script never stops anything) or pass --gpu-device N."
    if $DRY_RUN; then
        log "${YELLOW}(dry run — would refuse with exit ${EXIT_PRECONDITION})${NC}"
        return 0
    fi
    return "$EXIT_PRECONDITION"
}
