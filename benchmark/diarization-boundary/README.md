# Diarization boundary benchmark corpus (issue #193)

Test corpus + ground truth for measuring and tuning the speaker-boundary fix. **Audio and
`*.rawinfer.json` caches are NOT committed** (see `.gitignore`) — only references, manifests,
and `download.sh` scripts are, so the corpus is reproducible. Run from the **nvm** working
copy; back the datasets up to `/mnt/nas/datasets/diarization-boundary/`.

## Layout

```
diarization-boundary/
  corpus.json                     # manifest (one entry per file)
  <dataset>/<file_id>/
    download.sh                   # source URL + sha256 of the decoded 16 kHz mono WAV
    audio.wav                     # 16 kHz mono (gitignored)
    reference.words.json          # [{start,end,word,speaker}] — WSER reference
    reference.rttm                # same-speaker words collapsed — DER cross-check
    reference.turns.rttm          # model-independent turns (midpoint lookup for the model sweep)
    deepgram.rttm / aws.rttm / pyannote_ai.rttm   # cloud oracle outputs (after keys)
    provenance.md                 # model, commit SHA, date, labeler
  cache/<file_id>__<model>.rawinfer.json          # GPU-once fixtures (gitignored)
```

## Workflow (all steps run in the backend container)

1. **Download** `bash <dataset>/<id>/download.sh` (yt-dlp/ffmpeg → 16 kHz mono WAV).
2. **Ingest via the app** (upload or `POST /api/files/from-url`) with `large-v3` so it lands in
   Postgres and is editable in the UI — AND capture the GPU fixture on the same WAV:
   `python -m backend.scripts.build_fixture --audio audio.wav --model large-v3 --out cache/<id>__large-v3.rawinfer.json`.
3. **Hand-label** in the segment UI (fix wrong-speaker fragments), then run the
   ±2 s seam audit in Audacity (`backend/scripts/make_seam_labels.py emit ...`).
4. **Export the reference** (in-container, DB session) via
   `app.utils.transcript_comparison.export_word_reference(db, file_id, "reference.words.json", "reference.rttm")`,
   and build `reference.turns.rttm` from the corrected seam audit
   (`make_seam_labels.py to-rttm ...`).
5. **Public datasets** ship RTTM directly (AMI, VoxConverse) or via `nlp_to_rttm.py` (Earnings-21).
6. **Score**: `python -m backend.scripts.benchmark_boundary --corpus corpus.json --models large-v3 --smoothing sweep --out docs/diarization-boundary-results/<run>/`.

## Metrics

Headline **WSER** (`app.utils.diarization_metrics.wser`) + bleed-island count; cpWER (meeteval) and
DER (pyannote.metrics, collar 0.25 and 0) are cross-checks. See plan §4 / issue #193.

## Cloud-provider testing (reusable Karpathy clips)

The Karpathy acceptance clip is kept here so it can be re-used without re-downloading. These are
**gitignored** (`*.wav`) — persistent on the host, never committed:

```
karpathy/karpathy_kwSVtQ7dziU/
  audio.wav          # full ~66 min, 16 kHz mono (122 MB)  — the labeling source
  karpathy_10m.wav   # first 10 min  (19 MB)  — the cloud-vs-local comparison clip
  clip30.wav         # 30 s slice    (~940 KB) — quick single-provider smoke test
  reference.rttm     # COMMITTED — the maintainer's hand labels (ground truth for WSER)
```

The backend/worker container mounts only `backend/` → `/app`, so this folder is **not** visible
inside the container. Stage the clips into the worker's `/tmp` before running a test:

```bash
K=benchmark/diarization-boundary/karpathy/karpathy_kwSVtQ7dziU
docker compose cp $K/karpathy_10m.wav celery-worker:/tmp/karpathy_10m.wav
docker compose cp $K/clip30.wav       celery-worker:/tmp/clip30.wav
docker compose cp $K/reference.rttm   celery-worker:/tmp/karpathy_ref.rttm
```

Then run the harnesses (config ids are rows in `user_asr_settings`; keys decrypt from the DB):

```bash
# single provider, quick smoke test (validate + transcribe the 30 s clip)
docker compose exec -T celery-worker python -m scripts.test_cloud_asr \
    --config-id <id> --audio /tmp/clip30.wav --max-speakers 2

# full local-vs-cloud comparison (WSER + realtime) on the 10 min clip
docker compose exec -T celery-worker python -m scripts.compare_cloud_boundaries \
    --audio /tmp/karpathy_10m.wav --ref-rttm /tmp/karpathy_ref.rttm \
    --cloud 849 --cloud 610 --cloud 850 --cloud 851 --cloud 852 --cloud 853 \
    --min-speakers 2 --max-speakers 2
```

**If the clips are missing** (e.g. fresh checkout, or the host copy was cleared), rebuild them
from MinIO — the labeled file is stored as `media/1/<uuid>.mp4`:

```bash
docker compose exec -T celery-worker python -c \
  "from app.services.minio_service import download_file_to_path; \
   download_file_to_path('media/1/75f6a04f-5328-4f0c-a9d3-9cddc2d966f6.mp4','/tmp/karp_src.mp4')"
docker compose exec -T celery-worker bash -lc \
  'ffmpeg -y -i /tmp/karp_src.mp4 -ar 16000 -ac 1 -t 600 /tmp/karpathy_10m.wav && \
   ffmpeg -y -ss 90 -t 30 -i /tmp/karpathy_10m.wav -ar 16000 -ac 1 /tmp/clip30.wav'
# then docker compose cp them back into this folder to re-persist.
```

Results table + per-provider notes: `docs/diarization-boundary-results/cloud-comparison.md`.
The file `media/1/...mp4` uuid is the Karpathy ingest (DB `media_file` id 3141 / uuid `b1c6e10a…`);
**do not reprocess that DB file** — it holds the hand labels.
