# Engine Benchmark Results

Output of the end-to-end engine-optimization benchmark, run with:

```bash
./opentr.sh bench all --smoke    # validate the whole pipeline end-to-end (minutes)
./opentr.sh bench all --quick    # ~10-15h subset (repeatable regression)
./opentr.sh bench all --full     # ~58h corpus (paper-quality)
./opentr.sh bench collate         # aggregate -> master_results.csv + summary.md
```

Each phase-level runs in full isolation: a fresh `otbench` stack comes up, the corpus
is uploaded like a real user, processed, metrics are collected, then the stack is torn
down (`down -v`) before the next level. The orchestrator and phase list live in
`scripts/run_benchmark.py`; the workload driver + metrics are in
`scripts/benchmark_parallel.py`; the collator is `scripts/collate_benchmark.py`.

## Layout

| Path | Description |
|------|-------------|
| `<phase>_conc<N>/` | One directory per phase-level (e.g. `a6000_solo_conc8/`) |
| `<phase>_conc<N>/metrics.json` | Machine-readable per-level summary (collator input) |
| `<phase>_conc<N>/benchmark_summary_*.csv` | Per-batch aggregates incl. per-stage p50/p95 |
| `<phase>_conc<N>/benchmark_files_*.csv` | Per-file wall time + pipeline stage timings |
| `<phase>_conc<N>/benchmark_vram_*.csv` | Monitored-GPU VRAM/util/temp timeline |
| `<phase>_conc<N>/benchmark_cpu_*.csv` | Worker CPU% + RAM timeline (docker stats) |
| `<phase>_conc<N>/gpu_all.csv` | All-GPU VRAM/util/temp/power timeline (both cards) |
| `results.tsv` | Master append-log: one row per level (rtf, vram, cpu, ram, stable) |
| `master_results.csv` | Collated full metric table (all levels) |
| `summary.md` | Collated whitepaper-ready tables grouped by phase |
| `corpus_<tier>.json` | The reconciled corpus subset used for the run |

## Phases

`a6000_solo` (GPU 0 concurrency sweep) · `ti_solo` (GPU 1 sweep) ·
`dual_gpu_scale` (both cards, gpu-scale) · `gpu_split` (transcribe GPU 0 / diarize GPU 1) ·
`duration_curve` (sequential per-file RTF vs length).

Metrics are regenerable — `--fresh` wipes prior level dirs + aggregates for a clean run.

## GPU inventory (this machine)

| Slot | GPU | VRAM | Benchmark role |
|------|-----|------|----------------|
| 0 | RTX A6000 | 49 GB | `a6000_solo`, gpu-scale primary, gpu-split transcribe |
| 1 | RTX 3080 Ti | 12 GB | `ti_solo`, gpu-scale secondary, gpu-split diarize |
| 2 | RTX A6000 | 49 GB | LLM (vLLM) — never touched by the benchmark |
