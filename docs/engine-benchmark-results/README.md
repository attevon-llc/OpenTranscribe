# Engine Benchmark Results

This directory stores benchmark CSVs from the engine optimization work.

## Files

| File | Phase | Description |
|------|-------|-------------|
| `RUN_STATUS.md` | All | Current run status and how-to instructions |
| `engine_compare_phase1a.csv` | 1a | Parity gate: Engine vs TranscriptionPipeline |
| `engine_single_*.csv` | 2 | Per-stage latency (preprocess / GPU / finalize) |
| `engine_queue_*.csv` | 2 | Throughput at queue_depth ≥ 3 |

## How to run

See `RUN_STATUS.md` for exact commands.

The scripts live in `scripts/`:
- `benchmark_engine_compare.py` — parity gate
- `benchmark_engine_single.py` — per-stage latency
- `benchmark_engine_queue.py` — queue throughput + GPU idle measurement

## GPU inventory (this machine)

| Slot | GPU | VRAM | Typical use |
|------|-----|------|-------------|
| 0 | RTX A6000 | 49 GB | Primary transcription worker |
| 1 | RTX 3080 Ti | 12 GB | Hybrid-mode baseline |
| 2 | RTX A6000 | 49 GB | GPU-scaled worker |

## Gate criteria

Phase 2 results are considered acceptable if:
- Stage 1 (preprocess): < 30 s per file
- Stage 2 GPU (transcribe + diarize): ≥ 20× realtime factor
- Stage 3 (finalize): < 5 s per file
- GPU idle between tasks at concurrency = 3: < 5 s
