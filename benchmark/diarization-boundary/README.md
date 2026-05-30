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
