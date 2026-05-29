# Engine Optimization Benchmark — Collated Results

Generated from per-level `metrics.json`. RTF = audio-hours processed per wall-clock hour (steady-state throughput over the corpus).

## a6000_solo

| Conc | Files | RTF (h/h) | Speedup vs c1 | VRAM peak (MB) | GPU util % | CPU avg % | RAM avg (MB) | GPU p50 (s) | GPU p95 (s) | Preproc p95 (s) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 40 | 38.98 | 1.0 | 5239 | 54.0 | 668.0 | 9861.0 | 79.8 | 318.1 | 22.5 |
| 4 | 40 | 45.93 | 1.18 | 35883 | 85.0 | 758.0 | 24314.0 | 310.1 | 1025.6 | 28.6 |
| 8 | 40 | 39.19 | 1.01 | 48287 | 90.0 | 689.2 | 40508.0 | 924.5 | 2784.3 | 46.4 |
| 10 | 40 | 39.23 | 1.01 | 44279 | 91.0 | 788.6 | 50953.0 | 539.3 | 3169.6 | 68.5 |
| 12 | 40 | 39.53 | 1.01 | 46423 | 92.0 | 737.4 | 53628.0 | 688.4 | 3504.2 | 40.3 |
| 16 | 40 | 30.83 | 0.79 | 46921 | 93.0 | 750.6 | 65688.0 | 506.2 | 3114.8 | 49.1 |
