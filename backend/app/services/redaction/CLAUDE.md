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
- `coverage.py` — `uncovered_detectors(media_file, cfg)`: **which detectors a finished scan
  actually ran**, against what the policy relies on. The read half of
  `media_file.redaction_coverage` (`v391`). See the gotcha below.
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
  decision made where the policy lives. **`toxicity` follows the same three outcomes** (its
  `None` now means "blank text" and nothing else) — but with a different blocking disposition,
  because it produces no spans. See its gotcha below.
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
  Migrations `v364_add_content_redaction` and `v391_add_redaction_coverage`;
  `redaction_{start,end}_ms` on `FilePipelineTiming`.
- Tests: `tests/redaction/` (GPU-free; `test_detector_unavailability.py` pins the three detector
  outcomes and both `detect_and_store` dispositions, `test_scan_coverage.py` drives the real
  pre-LLM egress path — `resolve_llm_masking` → `build_full_transcript` — and carries the four
  controls that keep the gate narrow), `tests/integration/test_redaction_pipeline.py`,
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
- **⚠️ `done` says the scan FINISHED, not that it LOOKED — `redaction_coverage` is the
  difference.** Once unavailability became a skip (above), a scan could reach `done` having
  never run the PII detector. Every reader that trusted `done` alone then masked nothing,
  reported success, and sent the transcript on: `mask_segment` over an empty span list returns
  the input, with no error and no log line. The masker **cannot probe for itself** — the API
  process and `celery-redaction` preload different models, so "can I load Presidio *here*"
  answers a different question from "did the detector that produced these spans have it". Only
  the scan knows, and `skipped_detectors` is a **task return value** that reaches a Celery
  result backend with a TTL and a WebSocket toast — nothing a masker can read an hour later.
  So `detect_and_store` writes `media_file.redaction_coverage` (`v391`, `TEXT[]`) in the same
  commit as `done`, and `coverage.uncovered_detectors` is the one reader.
  - **`TEXT[]`, not JSONB.** One reader, by primary key, over a closed four-name vocabulary;
    nothing filters, aggregates or joins. JSONB buys nesting nobody needs and invites the
    column to become a second, undocumented status. No CHECK on the vocabulary either — a
    stray name grants no coverage (the gap is `required - covered`), and the hazardous state,
    a *missing* name, is what no CHECK can see.
  - **NULL is trusted, deliberately.** Pre-`v391` rows cannot be classified retroactively, and
    refusing them would break every existing file on upgrade day. `redaction.reindex_all` is
    the remedy and re-scanning writes the column. That residual is real; it is stated in
    `coverage.py` and pinned by a control test.
  - **A language skip is NOT a gap.** `detector_language_support` is subtracted first. Profanity
    and PII are English-only *by design*, identically on every future scan and unfixable by any
    operator action, so treating that as a gap would withhold every non-English transcript from
    every LLM feature permanently — a different decision from this one. An unavailable detector
    is the opposite on every count: a deployment fault, fixable by installing the dependency.
  - **A new detector needs a `REDACTION_MODEL_VERSION` bump.** Coverage records what ran, so a
    detector added to `DEFAULT_REDACTION_DETECTORS` reads as a gap on every previously scanned
    file until it is re-scanned. That was already true of the span cache; the column makes it
    enforced rather than tacit.
