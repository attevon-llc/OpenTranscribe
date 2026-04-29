# Engine Benchmark Run Status

## Phase 1a — Combined Entry Point (Engine package + pipeline parity gate)

**Status**: PENDING FIRST RUN

### How to run the parity gate

```bash
# Inside celery-worker container
docker exec -it opentranscribe-celery-worker \
  python /app/scripts/benchmark_engine_compare.py \
    --audio-dir /app/benchmark/test_audio \
    --max-files 2 \
    --output /tmp/engine_compare_phase1a.csv

# Copy result out
docker cp opentranscribe-celery-worker:/tmp/engine_compare_phase1a.csv \
  docs/engine-benchmark-results/engine_compare_phase1a.csv
```

### Gate criteria

| Check | Required |
|---|---|
| segments | Byte-equal (JSON-stable) |
| language | Identical string |
| overlap_info | Equal (absent/empty both treated as `{}`) |
| native_speaker_embeddings | Within 1e-6 absolute tolerance |
| Exit code | 0 (all PASS or SKIP, zero FAIL) |

### Results

*No runs recorded yet.*

---

## Phase 1b — Split-stage Celery wire-up

**Status**: PENDING

---

## Phase 2 — Baseline benchmark sweep

**Status**: PENDING
