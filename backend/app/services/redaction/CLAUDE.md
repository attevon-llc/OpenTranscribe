# app/services/redaction — PII / profanity / toxicity moderation

## Purpose

Detect profane / offensive / toxic words and PII, then mask them at every display and export
surface with `[CATEGORY]` placeholders. **The full original transcript always stays in the DB**
— masking is a read-time transform over cached detection *spans*, never a rewrite. Per-user
feature (Settings → Content Redaction) with an admin enforcement floor (Settings → Redaction
Policy) that can *force* PII/toxicity/profanity and mandate censored exports for everyone.

## Key files

- `spans.py` — `RedactionSpan` + **`apply_redactions`, the one read-time masker**. Pure (no I/O,
  no heavy deps) so it imports cleanly in the API process. Also `build_word_offsets` /
  `map_char_span_to_words`.
- `config.py` — `resolve_effective_config(db, user_id)` → `EffectiveRedactionConfig` (user
  `UserSetting` prefs ∪ admin `SystemSettings` `redaction.force_*` floor);
  `detection_config_for_all`, `detector_language_support`.
- `service.py` — `RedactionService.detect_and_store` / `mask_segment` / `is_segment_toxic`.
- `device.py` — `resolve_device()` + `inference_guard()`: **runtime, per-scan VRAM probe**.
- `detectors/` — `wordlist` (regex, read-time), `pii_presidio` (Presidio + spaCy, optional
  GLiNER), `toxicity` (`unitary/toxic-bert`; multilingual XLM-R for non-English), `llm`.

## Conventions / patterns

- **Detect-once, cache-forever.** Spans cache on `transcript_segment.redactions` + `.toxicity`.
  Enable/disable, categories, style, custom words, allowlist, PII-entity filter and toxicity
  threshold are all applied at *read* time — never a recompute. Only a segment text edit or a
  `REDACTION_MODEL_VERSION` bump re-detects.
- `detection_config_for_all()` deliberately ignores user/admin config (threshold `0.0`, all PII
  entities) so toggling a category later applies instantly.
- Overlapping spans merge; higher priority wins (`pii > toxicity > profanity > custom`).
- Detectors degrade to "no spans" on missing deps/failure — they never block the pipeline.
- NO `.env` vars for behavior; coded `DEFAULT_REDACTION_*` in `core/constants.py`.
  `REDACTION_DEVICE` / `REDACTION_MIN_FREE_VRAM_GB` / `REDACTION_PII_USE_GLINER` are ops knobs.

## How it connects

- Worker `tasks/redaction_task.py` (`redaction.detect`, `redaction.reindex_all`) on the
  `redaction` queue → the `celery-redaction` container (the only `PRELOAD_REDACTION_MODELS=true`).
- Read surfaces: `formatting_service::_apply_redaction`, `subtitle_service` (SRT/VTT/TXT),
  `utils/transcript_builders` (redact-before-LLM), `search/hybrid_search_service` (snippets).
- Reveal via `?redact=false` → `cfg.reveal_categories(...)` in `api/endpoints/files/crud.py`
  (`_resolve_redaction_for_request`) and `files/subtitles.py`; audited as
  `transcript.view_unredacted`. **Admin-forced categories never reveal.**
- Settings `api/endpoints/redaction_settings.py` (`/user-settings/redaction`,
  `/admin/redaction-policy`); UI `ContentRedactionSettings.svelte` / `RedactionPolicySettings.svelte`.
  Migration `v364_add_content_redaction`; `redaction_{start,end}_ms` on `FilePipelineTiming`.
- Tests: `tests/redaction/` (GPU-free), `tests/integration/test_redaction_pipeline.py`,
  `tests/e2e/test_redaction_e2e.py`, fixtures `tests/fixtures/redaction/`. ML detector tests are
  `@pytest.mark.models`. Docs: `docs-site/docs/features/content-redaction.md`.

## Gotchas

- **Redaction is OPT-OUT (`DEFAULT_REDACTION_ENABLED = False`) and detection is gated on it.**
  `tasks/transcription/postprocess.py::_dispatch_redaction` skips the scan when the owner has it
  off. Never-scanned files are dispatched lazily on first read, so an existing transcript with no
  spans is expected rather than a bug. (`redaction_task.py`'s docstring claimed the opposite until
  #296 — it now documents the gate.)
- **Enabling redaction withholds the transcript** until detection finishes:
  `crud.py::_redaction_pending` returns `redaction_pending: True` for `None|pending|processing`
  (`done`/`failed` never block). Usual cause of "my transcript is empty".
- **`_gpu_inference_lock` is non-reentrant.** `detect_and_store` already holds `inference_guard()`,
  so `toxicity.score_texts` must NOT re-acquire it (`score_text` does — single-segment path only).
- **"CPU service" is a misnomer**: `REDACTION_DEVICE=auto` puts models on GPU whenever free VRAM
  ≥ `REDACTION_MIN_FREE_VRAM_GB` (1.5), re-probed per scan, moving them back under pressure.
- **Segment-edit re-detection runs inline in the API process** (`crud.py`, `text_changed` branch),
  which never preloads Presidio/toxicity — ML detectors are best-effort there.
- PII is detected and cached but **not** in `DEFAULT_REDACTION_CATEGORIES` (too aggressive on
  conversation); `ORGANIZATION` is excluded from default entities (spaCy over-tags acronyms).
- Profanity and PII are **English-only** (`REDACTION_*_LANGUAGES`); unsupported languages come
  back as `skipped_detectors` rather than silently dropping.
- The `blur` style emits HTML; originals are `html.escape`d but it still must pass through the
  frontend `sanitizeHtml` allowlist.