- **Both LLM egress paths are wired to coverage — `llm_guard` AND `chat/redactor`.**
  `_mask_from_segments` and `mask_digests` each call `uncovered_detectors` beside their existing
  `redaction_status == done` check; a gap returns `None` (chunks) or skips the provenance read
  (digests), and both fall through to `_mask_inline`, which runs the detector here and now and
  fails closed. **The subject differs between them and that is deliberate**: `llm_guard` resolves
  the FILE OWNER's policy (the content is theirs), chat resolves the REQUESTING USER's (one turn
  spans a library of shared recordings with no single owner). Pinned by
  `tests/unit/test_chat_redactor.py::test_the_cached_path_refuses_a_scan_that_never_ran_pii`.
  ⚠️ That guard's predecessor was **unfalsifiable**: it used `555-1234`, which Presidio does not
  recognise as a phone number, so it passed whether the cached path or the inline path ran — it
  asserted the hazard with a string that could not demonstrate it either way. Any test here that
  claims to prove a leak must use PII the detector actually detects.
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
- **⚠️ Segment-edit re-detection is the one fail-closed path that WRITES** — and the API process
  it runs in never preloads Presidio/toxicity, so it is also where a detector is most likely to
  be unavailable. `crud.py`'s `text_changed` branch used to persist whatever
  `detect_segment_spans` returned; since that call swallows a detector exception, an outage was
  cached as `redactions = NULL` under `redaction_status = done`, and **every later read then
  took the cached-span path and found nothing to mask**. Unlike the request-scoped fixes in
  `chat/redactor.py`, the leak outlived its cause — the detector never had to fail again.
  `RedactionService.redetect_edited_segment` now owns the decision: it keeps the spans the
  detectors that DID run found (real findings, and detected against the NEW text so their
  offsets address the text they are stored beside — the previous spans are simply dropped,
  because nothing can realign them to edited text), and on a **blocking** failure sets the file
  to `pending` + queues `redaction_detect_task`.
  `pending`, not a new sentinel: every status-aware reader already honours it (`_redaction_pending`
  withholds, `chat/_mask_from_segments` refuses non-`done` and falls through to inline
  fail-closed masking, `llm_guard` defers **retryably**). Not `failed` — that is non-retryable in
  `llm_guard`. Not a 4xx — a detector outage must not block a user fixing a typo.
  **The queue drains in one hop**: `detect_and_store` writes `processing` then `done` or
  `failed` and never `pending`, so only an edit can set it and there is no cycle; a second edit
  while a scan is pending rides the first one's dispatch. Blocking is `blocking_detector_failures`
  against the **file owner's** policy (the subject `llm_guard` uses — the editor may be an admin
  whose own redaction is off), so a CPU-only deployment that never enabled `pii` is untouched.
  Pinned by `tests/redaction/test_segment_edit_redetection.py`.
- **⚠️ The toxicity detector emits a SCORE, never a span — so `_DETECTOR_CATEGORIES` maps
  `toxicity` to NOTHING.** That entry is the one with a decision in it, and it is what makes
  reporting toxicity failures safe. Its outcomes used to be swallowed entirely
  (`detect_segment_spans` logged them at `logger.debug`; `score_texts` caught its own
  exceptions and returned `[None] * len(texts)`, the same value a transcript of blank segments
  produces), so a toxicity-only fault marked nothing, reported nothing, and left a column of
  NULL scores that read as "scored, not toxic". Both now follow the PII split — absent weights
  raise `DetectorUnavailableError` (reported, `done`, in `skipped_detectors` and out of
  `redaction_coverage`), a thrown inference is a hard failure (`failed`, worth re-running).
  **What they must NOT do is withhold text.** `toxicity` *is* a default category, so routing it
  into the blocking sink the obvious way would, on any box without the ~500 MB toxic-bert
  weights, mark every default-configured user's file stale on every segment edit, queue a full
  re-scan that ends in the same state, and refuse every LLM feature — for a detector whose
  absence cannot leave one character unmasked. `is_segment_toxic` is consumed only by
  `formatting_service` to flag a segment in the UI; the `toxicity` *category*'s maskable spans
  come from the `llm` detector, which keeps all four categories. Pinned by
  `tests/redaction/test_scan_coverage.py` (the storm control drives the real edit endpoint).
- PII is detected and cached but **not** in `DEFAULT_REDACTION_CATEGORIES` (too aggressive on
  conversation); `ORGANIZATION` is excluded from default entities (spaCy over-tags acronyms).
- Profanity and PII are **English-only** (`REDACTION_*_LANGUAGES`); unsupported languages come
  back as `skipped_detectors` rather than silently dropping.
- The `blur` style emits HTML; originals are `html.escape`d but it still must pass through the
  frontend `sanitizeHtml` allowlist.
