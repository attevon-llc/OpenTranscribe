# v0.6.0 — Transcription and Speaker Identification Settings: Information Architecture Plan

Status: **PLAN ONLY, not started.** Written 2026-10-10 on branch `feat/v060-settings-ia-plan`
(worktree `.claude/worktrees/v060-settings-plan`). A fresh implementation agent with no
conversation memory should be able to execute this end to end from this file alone.

**Verified against base commit `e7fc25b8e7830b656dca0b0b481b879ea5028a30`**
(`fix(settings): drop the duplicate Speech Processing heading inside the panel`). Every line
number below was read from that tree. `SettingsModal.svelte` and `en.json` are edited often:
run `git rev-parse HEAD`, and if it differs, re-derive line numbers with the `rg` command next
to each citation before trusting them. Element ids (`#speaker-behavior`, …) are more stable than
line numbers; prefer them.

## The request (verbatim, from the owner)

> we need a full review of the transcription settings and we may need the same tab like design as
> there are a lot of options that expand and its also confusing that there is speaker detection.
> what is the difference between the transcription settings, the ASR provider and the Speech
> Processing and in the transcription it has speaker detection in the transcription settings.
> these are completely different tasks, speaker id and transcription are different models and
> processing. it looks confusing, we need a full agent audit of the settings related to
> transcription and speaker id and combine them and review duplicates if any and NOT removing
> features.

Hard constraints that follow from it, and from the repo rules:

- **No user-facing capability is removed.** Every control that exists today exists after this
  work, in a stated place (§4.4 parity matrix). Controls the audit found to be ignored by the
  pipeline are **kept** and filed as separate tickets (§7.2); this plan does not delete them.
- Tabs use `$components/ui/Tabs.svelte` plus a pure helper in `$lib/settings/*Tabs.ts` with a
  vitest test, exactly like Watch Sources and Privacy & Redaction.
