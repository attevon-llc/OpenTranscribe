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
- **A detector reports THREE outcomes, not two** (`detectors/DetectorUnavailableError`):
  found nothing (`[]`), ran and raised (a plain exception), could not run at all (that
  exception). The third used to be the first — `pii_presidio._get_analyzer` caught an absent
  Presidio, returned `None`, and `detect_pii` returned `[]`, which is a clean segment. See the
  gotcha below; degrading to "no spans" is still what the *pipeline* does, but that is now a
  decision made where the policy lives.
- NO `.env` vars for behavior; coded `DEFAULT_REDACTION_*` in `core/constants.py`.
  `REDACTION_DEVICE` / `REDACTION_MIN_FREE_VRAM_GB` / `REDACTION_PII_USE_GLINER` are ops knobs.

## How it connects

- Worker `tasks/redaction_task.py` (`redaction.detect`, `redaction.reindex_all`) on the
  `redaction` queue → the `celery-redaction` container (the only `PRELOAD_REDACTION_MODELS=true`).
- Read surfaces: `formatting_service::_apply_redaction`, `subtitle_service` (SRT/VTT/TXT),
  `utils/transcript_builders` (redact-before-LLM), `search/hybrid_search_service` (snippets).
- **Redact-before-LLM is centralized in `llm_guard.py`.** Every path that ships transcript text
  to a provider resolves its config through `resolve_llm_masking(db, media_file)` — summarization,
  speaker identification and topic extraction (chat has its own, see below). Do not call
  `resolve_effective_config` directly from a new LLM path; see the gotcha below for why.
- Reveal via `?redact=false` → `cfg.reveal_categories(...)` in `api/endpoints/files/crud.py`
  (`_resolve_redaction_for_request`) and `files/subtitles.py`; audited as
  `transcript.view_unredacted`. **Admin-forced categories never reveal.**
- Settings `api/endpoints/redaction_settings.py` (`/user-settings/redaction`,
  `/admin/redaction-policy`); UI `ContentRedactionSettings.svelte` / `RedactionPolicySettings.svelte`.
  Migration `v364_add_content_redaction`; `redaction_{start,end}_ms` on `FilePipelineTiming`.
- Tests: `tests/redaction/` (GPU-free; `test_detector_unavailability.py` pins the three detector
  outcomes and both `detect_and_store` dispositions), `tests/integration/test_redaction_pipeline.py`,
  `tests/e2e/test_redaction_e2e.py`, fixtures `tests/fixtures/redaction/`. ML detector tests are
  `@pytest.mark.models`. Docs: `docs-site/docs/features/content-redaction.md`.

## Gotchas

- **⚠️ "Unavailable" and "failed" are one sink and two dispositions — do not collapse them.**
  `detect_segment_spans` takes `failures` (issue #324) and `unavailable`, a strict SUBSET of it.
  **Every masker reads only `failures`**, so a dead detector and a broken one both withhold text
  — correct, because both mean "could not look" and the next step is a provider. Only
  `detect_and_store` reads `unavailable`, and it **subtracts it before deciding FAILED**.
  That subtraction is load-bearing: `redaction_status = failed` is not an inert label, because
  `llm_guard.resolve_llm_masking` turns it into a **non-retryable** `RedactionNotReadyError`
  that `defer_for_redaction` re-raises at once. Marking every file FAILED on a deployment that
  simply has no Presidio would permanently break summarization, speaker-ID **and** topic
  extraction for every user with `redact_before_llm` on. Unavailable detectors go into
  `skipped_detectors` instead — which the frontend already toasts
  (`fileDetail/notificationHandler.ts`), so the operator sees it with no UI change. Re-running
  a scan does not install a dependency, so FAILED ("re-run me") is also just the wrong word.
- **⚠️ RESIDUAL, KNOWN, PINNED (task #78): the CACHED path still trusts a scan that never ran
  PII.** The fix above closes `_mask_inline` — the *fallback*. `chat/redactor._mask_from_segments`
  is the path most requests take, and it trusts cached spans on `redaction_status == done`;
  since unavailability is a skip, a no-Presidio scan still reaches `done`. A `pii`-enabled user
  therefore still gets raw text with `was_masked=True` through the primary path. The masker
  **cannot probe for itself** — the API process and `celery-redaction` preload different models,
  so "can I load Presidio here" answers a different question from "did the detector that produced
  these spans have it". Closing it needs durable per-file **detector coverage** (a schema change).
  `tests/unit/test_chat_redactor.py::test_the_cached_path_still_trusts_a_scan_that_never_ran_pii`
  is a **must-fire** guard: it asserts the hazard, and goes RED when #78 lands.
- **⚠️ `mask_segment` with no cached spans masks NOTHING and returns the text unchanged.** This is
  the trap behind redact-before-LLM: `_dispatch_redaction` queues detection from the *same*
  post-processing step that queues summarization / speaker-ID / topic-extraction, onto a
  *different* queue. Nothing orders them, so the LLM tasks routinely reach a transcript whose
  `TranscriptSegment.redactions` are still NULL — the call looks masked and isn't.
  `resolve_llm_masking` is what closes this: it gates on `redaction_status == done` and raises
  `RedactionNotReadyError` otherwise, and `defer_for_redaction(self, exc)` re-queues the bound task
  (bounded by `REDACTION_LLM_MAX_DEFERRALS`) instead of sending raw text.
  Chat can't wait mid-request, so it masks inline instead (`services/chat/redactor.py`) — same
  guarantee, different tradeoff.
- **Celery's `Retry` subclasses `Exception`.** All three LLM tasks wrap their body in a broad
  `except Exception` that would swallow the deferral and report a failure, so each re-raises
  `Retry` first. Any new deferring task needs that arm too.
- **`transcript_builders.mask_segment_text` fails CLOSED** — a masking error yields
  `REDACTION_LLM_FAILSAFE_TEXT` (`[redacted]`), never the original text. It used to return the
  input, which turned any masking bug into a silent leak. `build_speaker_segments` masks *before*
  truncating to 200 chars so the window can't slice a mask open.
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
