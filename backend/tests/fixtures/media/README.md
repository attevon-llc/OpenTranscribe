# Small media + transcript fixtures

Tiny, committed, deterministic fixtures so suites that need "a real media file" or "a completed
transcript" stop skipping — and so they run in **GitHub Actions**, where there is no GPU and real
ASR is not an option. Everything here is derived from a local source video; the sources themselves
are **deliberately not committed** (93 MB total, see below).

| File | Size | What it is |
|---|---|---|
| `sample_short.wav` | 313 KB | 10.0 s, mono, 16 kHz, 16-bit PCM. The exact format the ASR path expects after `app/transcription/audio.py` preprocessing, so it can be fed straight to a transcriber or an upload endpoint. |
| `sample_short.mp4` | 46 KB | 5.0 s, 320×180, 15 fps, H.264 CRF 32 + 32 kbps mono AAC, `+faststart`. For upload / streaming / thumbnail / metadata-extraction tests that need a *video* container. |
| `sample_transcript.json` | 11 KB | A completed 2-speaker transcript, 7 segments, word-level timings, all inside the 10 s of the WAV. Shape derived from the code — see below. |

Total: ~370 KB.

## Source material (NOT committed)

Both media fixtures are cut from the same clip:

- **`test_videos/test_ai_video.mp4`** — 24 s, 1280×720 H.264, 48 kHz stereo AAC, 22 MB.

`test_videos/` is a local scratch directory holding three source videos (22–47 MB each, 93 MB
total). It is **not** in the repository and must not be added — that is the whole point of these
derived fixtures. Regenerate from the same source if a fixture ever needs to change.

## Exact ffmpeg commands used

Run from the repository root, with `test_videos/test_ai_video.mp4` present:

```bash
# sample_short.wav — 10 s from t=5s, mono / 16 kHz / s16le, metadata stripped
ffmpeg -hide_banner -v error -y -ss 5 -t 10 -i "test_videos/test_ai_video.mp4" \
  -vn -ac 1 -ar 16000 -c:a pcm_s16le -map_metadata -1 \
  backend/tests/fixtures/media/sample_short.wav

# sample_short.mp4 — 5 s from t=5s, heavily compressed, metadata stripped
ffmpeg -hide_banner -v error -y -ss 5 -t 5 -i "test_videos/test_ai_video.mp4" \
  -vf "scale=320:180,fps=15" -c:v libx264 -preset veryslow -crf 32 -pix_fmt yuv420p \
  -c:a aac -b:a 32k -ac 1 -ar 16000 -movflags +faststart -map_metadata -1 \
  backend/tests/fixtures/media/sample_short.mp4
```

`-map_metadata -1` drops the source container tags, so no title/author/device strings from the
original video leak into a committed fixture (and metadata-extraction tests see a clean slate).
The `t=5s` offset was chosen after checking `volumedetect` across the clip — every 5 s window has
real audio, so there is no leading silence.

## `sample_transcript.json` — where each shape comes from

Nothing here was invented; every key exists in the pipeline or the ORM.

- **`segments[]`** is the *processed-segment* dict — the contract between
  `app/tasks/transcription/speaker_processor.py:process_segments_with_speakers` (producer) and
  `app/tasks/transcription/storage.py:save_transcript_segments` (consumer). Hence `start` / `end`
  (not the `start_time` / `end_time` column names), plus `text`, `speaker`, `speaker_id`, `words`,
  `confidence`, `is_overlap`, and on the overlap pair `overlap_group_id` / `overlap_confidence`
  (set by `mark_overlapping_segments`). A test can pass this list to `save_transcript_segments`
  directly, or map it onto `app/models/media.py:TranscriptSegment` columns itself.
- **`words[]`** uses `{"word", "start", "end", "score"}` — the normalisation
  `storage.py:76` applies (`score` falls back to faster-whisper's `probability`). Word text keeps
  the leading space faster-whisper emits, and `"".join(w["word"] for w in words).strip() == text`
  holds for every segment, matching the frozen real inference in
  `../boundary/karpathy_10m.rawinfer.json`.
- **`speakers[]`** mirrors the `Speaker` rows `speaker_processor.create_or_get_speaker` inserts
  (`app/models/media.py:432`): `name` is the raw diarization label, plus `display_name`,
  `suggested_name`, `suggestion_source`, `verified`, `confidence`. The two speakers are
  deliberately different: `SPEAKER_00` is verified with a `display_name`, `SPEAKER_01` is
  unverified with only an AI suggestion — so speaker-status and label-resolution paths both have
  a case.
- **`media_file`** holds the columns `storage.py:update_media_file_transcription_status` writes on
  completion (`status="completed"`, `duration`, `language`, `whisper_model`, `diarization_model`,
  `embedding_mode`, `asr_provider`, `asr_model`, `diarization_disabled`) plus the upload-time
  technical metadata for the WAV. `duration` is `segments[-1]["end"]` — the same value the app
  computes — not the container's 10.0 s.

### Two things it deliberately does **not** contain

- **`speaker_id` is `null` on every segment.** It is an integer FK assigned at insert time; a
  fixture cannot know it. Insert the `speakers` first, then map `segment["speaker"]` (equal to
  `Speaker.name`) to the new row ids — exactly the `dict[label] -> id` that
  `create_speaker_mapping` returns.
- **No display fields.** `formatted_timestamp`, `display_timestamp`, `speaker_label`,
  `resolved_speaker_name`, `computed_status`/`status_text`/`status_color` are computed at read
  time by `app/services/formatting_service.py` and `SpeakerStatusService` and are never stored,
  so freezing them here would only create drift.

### The transcript text is synthetic

The dialogue is hand-authored, **not** an ASR transcription of `sample_short.wav`. The clip is
single-voice narration, and the fixture needs ≥2 speakers for speaker tests, so the two could not
be made to agree. Use `sample_transcript.json` for anything shape- or speaker-driven, and
`sample_short.wav` for anything that actually runs audio through a decoder; only assert on their
correspondence if you first regenerate the transcript from a real run.

## ⚠️ `.gitignore` currently excludes this directory

`.gitignore:213` has a bare `media/` rule (intended for runtime upload dirs), which matches
`media/` at **any** depth — including `backend/tests/fixtures/media/`. Verify before committing:

```bash
git check-ignore -v backend/tests/fixtures/media/sample_short.wav
```

If that still prints a match, the `media/` rule needs an anchor (`/media/`) or a negation
(`!backend/tests/fixtures/media/`) before these fixtures can be tracked. `git add -f` would work
once but leaves the trap in place for the next file added here.
