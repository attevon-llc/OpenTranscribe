# Boundary-regression fixture (issue #193)

Frozen test data for `backend/tests/integration/test_boundary_regression.py`. The committed
trio lets the boundary fix be regression-tested **GPU-free in CI** — the GPU output is frozen,
so the test only replays the CPU finalize/smoother path and asserts WSER + island count have
not drifted from the baseline.

## Source

All three files derive from the reporter's acceptance clip:

- **Video**: Karpathy on *No Priors* — <https://www.youtube.com/watch?v=kwSVtQ7dziU> (2 speakers,
  English, ~66 min). First **10 minutes** used here.
- **Ground truth**: the maintainer's hand labels, committed as
  `benchmark/diarization-boundary/karpathy/karpathy_kwSVtQ7dziU/reference.rttm`.
- **Audio**: gitignored, persisted on the host at
  `benchmark/diarization-boundary/karpathy/karpathy_kwSVtQ7dziU/{audio.wav, karpathy_10m.wav}`
  (rebuildable from MinIO — see that folder's `../../README.md`). DB `media_file` id 3141 /
  uuid `b1c6e10a…` — **do not reprocess that file**; it holds the labels.

## The three files

| File | What |
|---|---|
| `karpathy_10m.rawinfer.json` | Frozen `Engine.run_gpu_stage().serialize()` — Whisper words + PyAnnote intervals (the GPU output, ~205 KB). |
| `karpathy_10m.ref.words.json` | Per-word ground truth: each word's reference speaker, by midpoint lookup into `reference.rttm`. |
| `karpathy_10m.baseline.json` | Frozen `{off, on}` WSER + island + collar-0 DER. The drift gate. |

## Regenerate (after an intentional algorithm change)

Stage the clip + reference into the GPU worker (see the corpus README), then:

```bash
docker compose exec -T celery-worker python -m scripts.build_regression_fixture \
    --audio /tmp/karpathy_10m.wav --ref-rttm /tmp/karpathy_ref.rttm \
    --name karpathy_10m --out /tmp/fixtures
# copy /tmp/fixtures/karpathy_10m.* back into this directory and commit.
```

Regenerating resets the baseline to the new behavior — do it only when a change to
`smooth_word_speakers` / `assign_speakers` is deliberate, and review the WSER delta first.

See also: `benchmark/diarization-boundary/README.md` (audio + cloud-provider testing) and
`docs/diarization-boundary-results/cloud-comparison.md` (local-vs-cloud results).
