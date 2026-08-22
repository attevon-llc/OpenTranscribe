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

## TWO trios, one per diarization backend (issue #520)

`_discover_fixtures()` globs `*.rawinfer.json`, so each trio is parametrized independently
under its own name.

| Prefix | `config_snapshot.diarizer_backend` | Why it is here |
|---|---|---|
| `karpathy_10m` | `pyannote` | The documented **failover** path. Frozen May 2026, when PyAnnote was the default. |
| `karpathy_10m_native` | `native` | **What production runs.** speakrs became the coded default in `config.py`, at which point the PyAnnote trio kept passing while testing a path nobody takes. |

Each trio is:

| Suffix | What |
|---|---|
| `.rawinfer.json` | Frozen `Engine.run_gpu_stage().serialize()` — Whisper words + diarizer intervals (~205 KB). |
| `.ref.words.json` | Per-word ground truth: each word's reference speaker, by midpoint lookup into `reference.rttm`. |
| `.baseline.json` | Frozen `{off, on}` WSER + island + collar-0 DER. The drift gate. |

Measured smoother effect, same clip, same reference:

| Backend | WSER off | WSER on | Reduction | Islands |
|---|---|---|---|---|
| `native` (speakrs) | 0.011525 | 0.002660 | **−76.9%** | 13 → 3 |
| `pyannote` | 0.009317 | 0.006211 | −33.3% | 7 → 1 |

speakrs' raw boundaries are **noisier** but smooth to a **better** result. The smoother is
therefore more load-bearing under the default than the original 32% figure suggested — the
opposite of the "it may no longer be needed" concern #520 was opened with.

## Regenerate (after an intentional algorithm change)

Stage the clip + reference into the GPU worker (see the corpus README), then:

```bash
docker compose cp benchmark/diarization-boundary/karpathy/karpathy_kwSVtQ7dziU/karpathy_10m.wav \
    celery-worker:/tmp/karpathy_10m.wav
docker compose cp benchmark/diarization-boundary/karpathy/karpathy_kwSVtQ7dziU/reference.rttm \
    celery-worker:/tmp/karpathy_ref.rttm

# the DEFAULT (speakrs) trio — needs the diar-native sidecar up
docker compose exec -T celery-worker bash -lc 'cd /app && ENGINE_DIARIZER_BACKEND=native \
    python -m scripts.build_regression_fixture --audio /tmp/karpathy_10m.wav \
    --ref-rttm /tmp/karpathy_ref.rttm --name karpathy_10m_native --out /tmp/fixtures'

# the FAILOVER (PyAnnote) trio
docker compose exec -T celery-worker bash -lc 'cd /app && ENGINE_DIARIZER_BACKEND=pyannote \
    python -m scripts.build_regression_fixture --audio /tmp/karpathy_10m.wav \
    --ref-rttm /tmp/karpathy_ref.rttm --name karpathy_10m --out /tmp/fixtures'
```

⚠️ **Backend resolution is DB → env → coded default.** A `engine.diarizer_backend` row in
`system_settings` **overrides** the env var above, so check it is unset before you trust which
engine ran. Then verify the artifact rather than the invocation:

```bash
python3 -c "import json;print(json.load(open('karpathy_10m_native.rawinfer.json'))['config_snapshot']['diarizer_backend'])"
# must print: native
```

That check is the whole point — without it a run that silently fell back to PyAnnote produces a
"speakrs twin" that is nothing of the kind, and the gate then guards the wrong path again.

Regenerating resets the baseline to the new behavior — do it only when a change to
`smooth_word_speakers` / `assign_speakers` is deliberate, and review the WSER delta first.

See also: `benchmark/diarization-boundary/README.md` (audio + cloud-provider testing) and
`docs/diarization-boundary-results/cloud-comparison.md` (local-vs-cloud results).
