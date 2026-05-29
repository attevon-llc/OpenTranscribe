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

## dual_a6000

| Conc | Files | RTF (h/h) | Speedup vs c1 | VRAM peak (MB) | GPU util % | CPU avg % | RAM avg (MB) | GPU p50 (s) | GPU p95 (s) | Preproc p95 (s) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 40 | 40 | 81.27 |  | 35739 | 78.0 | 1489.9 | 40638.0 | 405.9 | 1203.7 | 48.0 |

## Latency & Contention (multi-user serving view)

Per-file behaviour as concurrency rises: throughput goes up but per-file latency and GPU-queue wait grow. "GPU inflation" = a file's GPU-compute time vs the conc-1 (isolated) baseline. "Per-file RTF" = audio_s / gpu_s.

### a6000_solo
| Conc | Wall p50 (s) | Wall p95 (s) | Queue-wait p50 (s) | Queue-wait p95 (s) | GPU p50 (s) | GPU inflation vs c1 | Per-file RTF p50 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 1713.2 | 4586.1 | 1685.6 | 4513.7 | 79.8 | 1.00x | 39.3x |
| 4 | 2475.6 | 4089.0 | 1951.7 | 3693.5 | 310.1 | 3.89x | 12.0x |
| 8 | 2584.5 | 4926.5 | 1371.6 | 3647.5 | 924.5 | 11.59x | 5.2x |
| 10 | 1399.7 | 5075.1 | 690.5 | 3374.4 | 539.3 | 6.76x | 5.8x |
| 12 | 2630.6 | 4726.0 | 426.7 | 3681.7 | 688.4 | 8.63x | 5.2x |
| 16 | 1005.2 | 5156.7 | 237.4 | 4641.3 | 506.2 | 6.34x | 4.4x |