- Role-aware: lock (greyed, padlock, tooltip) for a role that lacks access; hide only when the
  deployment lacks the capability (`frontend/src/components/settings/CLAUDE.md`, "Privilege
  gating").
- Old section ids keep working as aliases (deep links, settings search, tests).
- i18n in all 12 locales with real translations; `ar` is RTL.

## Before screenshots (light, en, super_admin `admin@example.com`, live stack :5373)

Captured 2026-10-10 by `/tmp/ia-plan/settings-shots.mjs`, `/tmp/ia-plan/surface-shots.mjs`,
`/tmp/ia-plan/reprocess-shots.mjs` (read-only: no Save/Submit/Confirm was clicked).
`/tmp` is not durable: copy them somewhere if you need them past a reboot, and re-run the
scripts against the stack if they are gone.

| Surface | Files in `/tmp/ia-plan/shots/` |
|---|---|
| Settings → Transcription Settings (Advanced expanded) | `settings-01-Transcription_Settings-expanded-p1..p4-light-en.png` |
| Settings → ASR Provider | `settings-02-ASR_Provider-p1..p2-light-en.png` |
| Settings → Speech Processing | `settings-03-Speech_Processing-p1..p2-light-en.png` |
| Settings → Custom Vocabulary | `settings-04-Custom_Vocabulary-p1-light-en.png` |
| Settings → Speaker Attributes | `settings-05-Speaker_Attributes-p1-light-en.png` |
| Settings → Auto-Label Settings | `settings-06-Auto_Label_Settings-p1-light-en.png` |
| Settings → Search & Indexing (System) | `settings-07-Search_Indexing-p1-light-en.png` |
| Settings → Speaker Embedding System (System) | `settings-08-Speaker_Embedding_System-p1..p2-light-en.png` |
| Settings → Audio Extraction (Media & Output) | `settings-09-Audio_Extraction-p1..p2-light-en.png` |
| Settings → Watch Sources (the tab pattern the owner likes) | `watch-sources-tab-pattern-light-en.png` |
| Watch source create modal (min/max speakers) | `watch-source-modal-p1-light-en.png` |
| Upload wizard (open, file chosen, Tags, Collections, Speakers, Options, Review) | `upload-00-open … upload-06-step-light-en.png` (`upload-04` = Speakers step) |
| File detail page | `file-detail-light-en.png` |
| Reprocess dialog (stages, then Settings step) | `reprocess-01-stages-light-en.png`, `reprocess-02-settings-light-en.png` |
| Speakers page | `speakers-page-light-en.png` |

---

## ⚠️ Premise corrections (read before doing anything)

The request and the current UI both carry assumptions that the code does not support. Correcting
them shapes the design, so they come first.

### PC1 — "Speech Processing" contains no transcription settings at all

The panel's description (`settings.engineSettings.description`, `en.json:4139`) says *"Advanced
configuration for transcription and diarization engine backends"*. Every control in it is a
**speaker identification** control: `diarizer_backend`, `diarizer_require_sidecar`,
`boundary_smoothing_enabled`, `boundary_acoustic_recheck_enabled`,
`boundary_acoustic_cosine_margin`, `boundary_acoustic_max_word_dur`
(`frontend/src/components/settings/EngineSettings.svelte:16-23`). The backend has one more key,
`engine.transcriber_backend`, which the panel does not render. Its only reader is a log line
(`backend/app/services/engine/backends/__init__.py:57-61`, `engine.py:59-61`), so it is
effectively dead (finding F9). "Speech Processing" therefore belongs entirely under Speaker
Identification.

### PC2 — "Auto-Label Settings" is not about speakers

It sits in the Transcription group but auto-applies **AI tags and collections**
(`autoLabel.description`, `en.json:1607`: *"Automatically apply high-confidence AI-generated tags
and collections after transcription"*). It is stored under the US keys `auto_label_*` and read by
`backend/app/tasks/auto_labeling.py:50,156,258`. It is neither transcription nor speaker
identification, so it moves to the **AI & Chat** group. It keeps its section id.

### PC3 — "speaker detection" in Transcription Settings IS speaker identification

The "Speaker Detection" card (`TranscriptionSettings.svelte:313-432`) holds `diarization_source`
(on/off and which detector), `speaker_prompt_behavior` and `min/max_speakers`. All are stored in
the **same** `PUT /api/user-settings/transcription` payload as the language and VAD fields
(`backend/app/api/endpoints/user_settings.py:800-819`), which is why they ended up in one panel.
The fact that they share a storage endpoint does not make them one task. The partial-update PUT
already supports saving a subset (`model_dump(exclude_none=True)`, `user_settings.py:756`), so
the split needs no new write endpoint. It needs only a **scoped reset** (Step 1), because
`DELETE /user-settings/transcription` wipes all 15 keys (`user_settings.py:846-912`).

### PC4 — The word "pyannote" means two different things in two panels

- **Per-user**: `diarization_source = 'pyannote'` is labeled "pyannote.ai Cloud"
  (`en.json:188`). It means sending audio to the hosted **pyannote.ai API** (`cloud_asr.py:255-268`,
  `DiarizationProviderFactory`).
- **Admin**: `engine.diarizer_backend = 'pyannote'` (`EngineSettings.svelte:186`, hardcoded
  English *"pyannote (failover)"*) means the **local PyAnnote engine** on the GPU instead of the
  diar-native sidecar (`transcription/config.py:357-399` → `model_manager.py:99-180`).

These are different products under one name. The new wording (§5) always says
"pyannote.ai cloud service" for the first and "PyAnnote fallback engine" for the second.

### PC5 — With the local model, three of the four "Speaker Diarization" choices behave the same

For local ASR, only `off` is checked (`backend/app/tasks/transcription/core.py:470-471`).
`provider`, `local` and `pyannote` all mean "diarize locally". The choice only diverges for cloud
ASR (`cloud_asr.py:242`, `:255-268`; `postprocess.py:216-238`). The option labels must not imply
otherwise (§5).

### PC6 — The min/max speaker range is a hint the default engine ignores

The default `native` diarizer sends no speaker constraints. Its own module docstring says so
(`backend/app/transcription/diarizer_native.py:17-21`), and `num_speakers` only logs a warning
(`:946-951`). The range is honoured only by the PyAnnote engine (`diarizer.py:281-285`) and by
cloud providers (`cloud_asr.py:238-241`). This plan **keeps** the control and changes its help
text so it no longer over-promises (§5). Making the native engine honour it is out of scope
(F1, §7.2).

### PC7 — There are no deep links into any of these sections today

No frontend code calls `settingsModalStore.open(...)` with `transcription`, `asr-provider`,
`engine-settings`, `custom-vocabulary`, `speaker-attributes`, `auto-labeling`,
`search-indexing`, `embedding-migration` or `audio-extraction`. The only `open()` call sites are
`MediaRecordPanel.svelte:169`, `Navbar.svelte:112`, `ChatEmptyState.svelte:43`,
`FirstRunWizard.svelte:82,87` and `lib/supportAccess/wsHandlers.ts:46`. The backend sends no
`settings_section` payloads either. Aliases are still needed, for the **settings search index**
(`frontend/src/lib/search/settingsSearchIndex.ts:38-78` is keyed by section id), for e2e/vitest
fixtures, and for future deep links. Nothing external breaks if an alias is mishandled, so the
test coverage in Step 5 is what proves the aliases work.

---

## 1. Inventory

### 1.1 Legend

- **Scope**: `user` = per-user preference; `file` = per upload/reprocess/watch source;
  `system` = deployment-wide.
- **Role**: who may change it. `user` = any signed-in user (`get_current_active_user`);
  `admin` = `get_current_admin_user`; `super` = `get_current_active_superuser`.
- **Storage**: `US` = `user_setting` row (`backend/app/models/prompt.py:99-106`), key shown;
  `SS` = `system_settings` row; `env` = `backend/app/core/config.py`.
- **Lock (#1109)**: whether `backend/app/core/locked_settings.py` can force the value. The lockable
  set is exactly `diarization_source` (capability `transcription.diarization_source`), the six VAD
  and accuracy fields (`transcription.advanced`), and per-file `whisper_model`
  (`transcription.model_choice`) (`locked_settings.py:24-38,57-69`). Everything else: **no**.
- **Reader**: the pipeline code that consumes the value. **DEAD** means no backend reader was
  found (the verifying `rg` is in §3).
- Lines marked ✔ were re-verified by the planner in this tree. The others come from a structured
  code audit and should be spot-checked before a ticket is filed on them.

### 1.2 Settings → "Transcription Settings" (`id: transcription`, cap `transcription.prefs`; panel `frontend/src/components/settings/TranscriptionSettings.svelte`, 1384 lines; endpoint `GET/PUT/DELETE /api/user-settings/transcription`, role user, `user_settings.py:604/728/846`)

| # | Control (en / key) | Element | What it does | Scope | Role | Storage | Pipeline reader | Lock | Default |
|---|---|---|---|---|---|---|---|---|---|
| T1 | "Speaker Diarization" `settings.transcription.diarizationSource` (options provider/local/pyannote/off) | `#diarization-source` :334, rendered only if cap `transcription.diarization_source` | Chooses who detects speakers, or turns detection off | user | user | US `transcription_diarization_source` | `preprocess.py:265-279` → `core.py:470-476` (off), `cloud_asr.py:242,255-268`, `postprocess.py:216-238` | **yes** | `provider` (`constants.py:795-796`) |
| T2 | "Behavior" `settings.transcription.behavior`; option labels **hardcoded English** in `getSpeakerBehaviorLabel` (`lib/api/transcriptionSettings.ts:168-187`) ✔ | `#speaker-behavior` :383 | Pre-fills the upload wizard's Speakers step | user | user | US `transcription_speaker_prompt_behavior` | **No backend reader** (UI only: `FileUploader.svelte:290-307`) | no | `always_prompt` (`constants.py:623`) |
| T3 | "Min Speakers" `settings.transcription.minSpeakers` | `#min-speakers` :400 (shown only when T2=`use_custom` and T1≠off) | Lower bound of the speaker count | user | user | US `transcription_min_speakers` | `pipelines.py:110,225,351`; `cloud_asr.py:239`; honoured by PyAnnote `diarizer.py:284` and cloud; **ignored by native** ✔ | no | env `MIN_SPEAKERS`=1 (`config.py:1052`) |
| T4 | "Max Speakers" `settings.transcription.maxSpeakers` | `#max-speakers` :411 | Upper bound | user | user | US `transcription_max_speakers` | same as T3 | no | env `MAX_SPEAKERS`=20 |
| T5 | "System defaults: Min: 1, Max: 20" (read-only) | `.defaults-info` :426-431 | Shows the system range | — | — | `GET …/system-defaults` | — | — | — |
| T6 | "Enable cleanup" `settings.transcription.enableCleanup` | toggle :452 | Replaces long noise tokens with "[background noise]" | user | user | US `transcription_garbage_cleanup_enabled` | **DEAD** ✔. The pipeline reads SS `transcription.garbage_cleanup_enabled` (`finalize.py:197,346` → `system_settings_service.py:303-317`) | no | true |
| T7 | "Threshold: … chars" | number :460 | Word-length cutoff | user | user | US `transcription_garbage_cleanup_threshold` | **DEAD** ✔. The pipeline uses SS `transcription.max_word_length` | no | 50 |
| T8 | "Source Language" `settings.transcription.sourceLanguage` | `#source-language` :512 | Language hint for Whisper or the cloud provider | user | user | US `transcription_source_language` | `pipelines.py:42-60` → `transcriber.py:77-124,384`; `cloud_asr.py:238` | no | `auto` (`constants.py:847`) |
| T9 | "Translate to English" `settings.transcription.translateToEnglish` | toggle :547 (disabled if the model can't translate) | Whisper translate task | user | user | US `transcription_translate_to_english` | same as T8; cloud guard `cloud_asr.py:222-229` | no | false |
| T10 | "AI Summary Language" `settings.transcription.aiSummaryLanguage` | `#llm-output-language` :581 | Output language of **LLM** features (not ASR) | user | user | US `transcription_llm_output_language` | `summarization.py:477`, `speaker_identification_task.py:475`, `topic_extraction_service.py:178-195` | no | `en` |
| T11 | "Speech Detection Sensitivity" `settings.transcription.vadThreshold` | `#vad-threshold` :657, inside the collapsible "Advanced Transcription" (`advancedExpanded`, :64/601-635); cap `transcription.advanced` | Silero VAD threshold | user | user | US `transcription_vad_threshold` | `transcriber.py:406-411` | **yes** | 0.5 (`constants.py:782`) |
| T12 | "Minimum Silence Duration" | `#vad-min-silence` :685 | VAD | user | user | US `transcription_vad_min_silence_ms` | same | **yes** | 2000 |
| T13 | "Minimum Speech Duration" | `#vad-min-speech` :713 | VAD | user | user | US `transcription_vad_min_speech_ms` | same | **yes** | 250 |
| T14 | "Speech Padding" | `#vad-speech-pad` :741 | VAD | user | user | US `transcription_vad_speech_pad_ms` | same | **yes** | 400 |
| T15 | "Hallucination Filter" toggle + "Silence gap" | `#hallucination-toggle` :776 | Drops text produced inside silence gaps | user | user | US `transcription_hallucination_silence_threshold` (`""` = off) | `transcriber.py:417-418` | **yes** | null (off) |
| T16 | "Repetition Penalty" | `#repetition-penalty` :814 | Decoder repetition penalty | user | user | US `transcription_repetition_penalty` | `transcriber.py:412` | **yes** | 1.0 |
| T17 | "Reset to Defaults" / "Save Settings" | `.button-row` (~:862-878) | DELETE all 15 keys / PUT | user | user | — | — | — | — |

### 1.3 Settings → "ASR Provider" (`id: asr-provider`, cap `asr.user_providers`; `ASRSettings.svelte` 1253 lines + `ASRConfigModal.svelte` 714 lines; router `/api/asr-settings`, `router.py:237-242`)

| # | Control | Element | What it does | Scope | Role | Storage | Reader | Lock | Default |
|---|---|---|---|---|---|---|---|---|---|
| A1 | "Local GPU Model" info card + capability badges ("Speaker diarization" `settings.asrProvider.diarizationQuality`, "Translation", **hardcoded** "English optimized"/"Multilingual" ✔ :331) | :302-340, everyone | Shows the pinned local Whisper model | — | — | — | — | — | — |
| A2 | "Active model" select `settings.asrProvider.activeModel` + "Save model" | `#local-model-select` :352, rendered when **`isAdmin`** | Pins the deployment's local Whisper model | system | **super** ✔ (`asr_settings.py:950-958`). The UI shows it to plain admins, who get a 403 (finding F23) | SS `asr.local_model` | `transcription/config.py:317-355` (pinned at worker start) | no | env `WHISPER_MODEL` → `large-v3-turbo` |
| A3 | "Restart GPU worker" | :375 (isAdmin) | Restarts the worker to load A2 | system | **super** ✔ (`asr_settings.py:1024-1031`) | — | — | — | — |
| A4 | Status bar + "Use local" `settings.asrProvider.useLocal` | :396-410 | Clears the user's active cloud config | user | user | US `active_asr_config_id` | `services/asr/factory.py:540-610` | no | local |
| A5 | Config list: set active / test / edit / share toggle / delete / "Delete all" | :415-600 | Manages own and shared cloud ASR configs | user | user (share: see `asr_settings.py`) | table `user_asr_settings` | `factory.py:540-610`; routing `dispatch.py:103-155` | no | none |
| A6 | Config modal: name, provider, model, access key id, API key/secret, region, base URL, "Share globally", "Test connection" | `ASRConfigModal.svelte:202-386` | Creates or edits a cloud ASR config | user | user | `user_asr_settings` (encrypted keys) | same as A5 | no | — |

### 1.4 Settings → "Speech Processing" (`id: engine-settings`, cap `engine.settings`, `SECTION_MIN_ROLE` super_admin `SettingsModal.svelte:118`; the row is spread behind `isAdmin` at :342, so absent for plain users and locked for admins; `EngineSettings.svelte` 661 lines; endpoints `/api/admin/engine-settings`, all **super** `engine_settings.py:118,137,184`)

| # | Control | Element | What it does | Scope | Role | Storage | Reader | Lock | Default |
|---|---|---|---|---|---|---|---|---|---|
| E1 | "Diarizer Backend" (options **hardcoded English** "native (default)"/"pyannote (failover)" ✔) + source badge + Reset | `#diarizer-backend` | Which local engine separates speakers | system | super | SS `engine.diarizer_backend` → env `ENGINE_DIARIZER_BACKEND` | `transcription/config.py:357-399` → `model_manager.py:99-180` | no | `native` |
| E2 | "Require Diarization Sidecar" | `#diarizer-require-sidecar-input` | Fail instead of silently falling back to PyAnnote | system | super | SS `engine.diarizer_require_sidecar` | `diarizer_native.py:634-662,875-884` | no | false |
| E3 | "Boundary Smoothing" | `#boundary-smoothing-input` | Removes 1-3 word wrong-speaker islands | system | super | SS `engine.boundary_smoothing_enabled` | `boundary_resolver.py:62-147` at `finalize.py:150,311` | no | true |
| E4 | "Acoustic Backchannel Re-check" | `#boundary-acoustic-recheck-input` | Reassigns short backchannels by voiceprint | system | super | SS `engine.boundary_acoustic_recheck_enabled` | `stages.py:655-674`, **engine fast path only** (F8) | no | false |
| E5 | "Re-check Cosine Margin" | `#boundary-acoustic-cosine-margin` | Threshold for E4 | system | super | SS `engine.boundary_acoustic_cosine_margin` | `stages.py:671` | no | 0.05 |
| E6 | "Re-check Max Word Duration (s)" | `#boundary-acoustic-max-word-dur` | Threshold for E4 | system | super | SS `engine.boundary_acoustic_max_word_dur` | `stages.py:672` | no | 1.0 |
| E7 | "Save", per-key "Reset" (toast `resetToDefault`; error fallback **hardcoded** `` `Failed to reset ${key}` `` ✔ `EngineSettings.svelte:122`) | — | POST `/update`, DELETE `/{key}` | — | super | — | — | — | — |

The panel does **not** report dirty state (`rg -n setDirty EngineSettings.svelte` → nothing ✔), so
navigating away silently discards a draft.

### 1.5 Settings → "Custom Vocabulary" (`id: custom-vocabulary`, cap `vocab.user`; `CustomVocabularySettings.svelte` 662 lines; `/api/custom-vocabulary`, role user)

| # | Control | What it does | Scope | Role | Storage | Reader | Lock | Default |
|---|---|---|---|---|---|---|---|---|
| V1 | Domain filter tabs (hand-rolled `.domain-tabs`, :160) | Filters the list by domain | — | user | — | the pipeline ignores domain | — | all |
| V2 | Add term (term, domain select, category) | Adds a user-owned term | user | user | table `custom_vocabulary` (`user_id` set) | `tasks/transcription/user_settings.py:121-149` → local `hotwords` (`transcriber.py:389-415`); cloud `ASRConfig.vocabulary` (Deepgram/AssemblyAI/Speechmatics/Gladia) | no | — |
| V3 | Active toggle per term (disabled for system terms) | Includes or excludes a term | user | user (system terms are read-only, 403) | `is_active` | same | no | true |
| V4 | Delete term (confirm) | — | user | user | — | — | — | — |
| V5 | Bulk import textarea | Many terms at once | user | user | `POST /bulk` | — | — | — |

### 1.6 Settings → "Speaker Attributes" (`id: speaker-attributes`, no cap; `SpeakerAttributeSettings.svelte` 789 lines; `/api/user-settings/speaker-attributes` role user `user_settings.py:1219/1252/1280`)

| # | Control | Element | What it does | Scope | Role | Storage | Reader | Lock | Default |
|---|---|---|---|---|---|---|---|---|---|
| S1 | "Enable speaker attribute detection" | `#detection-enabled` | Runs the voice-attribute task after transcription. **Also gates LLM speaker-name suggestions** (F12) ✔ `postprocess.py:725-729` | user | user | US `speaker_attribute_detection_enabled` → SS → env | `speaker_attribute_task.py:99-125` | no | true |
| S2 | "Predict gender from voice" | `#gender-detection` | Intended to toggle gender prediction | user | user | US `speaker_attribute_gender_detection_enabled` | **DEAD** ✔ (no backend or frontend reader) | no | true |
| S3 | "Show predictions on speaker cards" | `#show-on-cards` | Intended to toggle the badges | user | user | US `speaker_attribute_show_on_cards` | **DEAD** ✔ (no backend or frontend reader) | no | true |
| S4 | Reset / Save | — | — | user | user | — | — | — | — |
| S5 | "Bulk Processing": "Run Detection on All Files", "Reprocess All Files" (confirm), "Stop Processing", progress | `.bulk-section` :352, gated only on cap `speaker_attributes.migration` | Re-runs attribute detection over the whole library | system | **super** ✔ (`speaker_attribute_migration.py:27,95,139,227`). The panel shows it to **every** role (F24) | Celery job | `speaker_attribute_migration` | no | — |

The panel has no `.section-title`. Its heading is a bare `<h3>` (:250), so the e2e helper
`_open_section` would time out on it.

### 1.7 Settings → "Auto-Label Settings" (`id: auto-labeling`, no cap; `AutoLabelSettings.svelte` 647 lines; `/api/user-settings/auto-label` role user)

| # | Control | Storage (US) | Reader | Default |
|---|---|---|---|---|
| L1 | "Enable Auto-Labeling" `#auto-label-enabled` | `auto_label_enabled` | `tasks/auto_labeling.py:50,156,258`; `topic_extraction_service.py:718` | per `constants.py:1118+` |
| L2 | "Confidence Threshold" `#confidence-threshold` | `auto_label_confidence_threshold` | same | 0.75 (`constants.py:1121`) |
| L3 | "Auto-Apply Tags" | `auto_label_tags_enabled` | same | — |
| L4 | "Auto-Apply Collections" | `auto_label_collections_enabled` | same | — |
| L5 | "Bulk Import Grouping" | `auto_label_bulk_grouping_enabled` | same | — |
| L6 | "Apply to Existing Files" | `POST /files/retroactive-auto-label` | — | — |

Scope user, role user, not lockable. This is **tags and collections, not speakers** (PC2).

### 1.8 Related settings outside the Transcription group

| # | Surface | Control | Scope / role | Storage | Note |
|---|---|---|---|---|---|
| X1 | System → "Speaker Embedding System" (`embedding-migration`, `SECTION_MIN_ROLE` admin) | "Re-extract All Embeddings", v3→v4 migration | system / **super** ✔ (all 9 routes in `embedding_migration.py` use `get_current_active_superuser`) | job | The section is admin-tier but the migration half is super-only (F25) |
| X2 | same section, "Embedding Consistency" | "Check Counts", "Repair Now", "Stop" | system / admin ✔ (`admin.py:2751-2773`) | job | Speaker voiceprint index repair |
| X3 | System → "Search & Indexing" | transcript embedding model, "Re-index All Transcripts" | system / admin | SS `search.embedding_model` | **Transcript search only**; does not affect speaker matching. Out of this IA; listed so nobody moves it here |
| X4 | Media & Output → "Audio Extraction" | auto-extract, threshold MB, show dialog, remember choice | user / user | US `audio_extraction_*` | Client-side upload behaviour; no pipeline reader. Out of this IA |
| X5 | Watch Sources → source modal | "Min speakers" `#ws-min`, "Max speakers" `#ws-max` (**hardcoded 1/20** defaults, `WatchSourceModal.svelte:100-101`), "Transcribe automatically on import" | file (per source) / owner | `watch_source.min/max_speakers` | Duplicates T3/T4 without loading the user's saved values (F31) |
| X6 | admin garbage-cleanup config | **no UI**. API client `AdminSettingsApi.getGarbageCleanupConfig` exists (`lib/api/adminSettings.ts:49-60`) with no component consumer ✔ | system / admin | SS `transcription.garbage_cleanup_enabled`, `transcription.max_word_length` | This is the value the pipeline actually uses (F6) |

### 1.9 Per-file surfaces (upload, reprocess, file detail, Speakers page)

| # | Surface | Control (en) | Payload | Overrides | Notes |
|---|---|---|---|---|---|
| U1 | Upload wizard step "Speakers" (`upload/UploadStepSpeakers.svelte`) | "Min Speakers" `#min-speakers`, "Max Speakers" `#max-speakers`, "Fixed Speaker Count" `#num-speakers` | `min/max/num_speakers` on `/files/complete` or `X-*-Speakers` headers | T3/T4 per file; `num` has **no** user pref (env `NUM_SPEAKERS`) | Pre-filled per T2. The step is always shown, even when T1=off or the model is CPU "Fast" (F28). Same DOM ids as T3/T4 |
| U2 | Upload wizard step "Options" (`upload/UploadStepModel.svelte`) | "Transcription Model" `#whisper-model-select` (High Quality GPU / Fast CPU), "Generate AI summary" | `whisper_model` on `/files/prepare` | Only tiny/base route to the CPU worker. Other values are ignored (F17). Lock `transcription.model_choice` replaces it with "managed by your deployment" | |
| U3 | Reprocess dialog (`SelectiveReprocessModal.svelte`) stage checkboxes | "Transcription & Diarization", "Re-diarize Only (v4)", "Speaker Identification (AI)", plus non-ASR stages | `stages` | — | `rediarize` is disabled for cloud ASR or without word timestamps |
| U4 | Reprocess "Settings" step | "Transcription Model" `#reprocess-model-select`, "Min/Max Speakers" `#modal-min-speakers/#modal-max-speakers`, "Fixed Count" `#modal-num-speakers` (hidden for cloud ASR) | `POST /files/{uuid}/reprocess` or `/files/management/bulk-action` | per file | Does **not** load T2/T3/T4 (F29). `disable_diarization` in the schema is dropped (F2) |
| U5 | File detail → speaker rename → `SpeakerProfileConfirmModal` | "Update Profile Globally" / "Create New Profile" | `profile_action` on `PUT /speakers/{uuid}` | — | An action, not a setting; no change |
| U6 | Speakers page → Clusters tab | "Re-cluster All" | `POST /speaker-clusters/recluster` (no threshold sent) | — | Matching thresholds are hard-coded constants (`constants.py:296-298`, `speaker_clustering_service.py:43-53`); there is no setting to move |
| U7 | URL import / recording / navbar quick-record | none | — | — | Drop speaker counts and model (F30) |

### 1.10 Env-only knobs (not UI-configurable, listed for completeness, no change)

`WHISPER_BEAM_SIZE`, `BATCH_SIZE`, `WHISPER_COMPUTE_TYPE`/`COMPUTE_TYPE`, `WHISPER_HYBRID_MODE`,
`WHISPER_HYBRID_CPU_MODEL`, `WHISPER_LIGHTWEIGHT_MODEL`, `ENABLE_SEGMENT_DEDUP`, `NUM_SPEAKERS`,
`DIAR_NATIVE_*` (URL, timeout, inflight, gender), `DIARIZATION_BATCH_SIZE`,
`DIARIZATION_EMBEDDING_BATCH_SIZE`, `DIAR_OVERLAP`, `ENABLE_OVERLAP_DETECTION`,
`OVERLAP_MIN_DURATION`, `USE_NATIVE_SPEAKER_EMBEDDINGS`, `ENGINE_GPU_SPLIT`,
`SPEAKER_ATTRIBUTE_MAX_CONCURRENCY`, `SPEAKER_ATTRIBUTE_MAX_CLIP_SECONDS`. There are also SS keys
with a reader but no writer: `engine.boundary_max_island_words`, `_max_island_duration`,
`_min_flank_words`, `_min_silent_gap`, `_margin_threshold` (`boundary_resolver.py:97-126`) and
`speaker_attribute.*` (F10/F11).

---

## 2. What each existing section actually is (from code)

| Section | What it really configures | Job | Who |
|---|---|---|---|
| **Transcription Settings** (`transcription`) | A mix of three jobs in one form and one PUT. (a) **Speaker identification**: T1-T5. (b) **Transcription**: T6-T9 and T11-T16 (T6/T7 currently dead). (c) **AI output language**: T10. | mixed | each user, for their own files |
| **ASR Provider** (`asr-provider`) | **Which speech-to-text engine runs.** Either the deployment's local Whisper model (A2, super_admin pins it) or the user's own cloud provider config (A4-A6). The provider choice also *indirectly* affects speakers, because a cloud provider may detect speakers itself (that is what T1="provider" means). | transcription | users pick a provider; super_admin pins the local model |
| **Speech Processing** (`engine-settings`) | **How speakers are separated on the local GPU** (E1, E2) and **how speaker changes at turn boundaries are corrected** (E3-E6). No transcription setting at all (PC1). | speaker identification | super_admin, deployment-wide |
| **Speaker Attributes** (`speaker-attributes`) | Voice-attribute (gender) prediction after transcription, plus a super_admin bulk job. Turning it off also stops AI speaker-name suggestions (F12). | speaker identification | user (bulk: super_admin) |
| **Custom Vocabulary** (`custom-vocabulary`) | Hotwords/keywords that bias **transcription**. | transcription | user |
| **Auto-Label Settings** (`auto-labeling`) | AI tags and collections (PC2). | AI organisation | user |

**What "Speaker Detection" in Transcription Settings controls** (quoting the code):

- **T1 "Speaker Diarization"** is the on/off switch and the choice of detector. Its value is
  resolved in this order: explicit argument → legacy `disable_diarization` bool → user setting
  (`backend/app/tasks/transcription/preprocess.py:265-279`).
  - `off` sets `enable_diarization=False` (`pipelines.py:120,204,338`), assigns everything to
    `SPEAKER_00` (`stages.py:678-687`) and persists `media_file.diarization_disabled`
    (`core.py:470-476`).
  - With local ASR, any value other than `off` diarizes locally on the engine chosen by **E1**.
  - With cloud ASR: `provider` = the provider's own detection (`cloud_asr.py:242`); `pyannote` =
    pyannote.ai in parallel (`cloud_asr.py:255-268`); `local` = GPU re-diarization
    (`postprocess.py:216-238`).
- **T2 "Behavior"** never reaches the backend. It only decides what the upload wizard's Speakers
  step is pre-filled with (`FileUploader.svelte:290-307`).
- **T3/T4** are the speaker range (`pipelines.py:110-112`):
  ```python
  min_speakers=min_speakers if min_speakers is not None else user_settings["min_speakers"],
  max_speakers=max_speakers if max_speakers is not None else user_settings["max_speakers"],
  num_speakers=num_speakers if num_speakers is not None else settings.NUM_SPEAKERS,
  ```
  The default native engine then ignores the range (PC6).
- Speaker **names** are not set here. Names come from the user (file detail and Speakers page)
  or from LLM suggestions (reprocess stage "Speaker Identification (AI)", and the auto-chain
  after attribute detection, F12), and are never auto-applied (root `CLAUDE.md`).

**So the relationship is:**

- **E1** chooses *which local engine* separates voices, for the whole deployment.
- **T1** chooses, per user, *whether* voices are separated and, for cloud transcription, *which
  service* does it.
- **T3/T4** give that engine a speaker-count hint.
- **Speaker Attributes** runs *after* separation to estimate voice traits.
- **Auto-Label** is unrelated.

---

## 3. Duplicates, overlaps, conflicts, dead or misleading controls

Each item names the writer and the reader. "Plan action" says what *this* plan does. Nothing is
deleted here; anything needing pipeline work becomes a ticket (§7.2).

| ID | Finding | Writer | Reader | Plan action |
|---|---|---|---|---|
| D1 | **One concept, two places: speaker count.** T3/T4 (Settings), U1 (upload), U4 (reprocess) and X5 (watch source) all set min/max. Precedence is per file → user → env (`pipelines.py:110-112`). Reprocess (U4) and watch source (X5) do not load the user's values (F29, F31). | `user_settings.py:800-819`; `complete_upload.py:363-365`; `files/__init__.py:1231-1261`; `WatchSourceModal.svelte:261-263` | `pipelines.py:110-112` | Keep all of them. The help text in Settings explains it is a default that a file can override. Tickets F29/F31. |
| D2 | **"pyannote" is two different things** (PC4). | T1 vs E1 | `cloud_asr.py:255-268` vs `model_manager.py:99-180` | Reword both (§5). |
| D3 | **Speaker detection inside Transcription Settings** (PC3) while the speaker engine lives in "Speech Processing" and attributes in a third row. | — | — | Regroup into one **Speaker Identification** row (§4). |
| D4 | **Garbage cleanup: the user control is dead and the live admin control has no UI** ✔. `rg -n 'transcription_garbage_cleanup' backend/app` matches only `user_settings.py`. The two "thresholds" also mean different things: the user schema calls it a threshold, the admin one is a 50-char word length. | `user_settings.py:804-805` | none. The pipeline reads SS via `finalize.py:197,346` | Keep T6/T7 where they are (Transcription → Accuracy & Cleanup). Ticket F6 decides which value wins and wires it. No copy change claims it works or doesn't. |
| D5 | **T2 options are hardcoded English** ✔ (`transcriptionSettings.ts:168-187`), so the "Behavior" dropdown is English in all 12 locales. | — | — | Fix in Step 3 (i18n keys; delete the helpers). |
| D6 | **T2 "use_defaults" sends null; the task then falls back to the user's stored min/max, not the system default** (`pipelines.py:110-111`; `FileUploader.svelte:290-307`). | — | — | Wording avoids promising "system default" values (§5). Ticket F7. |
| D7 | **T1 provider/local/pyannote are identical with local ASR** (PC5). | — | `core.py:470-471` | Wording (§5). |
| D8 | **T3/T4 ignored by the default native engine** (PC6) ✔. | — | `diarizer_native.py:17-21,946-951` | Honest help text (§5). Ticket F1. |
| D9 | **S2/S3 are stored but nothing reads them** ✔ (`rg -n 'speaker_attribute_(gender|age|show)' backend/app`: only `user_settings.py`; `rg -n 'show_attributes_on_cards|gender_detection_enabled' frontend/src`: only the panel and its API module). | `user_settings.py:1267-1269` | none | Keep both. Ticket F11. |
| D10 | **S1 also switches off AI speaker-name suggestions** ✔ (`postprocess.py:725-729`; the chain is dispatched at the end of `detect_speaker_attributes_task`). | — | `speaker_attribute_task.py:197-206` | Add a one-line note under S1 (§5). Ticket F12 to decouple. |
| D11 | **A2/A3 shown to admins, but the endpoints are super_admin** ✔. | `ASRSettings.svelte:315,343` (`isAdmin`) | `asr_settings.py:950-958,1024-1031` | Fix in Step 4: show the controls locked to admins, unlocked to super_admins. |
| D12 | **S5 bulk processing shown to every role, endpoints super_admin** ✔. A plain user gets swallowed 403s (`console.error` at :164). | `SpeakerAttributeSettings.svelte:352` | `speaker_attribute_migration.py:27…` | Move into the super_admin **Maintenance** tab (§4). This is a move, not a removal. |
| D13 | **E1 option labels and E7's error fallback hardcoded English** ✔. | `EngineSettings.svelte:185-186,122` | — | Fix in Step 4. |
| D14 | **A1 "English optimized"/"Multilingual" hardcoded** ✔ (`ASRSettings.svelte:331`). | — | — | Fix in Step 4. |
| D15 | **EngineSettings never reports dirty state** ✔. | — | — | Fix in Step 4 (dispatch `change`). |
| D16 | **Orphan i18n keys** ✔: `settings.transcription.disableDiarization` and `disableDiarizationDesc` have 0 references. Stale hint: `diarizationPyannoteConfigHint` says "ASR Settings tab", but the row is "ASR Provider", and there is **no endpoint that writes the pyannote.ai credential** (`UserDiarizationSettings`, F15). | — | `services/diarization/factory.py:97-106` | Delete the two orphans in Step 7. Rewrite the hint after reading `factory.py:86-120` (Step 3 instructions). |
| D17 | **Speaker Attributes panel has no `.section-title`** (bare `<h3>` at :250). | — | `backend/tests/e2e/test_settings_modal.py:216-223` | The new shell renders the `.section-title` (Step 5). |
| D18 | **Mobile section `<select>` binds `activeSection`, not the effective id**, so an alias (`redaction-policy` today; five more after this plan) has no matching `<option>`. | `SettingsModal.svelte:946` | — | Bind the alias-resolved id (Step 5). |
| D19 | **`Tabs.svelte` arrow keys are not mirrored in RTL** ✔ (no `dir` handling in `ui/Tabs.svelte`). In `ar`, ArrowRight moves visually left. | — | — | Fix in Step 2. |
| D20 | **Tab DOM ids are global** (`tab-${id}`/`tabpanel-${id}`, `Tabs.svelte:66-89`). | — | — | All new tab ids are namespaced (`tx-*`, `spk-*`). |
| D21 | **"Speaker Embedding System" is admin-tier but its migration routes are super_admin** ✔ (F25). | `SettingsModal.svelte` `SECTION_MIN_ROLE['embedding-migration']='admin'` | `embedding_migration.py` | Out of scope (System group). Ticket F25. |

The deeper pipeline findings (F1-F22) are listed with file:line in §7.2.

---

## 4. Proposed information architecture

### 4.1 Decision: one sidebar group, two rows, each a tabbed section

The current group "Transcription" has 6 rows; after this plan it has **2**:

```
TRANSCRIPTION & SPEAKERS                (group, key settings.sections.transcription)
  Transcription                 id: transcription           → TranscriptionSection.svelte
      [Language] [Provider & Model] [Vocabulary] [Accuracy & Cleanup]
  Speaker Identification        id: speaker-identification  → SpeakerIdentificationSection.svelte
      [Speaker Detection] [Voice Attributes] [Speaker Engine 🔒] [Maintenance 🔒]

AI & CHAT
  Chat · LLM Provider Configuration · AI Summarization Prompts · Organization Context
  Auto-Labeling (Tags & Collections)    id: auto-labeling (moved, unchanged panel)
```

Why two rows and not one row with nested tabs: the two jobs use different models, different
pipeline stages and partly different roles. Two rows put the distinction the owner asked for in
the sidebar itself, avoid tabs inside tabs (Vocabulary already has domain sub-tabs), and keep both
rows well under the #861 limit of 6 per group.

Why not one row per old panel: that is the current state, and it is what confused the owner.

### 4.2 Tabs per row (pure helpers)

**`frontend/src/lib/settings/transcriptionTabs.ts`** (new)

```ts
export type TranscriptionTabId = 'tx-language' | 'tx-provider' | 'tx-vocabulary' | 'tx-accuracy';
export interface TranscriptionTab { id: TranscriptionTabId; locked: boolean; }
export interface TranscriptionAccess {
  prefsCap: boolean;   // capability 'transcription.prefs'
  asrCap: boolean;     // capability 'asr.user_providers'
  vocabCap: boolean;   // capability 'vocab.user'
}
export function transcriptionTabs(a: TranscriptionAccess): TranscriptionTab[];
export function transcriptionVisible(a: TranscriptionAccess): boolean; // tabs.length > 0
export function resolveTranscriptionTab(requested: TranscriptionTabId, tabs: TranscriptionTab[]): TranscriptionTabId | null;
```

Rules: `tx-language` and `tx-accuracy` if `prefsCap`; `tx-provider` if `asrCap`; `tx-vocabulary`
if `vocabCap`. Order: language, provider, vocabulary, accuracy (most frequent first). No tab is
role-locked: every endpoint behind them is user-tier, and the super_admin parts of the provider
tab (A2/A3) are locked *inside* the panel (Step 4). `resolve…` follows
`resolvePrivacyRedactionTab` exactly: the requested tab if offered and unlocked, else the first
unlocked tab, else the first tab, else null.

**`frontend/src/lib/settings/speakerIdentificationTabs.ts`** (new)

```ts
export type SpeakerIdTabId = 'spk-detection' | 'spk-attributes' | 'spk-engine' | 'spk-maintenance';
export interface SpeakerIdTab { id: SpeakerIdTabId; locked: boolean; }
export interface SpeakerIdAccess {
  isAdmin: boolean;
  isSuperAdmin: boolean;
  prefsCap: boolean;      // 'transcription.prefs' (same endpoint as today's speaker card)
  engineCap: boolean;     // 'engine.settings'
  migrationCap: boolean;  // 'speaker_attributes.migration'
}
export function speakerIdentificationTabs(a: SpeakerIdAccess): SpeakerIdTab[];
export function speakerIdentificationVisible(a: SpeakerIdAccess): boolean;
export function resolveSpeakerIdTab(requested: SpeakerIdTabId, tabs: SpeakerIdTab[]): SpeakerIdTabId | null;
```

Rules (mirroring `privacyRedactionTabs` and today's sidebar):

- `spk-detection` if `prefsCap`, unlocked.
- `spk-attributes` always (it has no capability today), unlocked.
- `spk-engine` if `isAdmin && engineCap`, `locked: !isSuperAdmin`. This is today's behaviour for
  the `engine-settings` row: absent for plain users (the row sits in an `isAdmin` spread,
  `SettingsModal.svelte:342`), locked for admins, open for super_admins.
- `spk-maintenance` if `isAdmin && migrationCap`, `locked: !isSuperAdmin`.

### 4.3 Section ids and aliases

**`frontend/src/lib/settings/sectionAliases.ts`** (new, pure)

```ts
export type AliasTarget =
  | { row: 'transcription'; tab: TranscriptionTabId }
  | { row: 'speaker-identification'; tab: SpeakerIdTabId };
export const SECTION_ALIASES: Record<'asr-provider' | 'custom-vocabulary' | 'engine-settings' | 'speaker-attributes', AliasTarget>;
// 'asr-provider'       → { row: 'transcription',          tab: 'tx-provider' }
// 'custom-vocabulary'  → { row: 'transcription',          tab: 'tx-vocabulary' }
// 'engine-settings'    → { row: 'speaker-identification', tab: 'spk-engine' }
// 'speaker-attributes' → { row: 'speaker-identification', tab: 'spk-attributes' }
export function sidebarRowFor(section: SettingsSection): SettingsSection;
// alias → its row; 'redaction-policy' → 'content-redaction'; 'chat-admin' → 'chat'; else identity
export function initialTabFor(section: SettingsSection): string | null;
```

| Id | Before | After | Opens |
|---|---|---|---|
| `transcription` | row "Transcription Settings" | row "Transcription" | tab `tx-language` |
| `speaker-identification` | — (new) | row "Speaker Identification" | tab `spk-detection` |
| `asr-provider` | row | **alias** | Transcription › `tx-provider` |
| `custom-vocabulary` | row | **alias** | Transcription › `tx-vocabulary` |
| `engine-settings` | row (super_admin) | **alias** | Speaker Identification › `spk-engine` |
| `speaker-attributes` | row | **alias** | Speaker Identification › `spk-attributes` |
| `auto-labeling` | row in Transcription group | row in **AI & Chat** group | unchanged panel |

Keep every alias in the `SettingsSection` union (`frontend/src/stores/settingsModalStore.ts:3-50`)
and in `dirtyState` (`:61-98`); add `'speaker-identification'` to both. **Remove**
`'engine-settings'` from `SECTION_MIN_ROLE` (`SettingsModal.svelte:118`). It now gates a tab, not
a row; the tab lock is the same super_admin tier. This follows the `redaction-policy` precedent
documented at `SettingsModal.svelte:108-111`. Add `'speaker-identification'` to nothing (no
minimum role).

### 4.4 Feature-parity matrix (every existing control → new home)

"Component" is the file that renders it after the change. ✱ marks a control whose element id must
not change (e2e or tests depend on it).

| # | Control | Old section id › place | New row › tab › card | Component |
|---|---|---|---|---|
| T1 | Speaker Diarization (source) | `transcription` › Speaker Detection | Speaker Identification › Speaker Detection › "How speakers are detected" | `SpeakerDetectionSettings.svelte` (`#diarization-source`) |
| T2 | Behavior (count prompt) ✱ | `transcription` › Speaker Detection | Speaker Identification › Speaker Detection › "Number of speakers" | same (`#speaker-behavior` ✱) |
| T3 | Min Speakers ✱ | `transcription` › Speaker Detection | same card | same (`#min-speakers` ✱) |
| T4 | Max Speakers ✱ | `transcription` › Speaker Detection | same card | same (`#max-speakers` ✱) |
| T5 | System default range (read-only) | `transcription` | same card | same |
| T6 | Enable cleanup | `transcription` › Garbage Word Cleanup | Transcription › Accuracy & Cleanup › "Noise cleanup" | `TranscriptionAccuracySettings.svelte` |
| T7 | Cleanup threshold | same | same card | same |
| T8 | Source Language | `transcription` › Language Settings | Transcription › Language › "Spoken language" | `TranscriptionLanguageSettings.svelte` (`#source-language`) |
| T9 | Translate to English | same | same card | same |
| T10 | AI Summary Language | same | Transcription › Language › "Language for AI features" (own card) | same (`#llm-output-language`) |
| T11-T14 | VAD ×4 | `transcription` › Advanced (collapsible) | Transcription › Accuracy & Cleanup › "Voice activity detection" (**always expanded**, no collapsible) | `TranscriptionAccuracySettings.svelte` (ids unchanged) |
| T15 | Hallucination filter | `transcription` › Advanced › Accuracy | Transcription › Accuracy & Cleanup › "Hallucinations and repetition" | same (`#hallucination-toggle`) |
| T16 | Repetition penalty | same | same card | same (`#repetition-penalty`) |
| T17 | Reset / Save | `transcription` (one pair for everything) | one pair **per tab form** (Language; Accuracy & Cleanup; Speaker Detection). Reset is scoped to that tab's fields (Step 1). | the three forms |
| A1-A6 | All ASR provider controls | `asr-provider` | Transcription › Provider & Model | `ASRSettings.svelte` (unchanged except Step 4 fixes) + `ASRConfigModal.svelte` |
| E1-E2 | Diarizer backend, require sidecar | `engine-settings` | Speaker Identification › Speaker Engine › "Speaker separation engine" | `EngineSettings.svelte` |
| E3-E6 | Boundary smoothing, acoustic re-check, margin, max dur | `engine-settings` | Speaker Identification › Speaker Engine › "Speaker boundary correction" | `EngineSettings.svelte` |
| E7 | Save / per-key reset | `engine-settings` | same tab | `EngineSettings.svelte` |
| V1-V5 | Vocabulary list, add, toggle, delete, bulk import | `custom-vocabulary` | Transcription › Vocabulary | `CustomVocabularySettings.svelte` (unchanged) |
| S1-S4 | Attribute detection prefs, reset/save | `speaker-attributes` | Speaker Identification › Voice Attributes | `SpeakerAttributeSettings.svelte` (bulk block removed from it) |
| S5 | Bulk attribute detection (run / reprocess all / stop / progress) | `speaker-attributes` (all roles) | Speaker Identification › Maintenance (super_admin; locked for admin; absent for plain users, who could never run it) | new `SpeakerAttributeBulkPanel.svelte` (code moved verbatim) |
| L1-L6 | Auto-label controls | `auto-labeling` (Transcription group) | `auto-labeling` row in **AI & Chat** group | `AutoLabelSettings.svelte` (unchanged) |
| X1-X2 | Speaker Embedding System | `embedding-migration` (System) | **unchanged** row in System, plus a link card in Speaker Identification › Maintenance that calls `switchSection('embedding-migration')` | link only, no second copy |
| X3-X6, U1-U7 | Search & Indexing, Audio Extraction, watch-source modal, upload, reprocess, file detail, Speakers page | — | **unchanged** | — |

Count check for the reviewer: 17 + 6 + 7 + 5 + 5 + 6 = **46** rows across §1.2-§1.7. All 46 are
placed above. The implementer must re-run the inventory greps in Step 8 and confirm the count.

### 4.5 Progressive disclosure

- The "Advanced Transcription" collapsible (`advancedExpanded`, `TranscriptionSettings.svelte:64,601-635`)
  goes away. Its contents get their own tab, **Accuracy & Cleanup**, rendered as three always-open
  cards. Nothing on the Language tab expands any more.
- On Speaker Detection, the min/max inputs keep their existing conditional (shown when T2 =
  `use_custom` and T1 ≠ `off`). That is real progressive disclosure tied to a choice, not a
  "show more" expander.
- On Speaker Engine, the four boundary-correction fields sit in their own card under the two
  engine fields. E5/E6 are disabled (not hidden) while E4 is off, so the dependency is visible.
- Each tab label carries a `●` badge (via `TabItem.badge`) while that tab has unsaved edits, so a
  user switching tabs can see where the pending changes are.

### 4.6 Mounting and dirty state

Follow `PrivacyRedactionSettings.svelte` exactly:

- **Lazy-mount on first visit, then keep mounted** (`hidden` attribute) so edits survive a tab
  switch.
- Each panel renders `role="tabpanel" id="tabpanel-<tabId>" aria-labelledby="tab-<tabId>"`.
- A locked active tab shows the `EmptyState` with `settings.permission.superAdminTitle` /
  `superAdminMessage`.
- The tab strip renders only when there is more than one tab.

Dirty state: the **shell** owns `settingsModalStore.setDirty(<row id>, anyChildDirty)`. Children
dispatch `change` with `{ hasChanges }`, as `TranscriptionSettings` already does at :120. This is
the parent-report pattern `settings/CLAUDE.md` prescribes (CacheSettings → RetentionSettings).

Two existing self-reporting calls change:

- `SpeakerAttributeSettings.svelte:46` stops calling `setDirty('speaker-attributes', …)` and
  dispatches `change` instead.
- `AutoLabelSettings` is untouched (it is still its own row).

---

## 5. Wording (en) and i18n keys

Principle: every label says which job it belongs to. "Transcription" means **words**, "Speaker
identification" means **who spoke**. Field-level help never promises behaviour the code does not
deliver (PC5, PC6, D10).

### 5.1 Keys to ADD (all 12 locales; real translations; check `ar` renders RTL)

| Key | en |
|---|---|
| `settings.speakerIdentification.title` | Speaker Identification |
| `settings.speakerIdentification.description` | Work out who spoke when: separate the voices, set how many speakers to expect, and estimate voice attributes. These settings never change the transcribed words. |
| `settings.speakerIdentification.tabs.ariaLabel` | Speaker identification settings |
| `settings.speakerIdentification.tabs.detection` | Speaker Detection |
| `settings.speakerIdentification.tabs.attributes` | Voice Attributes |
| `settings.speakerIdentification.tabs.engine` | Speaker Engine |
| `settings.speakerIdentification.tabs.maintenance` | Maintenance |
| `settings.speakerIdentification.detectionHeading` | How speakers are detected |
| `settings.speakerIdentification.detectionDesc` | Split the recording into separate voices so each line of the transcript is labeled with a speaker. |
| `settings.speakerIdentification.source.label` | Speaker detection |
| `settings.speakerIdentification.source.provider` | Automatic (recommended) |
| `settings.speakerIdentification.source.providerDesc` | With the built-in transcription model, speakers are detected on this server's GPU. With a cloud transcription provider, that provider detects speakers. |
| `settings.speakerIdentification.source.local` | Always on this server's GPU |
| `settings.speakerIdentification.source.localDesc` | Detect speakers locally even when a cloud provider transcribes. Best quality; needs a GPU worker. |
| `settings.speakerIdentification.source.pyannote` | pyannote.ai cloud service |
| `settings.speakerIdentification.source.pyannoteDesc` | Send audio to the hosted pyannote.ai service to detect speakers when a cloud provider transcribes. This is not the PyAnnote fallback engine under Speaker Engine. |
| `settings.speakerIdentification.source.pyannoteHint` | *(implementer: write from `services/diarization/factory.py:86-120`. Say where the API key really comes from. If nothing can set it, say "Requires a pyannote.ai API key configured by your administrator" only if an env fallback exists there. Otherwise keep the option, use a neutral "Requires a pyannote.ai API key" and cite F15 in the commit message.)* |
| `settings.speakerIdentification.source.off` | Off (one speaker for the whole recording) |
| `settings.speakerIdentification.source.offDesc` | Skip speaker detection. Use for dictation, voicemail or single-speaker recordings. |
| `settings.speakerIdentification.source.offWarning` | Speaker profiles and cross-file speaker matching will not run. |
| `settings.speakerIdentification.source.offNote` | Speaker detection is off: every segment is assigned to one speaker. |
| `settings.speakerIdentification.countHeading` | Number of speakers |
| `settings.speakerIdentification.countDesc` | A hint for how many people speak. A single upload or reprocess can override it. The PyAnnote fallback engine and cloud providers apply the range; the default local engine counts speakers automatically. |
| `settings.speakerIdentification.countMode.label` | Speaker count when uploading |
| `settings.speakerIdentification.countMode.alwaysPrompt` | Start from my range and let me change it per file |
| `settings.speakerIdentification.countMode.alwaysPromptDesc` | The upload wizard's Speakers step is pre-filled with your range below. |
| `settings.speakerIdentification.countMode.useDefaults` | Leave the range empty on upload |
| `settings.speakerIdentification.countMode.useDefaultsDesc` | The upload wizard starts with no range, so the default range applies. |
| `settings.speakerIdentification.countMode.useCustom` | Always use my range |
| `settings.speakerIdentification.countMode.useCustomDesc` | Your saved range below is used on every upload. |
| `settings.speakerIdentification.minSpeakers` | Minimum speakers |
| `settings.speakerIdentification.maxSpeakers` | Maximum speakers |
| `settings.speakerIdentification.systemDefaults` | System default range: |
| `settings.speakerIdentification.minMaxFormat` | {{min}} to {{max}} |
| `settings.speakerIdentification.validationMinMax` / `validationMinRange` / `validationMaxRange` | *(move the existing en text verbatim)* |
| `settings.speakerIdentification.saved` / `saveFailed` / `resetSuccess` / `resetFailed` / `loadFailed` | Speaker detection settings saved / Failed to save speaker detection settings / Speaker detection settings reset to defaults / Failed to reset speaker detection settings / Failed to load speaker detection settings |
| `settings.speakerIdentification.maintenance.heading` | Speaker maintenance |
| `settings.speakerIdentification.maintenance.desc` | Library-wide jobs that re-run speaker analysis on files you already have. |
| `settings.speakerIdentification.maintenance.embeddingLinkTitle` | Speaker voiceprints |
| `settings.speakerIdentification.maintenance.embeddingLinkDesc` | Upgrade or repair the voiceprints used to recognise the same speaker across files. |
| `settings.speakerIdentification.maintenance.embeddingLinkButton` | Open Speaker Embedding System |
| `settings.transcription.tabs.ariaLabel` | Transcription settings |
| `settings.transcription.tabs.language` | Language |
| `settings.transcription.tabs.provider` | Provider & Model |
| `settings.transcription.tabs.vocabulary` | Vocabulary |
| `settings.transcription.tabs.accuracy` | Accuracy & Cleanup |
| `settings.transcription.aiLanguageHeading` | Language for AI features |
| `settings.transcription.aiLanguageDesc` | Used for AI summaries, topics and speaker-name suggestions. It does not change the transcript. |
| `settings.transcription.accuracyTabDesc` | Fine-tune how speech is found and cleaned up before and after transcription. The defaults work well for most audio. |
| `settings.transcription.languageSaved` / `accuracySaved` | Language settings saved / Accuracy settings saved |
| `settings.speakerAttributes.llmCouplingNote` | Turning this off also stops automatic AI speaker-name suggestions after transcription. |
| `settings.engineSettings.diarizerHeading` | Speaker separation engine |
| `settings.engineSettings.boundaryHeading` | Speaker boundary correction |
| `settings.engineSettings.backendNative` | Native engine (default) |
| `settings.engineSettings.backendPyannote` | PyAnnote fallback engine |
| `settings.engineSettings.resetFailed` | Failed to reset {{key}} |
| `settings.asrProvider.capEnglishOptimized` | English optimized |
| `settings.asrProvider.capMultilingual` | Multilingual |
| `settings.asrProvider.localModelLockedHint` | Only a super admin can change the local model. |

### 5.2 Keys whose en TEXT changes (key unchanged; update all 12 locales)

| Key | Old en | New en |
|---|---|---|
| `settings.sections.transcription` | Transcription | Transcription & Speakers |
| `settings.transcription.title` | Transcription Settings | Transcription |
| `settings.transcription.description` | Configure transcription quality, speaker diarization, and garbage segment cleanup preferences. | Turn speech into text: language, transcription provider and model, vocabulary and accuracy. Who spoke when is set under Speaker Identification. |
| `settings.transcription.languageSettings` | Language Settings | Spoken language |
| `settings.transcription.languageSettingsDesc` | Configure language preferences for transcription and AI analysis. | The language of the recording, and whether to translate it to English. |
| `settings.transcription.garbageCleanup` | Garbage Word Cleanup | Noise cleanup |
| `settings.transcription.accuracyTitle` | Accuracy | Hallucinations and repetition |
| `settings.engineSettings.title` | Speech Processing | Speaker Engine |
| `settings.engineSettings.description` | Advanced configuration for transcription and diarization engine backends. Changes take effect on the next processing task. | Which engine separates speakers on this server, and how speaker changes are corrected at turn boundaries. Applies to every user; takes effect on the next file processed. |
| `settings.speakerAttributes.title` | Speaker Attribute Detection | Voice Attributes |
| `settings.asrProvider.diarizationQuality` | *(current en)* | Built-in speaker detection |
| `autoLabel.title` | Auto-Label Settings | Auto-Labeling (Tags & Collections) |

### 5.3 Keys to DELETE in Step 7 (after every consumer has moved)

- `settings.transcription.`: `speakerDetection`, `speakerDetectionDesc`,
  `speakerDetectionTooltip`, `diarizationSource`, `diarizationProvider`, `diarizationLocal`,
  `diarizationPyannote`, `diarizationOff`, `diarizationProviderDesc`, `diarizationLocalDesc`,
  `diarizationPyannoteDesc`, `diarizationOffDesc`, `diarizationPyannoteConfigHint`,
  `disableDiarization` (orphan today), `disableDiarizationDesc` (orphan today),
  `disableDiarizationWarning`, `speakerDetectionDisabled`, `behavior`, `behaviorTooltipAlwaysAsk`,
  `behaviorTooltipAlwaysAskDesc`, `behaviorTooltipUseDefaults`, `behaviorTooltipUseDefaultsDesc`,
  `behaviorTooltipUseCustom`, `behaviorTooltipUseCustomDesc`, `minSpeakers`, `maxSpeakers`,
  `systemDefaults`, `minMaxFormat`, `validationMinMax`, `validationMinRange`,
  `validationMaxRange`, `advancedSettings`, `advancedSettingsDesc`, `advancedSettingsTooltip`,
  `saved`, `resetSuccess`.
- `settings.speakerAttributes.navTitle` (the row is gone).

Before deleting each key, `rg -n "<key>['\"]" frontend/src --glob '!**/locales/**'` must return
nothing.

**Translation rule:** where a key only *moves* and its en text is unchanged (e.g.
`validationMinMax`), copy each locale's existing translation to the new key. Do not
re-translate. New or changed en text needs a real translation in all 11 non-en locales. `npm run
check:i18n` checks parity only (root `CLAUDE.md`); review the `ar`, `ja` and `zh` strings by eye in
the after-screenshots. A key copied with English text into another locale is a defect.

---

## 6. Implementation plan

### 6.0 Rules for every step

- Work in a dedicated worktree on a branch off `feat/v060-settings-ia-plan` (or the active
  v0.6.0 integration branch, if the owner says so). **One writer commits** (root `CLAUDE.md`).
  Parallel lanes either use their own worktrees, or hand paths and a message to a single
  committer.
- Each step is one conventional commit, e.g. `feat(settings): …`, `fix(settings): …`,
  `test(e2e): …`, `docs(settings): …`.
- Red first: write the test, watch it fail against the old code with the `git archive HEAD`
  recipe (root `CLAUDE.md`), then implement.
- Panels stay under ~300 lines where practical. The split in Step 3 exists partly for this.
- Never `--no-verify`. Run `scripts/safe-precommit.sh run --all-files`, plus `--hook-stage
  pre-push` before pushing.
- Frontend checks after each frontend step (in `frontend/`):
  `npm run check && npm run lint && npm run test && npm run test:audit && npm run check:i18n && npm run build`.
- Backend checks after Step 1: `./scripts/run-backend-tests.sh tests/api/test_user_settings.py`,
  then `cd backend && python3 ../scripts/audit-tests.py tests` (0 open findings).

### 6.1 Dependency graph

```
Step 0 (i18n add) ─┬─────────────────────────────────────────────┐
Step 1 (backend scoped reset) ─┐                                 │
Step 2 (Tabs RTL) ─────────────┤                                 │
Step 2b (pure helpers) ────────┤                                 │
                               ├─ Step 3 (split forms) ──┐       │
                               └─ Step 4 (panel fixes) ──┴─ Step 5 (shells + modal) ─ Step 6 (e2e + docs) ─ Step 7 (delete old keys) ─ Step 8 (verify + after-screens)
```

These can run **in parallel**, because no two of them touch the same file:

- Step 0: locale JSON only.
- Step 1: backend only.
- Step 2: `ui/Tabs.svelte` and its test.
- Step 2b: new `$lib/settings/*` files.

Steps 3 and 4 can run in parallel with each other. Step 3 owns `TranscriptionSettings*`, the new
forms and `lib/api/transcriptionSettings.ts`. Step 4 owns `EngineSettings`, `ASRSettings`,
`SpeakerAttributeSettings` and the new `SpeakerAttributeBulkPanel`. Steps 5-8 are serial.

### Step 0 — Add the new i18n keys (all 12 locales)

- **Files:** `frontend/src/lib/i18n/locales/{ar,de,en,es,fr,it,ja,ko,nl,pt,ru,zh}.json`.
- Add every key in §5.1 and change every text in §5.2. **Do not delete** any key yet (Step 7);
  the old components still use them.
- **Red:** none (data only). `npm run check:i18n` must pass.
- ⚠ A commit that touches only locale JSON does not fire the `frontend-check` hook (root
  `CLAUDE.md`, i18n trap). Run `cd frontend && npm run check:i18n` by hand.
- Commit: `feat(i18n): add transcription and speaker identification settings strings`.

### Step 1 — Backend: scoped reset of transcription settings

- **Files:** `backend/app/core/constants.py`, `backend/app/api/endpoints/user_settings.py`,
  `backend/tests/api/test_user_settings.py`.
- Add to `constants.py`, next to the other transcription defaults (~:621-625):
  ```python
  TRANSCRIPTION_SETTING_GROUPS: dict[str, tuple[str, ...]] = {
      "language": ("source_language", "translate_to_english", "llm_output_language"),
      "accuracy": ("garbage_cleanup_enabled", "garbage_cleanup_threshold", "vad_threshold",
                   "vad_min_silence_ms", "vad_min_speech_ms", "vad_speech_pad_ms",
                   "hallucination_silence_threshold", "repetition_penalty"),
      "speakers": ("diarization_source", "speaker_prompt_behavior", "min_speakers", "max_speakers"),
  }
  ```
  The union must equal the 15 fields of `setting_mappings` (`user_settings.py:800-819`). Assert
  that in a unit test, so a 16th field cannot be added without a group.
- `reset_transcription_settings` (`user_settings.py:846`) gains
  `group: Literal["language", "accuracy", "speakers"] | None = Query(None)`. When set, it deletes
  only `transcription_<field>` for that group's fields. When absent, it behaves exactly as today
  (all 15). The response shape (`message`, `default_settings`) is unchanged. Unknown values get a
  422 from FastAPI's Literal validation.
- **Red tests** (in `tests/api/test_user_settings.py`, following `test_transcription_update_round_trip` at :201):
  1. PUT `source_language="de"` and `min_speakers=3`, then DELETE `?group=speakers`, then GET:
     expect `min_speakers` back to default **and `source_language == "de"`**. This fails on the
     old code, which ignores the param and wipes both.
  2. DELETE `?group=bogus` → 422. Fails on the old code (200).
  3. DELETE with no param still resets everything (regression guard; passes on both).
  4. `set().union(*TRANSCRIPTION_SETTING_GROUPS.values())` equals the keys of the PUT mapping.
     Fails on the old code (import error).
- ⚠ Editing `backend/app/*.py` hot-reloads the dev backend and dispatches
  `search_index_maintenance` (root `CLAUDE.md`). Batch both app files into one save window.
- Commit: `feat(api): scope transcription settings reset to a field group`.

### Step 2 — `Tabs.svelte`: mirror arrow keys in RTL

- **Files:** `frontend/src/components/ui/Tabs.svelte`, `frontend/src/components/ui/Tabs.test.ts`.
- In `onKeydown`, read `const rtl = (event.currentTarget as HTMLElement).closest('[dir]')?.getAttribute('dir') === 'rtl';`
  and swap `ArrowLeft`/`ArrowRight` when `rtl` is true. Up/Down/Home/End are unchanged.
- **Red test:** render inside `<div dir="rtl">` (use a small wrapper component, or set `dir` on
  `document.documentElement` in the test and reset it after). Press `ArrowLeft` on Alpha and
  expect Beta to be selected. The old code selects Gamma (it wraps backwards).
- Commit: `fix(ui): mirror tab arrow-key navigation in right-to-left layouts`.

### Step 2b — Pure helpers

- **Files (new):** `frontend/src/lib/settings/transcriptionTabs.ts` (+`.test.ts`),
  `speakerIdentificationTabs.ts` (+`.test.ts`), `sectionAliases.ts` (+`.test.ts`).
  Signatures and rules: §4.2 and §4.3.
- **Red tests** (written first; on the old tree they fail with "module not found"):
  - `transcriptionTabs`:
    - all caps → 4 tabs in order;
    - `asrCap=false` drops `tx-provider`;
    - `prefsCap=false` drops language and accuracy;
    - all caps off → `transcriptionVisible` is false;
    - `resolve` falls back to the first tab when the requested one is absent.
  - `speakerIdentificationTabs`:
    - plain user → `[detection, attributes]` (no engine, no maintenance);
    - admin → engine and maintenance present and `locked: true`;
    - super_admin → all four unlocked;
    - `engineCap=false` drops engine;
    - `migrationCap=false` drops maintenance;
    - `resolve('spk-engine')` for an admin → `spk-detection` (locked tab not honoured);
    - all locked → the first tab.
  - `sectionAliases`:
    - each of the four aliases maps to its row and tab;
    - `redaction-policy` → `content-redaction`;
    - `chat-admin` → `chat`;
    - an ordinary id maps to itself;
    - `initialTabFor('transcription')` is `null` (the shell's default applies).
- Commit: `feat(settings): tab and alias helpers for transcription and speaker identification`.

### Step 3 — Split `TranscriptionSettings.svelte` into three forms

- **Files:**
  - **new** `frontend/src/components/settings/TranscriptionLanguageSettings.svelte` (T8-T10)
  - **new** `TranscriptionAccuracySettings.svelte` (T6, T7, T11-T16)
  - **new** `SpeakerDetectionSettings.svelte` (T1-T5)
  - **delete** `TranscriptionSettings.svelte`
  - **move/split** `TranscriptionSettings.locks.test.ts` into `TranscriptionAccuracySettings.locks.test.ts` (the `transcription.advanced` lock) and `SpeakerDetectionSettings.locks.test.ts` (the `transcription.diarization_source` lock)
  - `frontend/src/lib/api/transcriptionSettings.ts`
- `lib/api/transcriptionSettings.ts`:
  - `resetTranscriptionSettings(group?: 'language'|'accuracy'|'speakers')` sends `params: { group }`.
  - **Delete** `getSpeakerBehaviorLabel` and `getSpeakerBehaviorDescription`; the options use the
    §5.1 `countMode.*` keys.
  - Keep `DEFAULT_TRANSCRIPTION_SETTINGS`, because `FileUploader.svelte` uses it.
- Each form:
  - loads with `getTranscriptionSettings()` (+ `getTranscriptionSystemDefaults()` where needed);
  - saves **only its own fields** with `updateTranscriptionSettings(partial)`;
  - resets with its group;
  - dispatches `change` `{ hasChanges }`;
  - does **not** call `settingsModalStore.setDirty` (the shell does).
- Root classes: `.transcription-language-settings`, `.transcription-accuracy-settings`,
  `.speaker-detection-settings`. Keep every element id in §4.4.
- Keep the deployment-lock behaviour exactly:
  - T1 hidden and left out of the payload when `transcription.diarization_source` is off;
  - T11-T16 hidden and left out of the payload when `transcription.advanced` is off. The
    Accuracy tab still shows T6/T7.
- Accuracy form: drop the `advancedExpanded` collapsible and render the VAD and accuracy cards
  open.
- Language form: T10 in its own card with `aiLanguageHeading` / `aiLanguageDesc`.
- `SpeakerDetectionSettings`:
  - uses the §5.1 keys;
  - add the `pyannoteHint` per the §5.1 instruction (read `services/diarization/factory.py:86-120` first);
  - keep the T2 tooltip content, rewritten with the `countMode.*Desc` strings.
- **Red tests** (vitest, mock `$lib/api/transcriptionSettings` as the existing locks test does):
  - Saving in `SpeakerDetectionSettings` calls `updateTranscriptionSettings` with exactly
    `{diarization_source, speaker_prompt_behavior, min_speakers, max_speakers}`, and no
    `source_language`. Red: the component does not exist.
  - Reset in each form calls `resetTranscriptionSettings` with its group.
  - The `#speaker-behavior` options render the translation keys, not English. Red against the
    old component: the label is "Always show speaker settings".
  - Both lock tests carry over unchanged in intent.
- Commit: `refactor(settings): split transcription settings into language, accuracy and speaker detection forms`.

### Step 4 — Panel fixes inside existing components (parallel with Step 3)

- **Files:**
  - `EngineSettings.svelte`, `EngineSettings.requireSidecar.test.ts` (extend)
  - **new** `EngineSettings.dirty.test.ts`
  - `ASRSettings.svelte`, **new** `ASRSettings.localModelRole.test.ts`
  - `SpeakerAttributeSettings.svelte`
  - **new** `SpeakerAttributeBulkPanel.svelte`, **new** `SpeakerAttributeBulkPanel.test.ts`
- `EngineSettings`:
  - group E1-E2 under `diarizerHeading` and E3-E6 under `boundaryHeading`;
  - options use `backendNative` / `backendPyannote`;
  - the reset error uses `$t('settings.engineSettings.resetFailed', { key })`;
  - E5/E6 `disabled` when `draftAcousticRecheck` is false;
  - dispatch `change` `{ hasChanges: isDirty }`.
  - **Red:** a test that changes the backend select and expects a `change` event with
    `hasChanges: true` (old: no event). A second test that the options render the keys (old:
    "native (default)").
- `ASRSettings`:
  - add `export let isSuperAdmin = false`;
  - keep the info card for everyone;
  - for `isAdmin && !isSuperAdmin`, render the model select and both buttons **disabled**, with
    `localModelLockedHint` and the `settings.nav.requiresSuperAdmin` tooltip;
  - enable them only for `isSuperAdmin`;
  - replace the hardcoded "English optimized"/"Multilingual" with the new keys.
  - **Red:** with `isAdmin=true, isSuperAdmin=false`, `#local-model-select` is `disabled` (old:
    enabled).
- `SpeakerAttributeSettings`:
  - move the whole `{#if migrationAvailable}` bulk block (:351-~470) and its script state
    (`loadMigrationStatus`, start/stop/force handlers, polling, confirm modal) **verbatim** into
    `SpeakerAttributeBulkPanel.svelte`;
  - add `llmCouplingNote` under `#detection-enabled`;
  - replace `settingsModalStore.setDirty('speaker-attributes', …)` with `dispatch('change', { hasChanges: isDirty })`.
  - **Red:** a `SpeakerAttributeSettings` test asserting it renders **no** bulk section, and
    makes no `/speaker-attributes/migration/status` request, even with the capability on (old:
    it requests). A `SpeakerAttributeBulkPanel` test asserting it requests the status and
    renders the run button.
- Commit: `fix(settings): role-correct and translate the speaker engine, ASR model and attribute panels`.

### Step 5 — Section shells and SettingsModal wiring

- **Files:**
  - **new** `frontend/src/components/settings/TranscriptionSection.svelte`
  - **new** `SpeakerIdentificationSection.svelte`
  - **new** `SpeakerIdentificationSection.test.ts` and `TranscriptionSection.test.ts`
  - `frontend/src/components/SettingsModal.svelte`
  - `frontend/src/components/SettingsModal.test.ts`
  - `frontend/src/stores/settingsModalStore.ts`
  - `frontend/src/lib/search/settingsSearchIndex.ts`, `settingsSearchIndex.test.ts`
- **Shells** are modelled line-for-line on `PrivacyRedactionSettings.svelte`:
  - props `initialTab`, role flags, caps;
  - specs from the helper; `resolve…` keeps a locked or vanished tab from staying active;
  - `visited` lazy-mount; `hidden` tabpanels; `EmptyState` for a locked active tab;
  - `data-testid="transcription-section"` / `"speaker-identification-section"`;
  - the dirty OR across children → `settingsModalStore.setDirty('transcription' | 'speaker-identification', any)`;
  - a `●` badge on a dirty tab.
  - `TranscriptionSection` children: Language, `ASRSettings {isAdmin} {isSuperAdmin}`,
    `CustomVocabularySettings`, Accuracy.
  - `SpeakerIdentificationSection` children: `SpeakerDetectionSettings`,
    `SpeakerAttributeSettings`, `EngineSettings`, and the Maintenance tab, which holds
    `SpeakerAttributeBulkPanel` plus the embedding link card. The link card dispatches
    `navigate` with `'embedding-migration'`; the modal calls `switchSection`. Render the card
    only when `isAdmin` (`embedding-migration` is admin-tier).
- **`SettingsModal.svelte`:**
  1. Sidebar (`:335-347`):
     - group key unchanged;
     - rows become `transcription` (label `settings.transcription.title`, cap removed: visibility now comes from `transcriptionVisible`) and `speaker-identification` (label `settings.speakerIdentification.title`, icon `user`, visibility from `speakerIdentificationVisible`);
     - delete the `asr-provider`, `engine-settings`, `custom-vocabulary`, `speaker-attributes` and `auto-labeling` rows from this group;
     - add `auto-labeling` as the last row of the **AI & Chat** group (`:349-358`).
     - Compute both access objects from `isAdmin`, `isSuperAdmin` and `capOn(capState, …)`, the
       same way `privacyAccess` is built.
  2. `SECTION_MIN_ROLE`: remove `'engine-settings'` (§4.3), and update the doc comment at
     `:107-111` to name the new aliases.
  3. `effectiveActiveSection` (`:298-303`): replace the `redaction-policy` ternary with
     `sidebarRowFor(activeSection)`, keeping the `system-statistics` capability fallback.
  4. Render blocks:
     - replace the `transcription`, `asr-provider`, `engine-settings`, `custom-vocabulary` and
       `speaker-attributes` blocks (`:1070-1076, 1088-1092, 1149-1173`) with two blocks;
     - `{#if sidebarRowFor(activeSection) === 'transcription'}` renders `<h3 class="section-title">{$t('settings.transcription.title')}</h3>`, the description and `<TranscriptionSection initialTab={initialTabFor(activeSection) ?? 'tx-language'} …/>`;
     - do the same for `speaker-identification` (default `'spk-detection'`).
     - The modal-level `.section-title` must stay the **first** `.section-title` in
       `.settings-content`: e2e and vitest use `.first`. Child forms may keep their own `h3`s,
       but give them a class other than `.section-title`.
  5. `visibleSections` (`:444-455`): next to the `redaction-policy` synthetic entry, add synthetic
     entries for each alias whose target tab is present **and unlocked**:
     - `asr-provider` (label `settings.asrProvider.title`)
     - `custom-vocabulary`
     - `speaker-attributes` (label `settings.speakerAttributes.title`)
     - `engine-settings` (super_admin only)

     This keeps them in the search index without re-adding sidebar rows.
  6. Mobile `<select>` (`:944-950`): `value={effectiveActiveSection}` (fixes D18).
- **`settingsModalStore.ts`:** add `'speaker-identification'` to the union and `dirtyState`.
- **`settingsSearchIndex.ts` `SECTION_NAMESPACES`:**
  - add `'speaker-identification': ['settings.speakerIdentification']`;
  - keep `asr-provider`, `engine-settings`, `custom-vocabulary` and `speaker-attributes` as they are (they now resolve to alias ids);
  - `transcription` stays `['settings.transcription']`. After Step 3 the speaker keys live under `settings.speakerIdentification`, so a search for "min speakers" lands on Speaker Identification.
  - Update the doc comment (`:33-37`).
- **Red tests:**
  - `SettingsModal.test.ts`: replace the engine-row case (`:274-289`) with these cases:
    - no `engine-settings` nav row exists for any role;
    - a plain user sees `speaker-identification`;
    - `open('engine-settings')` as an admin shows the Speaker Identification row active, with `#tab-spk-engine` `aria-selected` false and disabled;
    - as a super_admin, `#tab-spk-engine` is selected and `.engine-settings` renders;
    - `open('asr-provider')` selects `#tab-tx-provider`;
    - `open('custom-vocabulary')` selects `#tab-tx-vocabulary`;
    - `open('speaker-attributes')` selects `#tab-spk-attributes`;
    - `auto-labeling`'s `sectionOf` is `settings.sections.aiChat`;
    - the `asr.user_providers:false` case at `:160-175` now asserts the **tab** `#tab-tx-provider` is absent (the nav row no longer exists).

    Each fails on the old code.
  - Shell tests:
    - the tab strip renders the helper's tabs;
    - switching tabs keeps the first panel mounted (`hidden`, not removed);
    - a child `change` with `hasChanges:true` sets `dirtyState['speaker-identification']`.
  - `settingsSearchIndex.test.ts`:
    - fixtures use the current titles (replace `'Engine Configuration'`, `:10, :28`);
    - a `settings.speakerIdentification.minSpeakers` leaf resolves to `speaker-identification`.
- Commit: `feat(settings): split Transcription and Speaker Identification into tabbed sections`.

### Step 6 — E2E, docs and CLAUDE.md

- **Files:**
  - `backend/tests/e2e/test_settings_modal.py`
  - `frontend/src/components/settings/CLAUDE.md`, `frontend/src/components/upload/CLAUDE.md` (only if a selector note changes)
  - docs (list below), `CHANGELOG.md`, `README.md:208-209`, `backend/app/transcription/CLAUDE.md:213`, `backend/app/services/CLAUDE.md:495`
- **e2e** (`test_settings_modal.py`):
  - `:51` becomes `("Transcription", "Transcription")`. `has_text` is a substring match; the only
    nav item containing "Transcription" will be the Transcription row.
  - Add `("Speaker Identification", "Speaker Identification")` to `SECTIONS_TO_SWITCH`.
  - `:251-262` `test_transcription_controls_render`: open `"Speaker Identification"`. `#speaker-behavior`, `#min-speakers` and `#max-speakers` are unchanged.
  - `:298-355` persistence:
    - open `"Speaker Identification"`;
    - save selector `.speaker-detection-settings .btn-primary`;
    - the API round-trip on `speaker_prompt_behavior` is unchanged.
  - **New** `TestTranscriptionSpeakerTabs`:
    - as the super_admin fixture, open each row;
    - assert the four `role="tab"` elements by id (`#tab-tx-language` …, `#tab-spk-detection` …);
    - press ArrowRight on the first tab and assert focus and selection move;
    - open the Speaker Engine tab and assert `#diarizer-backend` is visible.
    - The test must never click Save; it does not mutate dev data (root `CLAUDE.md`).
  - Run: `./scripts/e2e/run-e2e.sh -k settings_modal`, plus the smoke suite.
- **docs** — rewrite each path phrase to the new names. Each was found by the reference sweep; re-run
  `rg -n "Settings (→|>|->) (Transcription|Speech Processing|Engine|ASR|Embeddings|Admin → Diarization|Auto-Label|AI → Auto-Label|Speaker Attributes)" docs-site/docs docs README.md`
  to catch new ones.
  - `docs-site/docs/features/boundary-correction.md:9,73,99` → "Settings → Speaker Identification → Speaker Engine"
  - `docs-site/docs/user-guide/admin-panel.md:124-126` (rename the section, keep the "formerly" note naming both old titles)
  - `features/transcription.md:52,90,166,368,384`
  - `features/speaker-diarization.md:228,279,288,294,401`
  - `user-guide/speaker-management.md:32,112`
  - `user-guide/uploading-files.md:103-104`
  - `user-guide/ai-summarization.md:138,146`
  - `features/llm-integration.md:35,69`
  - `faq.md:120,226`
  - `configuration/environment-variables.md:133,608,1080,1290`
  - `developer-guide/diarization-boundary-correction.md:174`
  - Leave the unrelated stale references (`faq.md:311`, `neural-search-setup.md`,
    `embedding-migration.md`, `operations/*`, `prompt-engineering.md`) to ticket F32; they are not
    about these sections.
- **CLAUDE.md** (`frontend/src/components/settings/CLAUDE.md`): replace the `engine-settings` bullet with a paragraph covering:
  - the two tabbed rows;
  - the helpers;
  - the alias table;
  - "Speaker Engine is a tab, not a row";
  - children dispatch `change` and the shell owns `setDirty`;
  - the E2E-guarded `.speaker-detection-settings`.
- `CHANGELOG.md` → `[Unreleased]` → Changed.
- Commit (one or two): `test(e2e): cover the transcription and speaker identification tabs` and `docs(settings): document the transcription and speaker identification sections`.

### Step 7 — Delete superseded i18n keys

- **Files:** the 12 locale JSONs.
- Delete the §5.3 keys only after the `rg` guard in §5.3 returns nothing for each one. Run
  `npm run check:i18n` by hand (see the Step 0 warning).
- Commit: `chore(i18n): drop strings superseded by the speaker identification section`.

### Step 8 — Verification and after-screenshots

1. Frontend (in `frontend/`): `npm run check && npm run lint && npm run test && npm run test:audit && npm run test:audit:selftest && npm run check:i18n && npm run build`.
2. Backend: `./scripts/run-backend-tests.sh tests/api/test_user_settings.py tests/api/test_deployment_locked_controls.py`, then `cd backend && python3 ../scripts/audit-tests.py tests`.
3. Pre-commit, both tiers: `scripts/safe-precommit.sh run --all-files` and `scripts/safe-precommit.sh run --all-files --hook-stage pre-push`.
4. E2E: `./scripts/e2e/run-e2e.sh -k "settings_modal"` and `./scripts/e2e/run-e2e-smoke.sh` (stack up via `./opentr.sh start dev`, or a `--fresh` stack if other agents are active).
5. **Parity re-count.** For each control id in §4.4, `rg -n 'id="<id>"' frontend/src/components/settings` must find exactly one component. Re-count the 46 inventory rows, and confirm the 6 old section ids still open something (vitest from Step 5).
6. **After-screenshots.** Copy `/tmp/ia-plan/settings-shots.mjs` (if gone, rebuild from the
   description in "Before screenshots") and capture each of the 8 tabs, plus Auto-Labeling in
   AI & Chat, in:
   - light and dark, en, as super_admin (`admin@example.com`);
   - `ar` (RTL) light: check the tab order mirrors, the arrow keys follow it, and the padlock
     badge sits after the label;
   - one capture as a non-super admin, **only if an admin-role test account already exists**. Do
     not create accounts on the shared dev data. Otherwise rely on the vitest role tests.

   Attach the before/after pairs to the PR.
7. **a11y:**
   - the tablist has `aria-label`;
   - each panel has `role="tabpanel"` and `aria-labelledby`;
   - locked tabs are `disabled` with a `title`;
   - Tab moves into the tablist, the arrow keys move between tabs, and Tab again enters the panel;
   - optionally run `test_a11y.py` (axe) against both sections.

---

## 7. Risks, open questions, out-of-scope findings

### 7.1 Open questions for the owner (each has a default, so work is not blocked)

1. **Two sidebar rows ("Transcription", "Speaker Identification") under one group "Transcription &
   Speakers", or one row with both as top-level tabs?** *Default: two rows* (§4.1). It shows
   the distinction the owner asked for in the sidebar and avoids nested tabs.
2. **Move "Auto-Label" to AI & Chat and retitle it "Auto-Labeling (Tags & Collections)"?** It is
   not a transcription or speaker setting (PC2). *Default: yes.* The section id is unchanged and
   the panel is untouched.
3. **AI Summary Language (T10) stays in Transcription › Language as its own "Language for AI
   features" card, rather than moving to AI & Chat.** It shares the transcription settings
   endpoint and Save, and users look for "language" in one place. *Default: stay, in its own card
   with explicit copy.*
4. **Controls the pipeline currently ignores (T6/T7 garbage cleanup, S2/S3 attribute toggles,
   and T3/T4 on the native engine): wire them first, or ship the IA with them in place and fix in
   tickets?** *Default: ship the IA unchanged in behaviour, with honest help text only for the
   speaker range (§5), and file F1/F6/F11 as P1 v0.6.0 tickets.* No "this does nothing" banners,
   which would read as a broken product.
5. **Speaker Embedding System stays in System, with a link from Speaker Identification ›
   Maintenance, rather than moving.** It is deployment maintenance, and #861 groups by who and
   how often. *Default: stay and link.*

Risks:

- **Concurrent edits to `SettingsModal.svelte` and the locales** from other v0.6.0 lanes.
  Re-derive line numbers and land Steps 0 and 7 quickly.
- The e2e label change from "Transcription Settings" to "Transcription" relies on substring
  `has_text`. Verify that no other nav label contains "Transcription".
- The scoped reset adds a query parameter. Old clients are unaffected (no param = old
  behaviour).

### 7.2 Out-of-scope findings to file as separate issues

File each with `gh issue create` using a type label plus area labels (`backend`/`frontend`/`asr`/`gpu`).
Then add it to the org Roadmap project and set Status, Priority, Epic and Target (root
`CLAUDE.md`, "Conventions"). ✔ = re-verified by the planner; the rest come from the audit and need
a 2-minute re-check before filing.

| ID | Finding | Evidence |
|---|---|---|
| F1 ✔ | The native diarizer ignores min/max/num speakers. The range from Settings, upload, reprocess and watch source has no effect on the default engine. | `transcription/diarizer_native.py:17-21,946-951` |
| F2 | `ReprocessRequest.disable_diarization` is accepted and dropped. | `schemas/media.py:81`; `files/__init__.py:1231-1261`; `files/reprocess.py:395-403,576-584` |
| F3 | `PrepareUploadRequest.min/max/num_speakers` and `disable_diarization` are dropped (they only work via `/files/complete` or headers). | `schemas/media.py:164-178`; `prepare_upload.py:275-276`; `upload.py:316-325` |
| F4 | `BulkActionRequest.disable_diarization` is dropped. | `files/management.py:91,1036-1067,1127-1143` |
| F5 | No API sets per-file `source_language`, `translate_to_english` or `diarization_source`, although `dispatch_transcription_pipeline` accepts them. `services/CLAUDE.md` claims per-file overrides win. | `tasks/.../dispatch.py:273-277` |
| F6 ✔ | The user garbage-cleanup pref is dead. The pipeline uses the admin SS keys, which have no UI. Decide the owner of the value and wire it. | `user_settings.py:804-805`; `finalize.py:197,346`; `lib/api/adminSettings.ts:49-60` |
| F7 | `speaker_prompt_behavior='use_defaults'` falls back to the user's stored range, not the system default. | `FileUploader.svelte:290-307`; `pipelines.py:110-111` |
| F8 | The acoustic boundary re-check (E4-E6) is honoured only on the engine fast path, not the monolithic or gpu-split paths. | `pipelines.py:259,376`; `pipeline.py:26`; `engine/config.py:157-166` |
| F9 | `engine.transcriber_backend` is written but only logged. | `engine/backends/__init__.py:57-61`; `engine.py:59-61` |
| F10 | `engine.boundary_*` island/flank/gap/margin keys and `engine.gpu_split`/`precompute_vad`/`shared_volume_path` are read with no writer. `engine.gpu_split` from the DB is ignored for routing. | `boundary_resolver.py:97-126`; `engine/config.py:77-82`; `dispatch.py:146` |
| F11 ✔ | Speaker-attribute `gender_detection_enabled`, `age_detection_enabled` and `show_on_cards` have no reader (and no age model exists). `speaker_attribute.*` SS keys have a reader and no writer. | `user_settings.py:1211-1216,1318-1337`; `speaker_attribute_task.py:99-125` |
| F12 ✔ | Turning attribute detection off disables LLM speaker-name suggestions (the chain only starts from the attribute task). | `postprocess.py:725-729`; `background.py:183-186`; `rediarize_task.py:461-466`; `speaker_attribute_task.py:197-206,532-535` |
| F13 | Sidecar gender is written regardless of the user's toggle (gated by env `DIAR_NATIVE_GENDER`). | `diarizer_native.py:123`; `finalize.py:166,328`; `speaker_processor.py:385-409` |
| F14 | Cloud ASR plus `local` diarization ignores the user's speaker range (rediarize falls back to env). | `postprocess.py:232-238`; `rediarize_task.py:196-198` |
| F15 | The pyannote.ai diarization source has no credential writer (`UserDiarizationSettings` is read, never created by an endpoint). | `models/user_diarization_settings.py`; `services/diarization/factory.py:97-106` |
| F16 | A plain retry rewrites `diarization_source` to `provider`/`off` (it bypasses a locked source too) and drops `requested_whisper_model`. | `management.py:323-327`; `preprocess.py:266-270`; `upload.py:310` |
| F17 | Per-file `whisper_model` cannot switch the GPU model; only tiny/base reroute to CPU. The UI wording "High Quality (GPU) (model)" is accurate, but other values are silently ignored. | `pipelines.py:127-143,242-255,365-374`; `dispatch.py:317,352-356` |
| F18 | Defaults are defined in several places (min/max in `constants.py` and env; diarization source hardcoded `"provider"` in four places; engine defaults three times; VAD constants vs env). | `constants.py:621-622,782-796`; `config.py:1052-1053`; `engine_settings.py:43-51`; `engine/config.py:26-38`; `boundary_resolver.py:49-60` |
| F19 | Env `VAD_*`, `WHISPER_HALLUCINATION_THRESHOLD`, `WHISPER_REPETITION_PENALTY`, `SOURCE_LANGUAGE` and `ENABLE_DIARIZATION` are always shadowed by user prefs on the task path. | `pipelines.py:107-122` |
| F20 | `Settings.COMPUTE_TYPE` vs raw `COMPUTE_TYPE` and `WHISPER_COMPUTE_TYPE` readers. | `config.py:1038,1484`; `hardware_detection.py:618`; `transcription/config.py:154-155` |
| F21 | `create_for_user` ignores `UserASRSettings.is_active`. | `services/asr/factory.py:563-574` |
| F22 | The admin local-model routes sit under the `asr.user_providers`-gated router. Capability `asr.model_selection` is declared but unread. | `router.py:237-242`; `capabilities.py:58-60` |
| F25 ✔ | "Speaker Embedding System" is admin-tier in `SECTION_MIN_ROLE`, but all migration routes are super_admin (consistency routes are admin). | `SettingsModal.svelte` `SECTION_MIN_ROLE`; `embedding_migration.py` (9× superuser); `admin.py:2751-2773` |
| F28 | The upload Speakers step is shown even when diarization is off, the CPU "Fast" model is chosen, or cloud ASR is active (reprocess hides it for cloud). | `upload/UploadStepSpeakers.svelte`; `SelectiveReprocessModal.svelte:56-57` |
| F29 | The reprocess dialog does not load the user's saved range or `speaker_prompt_behavior`. | `SelectiveReprocessModal.svelte:268-297` |
| F30 | URL import, recording and navbar quick-record drop speaker counts and the model; the legacy `POST /files` fallback drops `whisper_model`. | `FileUploader.svelte:703-723,756`; `stores/uploads.ts:152,156`; `Navbar.svelte:193`; `lib/services/uploadService.ts:866-883` |
| F31 | The watch-source modal hardcodes min/max defaults 1/20 instead of loading the user's or system values. | `WatchSourceModal.svelte:100-101,187-188` |
| F32 | Stale docs paths unrelated to this IA ("Settings → Search", "Settings → Embeddings", "Transcription & AI", Organization Context location). | see the §6 Step 6 docs list "leave" items |
| F33 | `ChatSettingsPanel` hand-rolls tabs with no keyboard support or tabpanel, and Watch Sources has no deep-link tab (`initialTab`). Align both to the shared pattern. | `ChatSettingsPanel.svelte:33-58`; `WatchSourcesSettings.svelte:56` |

Fixed **inside** this plan (no ticket needed): D5, D11, D12, D13, D14, D15, D16 (orphans), D17,
D18, D19.

---

## Addendum: pyannote.ai credential (#1204)

Fixes F15 / D16 without removing the `pyannote` diarization source. Written 2026-10-10 before
the code; the implementation follows it.

### A1. Who owns the key: a per-user key, stored like a cloud ASR key

- **Per-user, never shared, never a deployment key.** Cloud ASR keys are per-user
  (`user_asr_settings`, `api/endpoints/asr_settings.py`), and pyannote.ai only runs alongside a
  per-user cloud ASR provider (`cloud_asr.py` `_run_cloud_asr_pipeline`). The vendor bills the
  key's owner, so the key is not shareable (unlike ASR configs, which have `is_shared`). There is
  no env fallback and no admin route that reads or writes another user's key.
- **Storage: the existing `user_diarization_settings` table** (`v355`), one row per user with
  `provider='pyannote'`, `name='pyannote.ai'`, `model_name=PYANNOTE_DEFAULT_DIARIZATION_MODEL`.
  No Alembic revision. `api_key` holds `encrypt_api_key()` output (AES-256-GCM, `v3:` prefix), the
  same helper the ASR endpoints use; the factory switches from `decrypt_value` to the matching
  `decrypt_api_key`.
- **Never on the wire, never logged, never audited in clear.** Every response is a status
  object (`configured`, `test_status`, `last_tested`, `updated_at`, `locked`). There is no
  "reveal key" route (ASR has one, `GET /config/{uuid}/api-key`; this credential deliberately
  does not). Logs name the user id and the action only. Two new audit events,
  `user.credential.set` and `user.credential.delete`, carry
  `details={"provider": "pyannote.ai", "purpose": "diarization"}` and nothing else.
- **Gating.** The router is mounted under the `asr.user_providers` capability (the per-user
  cloud-provider-keys capability; off = 404, same as `/api/asr-settings`). When the deployment
  locks `transcription.diarization_source` (#1109), a user cannot choose the source, so the
  write routes refuse with **409** and `GET` reports `locked: true`; no platform-admin bypass,
  matching #1109.

### A2. API (`/api/user-settings/diarization/pyannote`, new module `api/endpoints/diarization_settings.py`)

| Method | Body | Result |
|---|---|---|
| `GET` | none | `PyannoteCredentialStatus` |
| `PUT` | `{"api_key": "<key>"}` (8 to 512 printable characters, no whitespace) | Upserts the encrypted key and clears the stale test result. `PyannoteCredentialStatus` |
| `DELETE` | none | Deletes the row. If the user's `transcription_diarization_source` was `pyannote`, it is reset to the default (`provider`). `{"deleted", "diarization_source", "source_reverted"}` |
| `POST /test` | optional `{"api_key": "<key>"}`; without one, the saved key | `{"success", "message", "response_time_ms"}`; the result is stored on the row only when the saved key was tested. Rate-limited like `POST /api/asr-settings/test`. |

Schemas live in `app/schemas/diarization_settings.py`. OpenAPI is regenerated with
`scripts/generate-openapi.py --write`.

### A3. Validation must not bill

`POST /test` calls the vendor's `GET /v1/test` (`PyAnnoteCloudDiarizationProvider.validate_connection`),
an authenticated no-op that starts no job. It never calls `/v1/media/input` or `/v1/diarize`. The
message is a fixed sentence per outcome (connected / key rejected / service unreachable); the vendor
response body and exception text are logged after sanitization, never returned.

### A4. No silent fallback

- **Selection time.** `PUT /api/user-settings/transcription` with `diarization_source='pyannote'`
  answers **409** with *"pyannote.ai speaker detection needs a pyannote.ai API key. Save your key
  first, then choose this option."* when the user has no stored key and the stored source is not
  already `pyannote`. A form that re-sends an unchanged legacy value is not rejected (the file-time
  check below catches it), so unrelated settings stay saveable.
- **Processing time.** `DiarizationProviderFactory.create_for_user` raises
  `DiarizationNotConfiguredError` when the source is `pyannote` and no usable key exists (no row,
  empty column, or a value that no longer decrypts). `_run_cloud_asr_pipeline` resolves the
  provider **before** the cloud ASR call, so the file fails without spending the user's ASR credit.
  The exception's text is a fixed sentence registered in `ErrorCategorizationService` as the new
  user-facing reason `diarization_not_configured`. The file goes to ERROR with that sentence and
  suggestions, through the existing #959 path, and is shown in the file list, file detail and
  status modal with the existing Retry button. Its retry category is the new permanent
  `ErrorCategory.CONFIGURATION_REQUIRED`, so automatic retries do not loop on it. The manual
  Retry works once a key is saved or another source is chosen.
- Before this change the same state produced a transcript with **no speakers at all**: the
  fallback called the ASR provider with `enable_diarization=False`. Failing visibly is the owner's
  "no silent fallback" rule. Vendor-side failures of a configured key (`diarize_error` in
  `_run_parallel_cloud_asr_and_diarization`) are unchanged and out of scope.

### A5. Data flow

`PUT` encrypts and stores the key. At file time, `_run_cloud_asr_pipeline` calls
`create_for_user(user_id, db)`. That reads `transcription_diarization_source` (the #1109 lock
still forces `provider`), loads the user's active `pyannote` row, decrypts the key and builds
`PyAnnoteCloudDiarizationProvider(api_key=..., model_name=current_pyannote_model(row.model_name))`,
which runs in parallel with the cloud ASR call as before.

### A6. Frontend (no settings components touched here)

The typed client is `frontend/src/lib/api/pyannoteCredential.ts` and the pure view-state helper is
`frontend/src/lib/settings/pyannoteCredential.ts`, both with vitest. The `settings.speakerIdentification.pyannoteKey.*`
i18n keys are added to all 12 locales, and `errors.media.diarizationNotConfigured` is mapped in
`lib/i18n/mediaErrors.ts`. The form itself is wired into the Speaker Identification tab by the
settings restructuring work, using this contract:

- Component `PyannoteCredentialForm.svelte` goes in the Speaker Identification tab, shown under the
  speaker-detection select when the `asr.user_providers` capability is on. Props:
  `diarizationSource: string` and `disabled?: boolean`. Events: `saved`, `deleted` (detail
  `{ sourceReverted: boolean }`, so the parent reloads the source select) and `tested` (detail
  `PyannoteTestResult`).
- It uses `getPyannoteCredential`, `savePyannoteCredential`, `deletePyannoteCredential` and
  `testPyannoteCredential` from the client, and `pyannoteCredentialView(status, diarizationSource)`
  for the badge, the button states and the warning.
- The key input is `type="password"` with `autocomplete="off"`. It is never pre-filled, because
  the API never returns the key.

### A7. Tests (each seen failing on the old code first)

`tests/api/test_pyannote_credential_endpoints.py` covers:
- store, then the DB column is not the plaintext and decrypts to it;
- the key appears in no response body, log record or audit event;
- 409 on selecting `pyannote` with no key, and success once a key is stored;
- DELETE reverts the source;
- users are isolated from each other;
- 404 when the capability is off, 409 when the source is locked;
- test-connection against a local fake HTTP server, asserting only `/v1/test` was hit.

`tests/unit/test_pyannote_diarization_credential_flow.py` covers:
- the factory returns a configured provider when a key exists;
- it raises the fixed error when the key is missing;
- the cloud pipeline fails the file with reason `diarization_not_configured` before calling ASR.
