# Diarization boundary benchmark corpus (issue #193)

Test corpus + ground truth for measuring and tuning the speaker-boundary fix. **Audio and
`*.rawinfer.json` caches are NOT committed** (see `.gitignore`) — only references, manifests,
and `download.sh` scripts are, so the corpus is reproducible. Keep your own backup of the
downloaded audio; it is not recoverable from the repository.

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

## Workflow

The backend/worker container mounts only `backend/` as `/app` (the GPU benchmark compose file also
mounts `docs/`), so this folder is **not** visible inside it. The scripts are `backend/scripts/*.py`;
in the container they are `scripts.<name>` relative to `/app`. Stage inputs with
`docker compose cp ... celery-worker:/tmp/...` (see "Cloud-provider testing" below), and note that
`build_fixture.py` refuses to run outside a container. The `docker compose` calls below are the
documented form for this workflow; the stack must already be up through `./opentr.sh start dev`.

1. **Download** `bash <dataset>/<id>/download.sh` (yt-dlp/ffmpeg → 16 kHz mono WAV).
2. **Ingest via the app** (upload or `POST /api/files/from-url`) with `large-v3` so it lands in
   Postgres and is editable in the UI, and capture the GPU fixture on the same WAV (in the
   worker container, audio staged under `/tmp`):
   `python -m scripts.build_fixture /tmp/audio.wav --model large-v3 --out /tmp/<id>__large-v3.rawinfer.json`.
3. **Hand-label** in the segment UI (fix wrong-speaker fragments), then run the
   ±2 s seam audit in Audacity (`python -m scripts.make_seam_labels emit transcript.json --out seams.txt`).
4. **Export the reference** (in-container, DB session) via
   `app.utils.transcript_comparison.export_word_reference(db, file_id, "reference.words.json", "reference.rttm")`,
   and build `reference.turns.rttm` from the corrected seam audit
   (`python -m scripts.make_seam_labels to-rttm corrected.txt --out reference.turns.rttm`).
5. **Public datasets** ship RTTM directly (AMI, VoxConverse) or via `nlp_to_rttm.py` (Earnings-21).
6. **Score** (in the container, with `corpus.json` and references staged):
   `python -m scripts.benchmark_boundary --corpus <path>/corpus.json --models large-v3 --smoothing sweep --out <outdir>`.
   `--help` lists `--cache-dir`, `--ref-dir`, `--datasets`, `--sample` and `--bootstrap`.

## Metrics

Headline **WSER** (`app.utils.diarization_metrics.wser`) + bleed-island count; cpWER (meeteval) and
DER (pyannote.metrics, collar 0.25 and 0) are cross-checks. See issue #193.

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
    --cloud <config-id> --cloud <config-id> ... \
    --min-speakers 2 --max-speakers 2
```

**If the clips are missing** (e.g. fresh checkout, or the host copy was cleared), re-download the
source with the folder's `download.sh`, or rebuild from a copy of the labeled file already stored
in MinIO (substitute that file's object key):

```bash
docker compose exec -T celery-worker python -c \
  "from app.services.minio_service import download_file_to_path; \
   download_file_to_path('<object-key>.mp4','/tmp/karp_src.mp4')"
docker compose exec -T celery-worker bash -lc \
  'ffmpeg -y -i /tmp/karp_src.mp4 -ar 16000 -ac 1 -t 600 /tmp/karpathy_10m.wav && \
   ffmpeg -y -ss 90 -t 30 -i /tmp/karpathy_10m.wav -ar 16000 -ac 1 /tmp/clip30.wav'
# then docker compose cp them back into this folder to re-persist.
```

Results table + per-provider notes: `docs/diarization-boundary-results/cloud-comparison.md`.
If you ingest the Karpathy clip into a deployment to hand-label it, **do not reprocess that file
afterwards**: the DB row holds the hand labels. (The maintainers' labeled copy is `media_file` uuid
`b1c6e10a…`; it exists only on their deployment.)
