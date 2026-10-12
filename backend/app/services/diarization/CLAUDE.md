# backend/app/services/diarization — pluggable diarization providers

## Purpose

Speaker segmentation decoupled from ASR. Same skeleton as `../asr/` (`base.py` + `types.py` +
`factory.py` + one module per vendor) — read **`../asr/CLAUDE.md`** for the shared pattern and the
"register in the factory, never branch at a call site" rule. This file covers only what differs.

⚠️ **This package is one of two independent axes and they are constantly confused.** *This* one
picks **where** diarization runs, per user (`transcription_diarization_source`). The other,
`TranscriptionConfig.diarizer_backend` (`native` | `pyannote`), picks **which local engine**
runs once the answer here is "on our own GPU" — see `app/transcription/CLAUDE.md`. Changing one
never changes the other.

## What a source MEANS is decided in one place: `services/pipeline_plan.py` (#1224)

`resolve_pipeline_plan` is the single decision point for "given this transcription engine
(local GPU / local CPU / cloud) and this stored source, which detector runs, where, on which
queue, and how its turns reach the text". Sources are `provider | local | pyannote |
asr_config | off` (`core/constants.VALID_DIARIZATION_SOURCES`, the only copy; the factory's
duplicate is gone). `asr_config` names another saved ASR configuration's speaker detection via
the `transcription_diarization_asr_config_uuid` user setting (locked with the source, #1109).
`load_plan_inputs` (`services/pipeline_plan_inputs.py`) gathers the inputs; the settings PUT and
`GET /user-settings/pipeline` already use it. **Never add a new `source == "..."` check at a
call site** — extend the resolver and its matrix test (`tests/unit/test_pipeline_plan.py`).

**The stages run what the plan says (#1225).** Dispatch resolves the plan once and carries
it in the stage context (`context["pipeline_plan"]`, read with `tasks/transcription/context.plan_of`);
no stage re-reads the stored source. What each execution means at run time:

| Execution | Who detects speakers, where |
|---|---|
| `inline_local` | this server's engine inside the GPU stage (`enable_diarization=True`) |
| `inline_asr` | the cloud transcription model returns them |
| `parallel_cloud` | pyannote.ai or another provider's speakers (`asr_config`) beside cloud transcription (`cloud_asr._run_parallel_cloud_asr_and_diarization`) |
| `post_stage_local` / `post_stage_cloud` | `rediarize_task` after the transcript is saved: local Whisper + pyannote.ai or `asr_config`, the tiny/base CPU path, and Automatic + a text-only timed cloud model |
| `none` | nobody (`off`) |

The post stage runs on the plan's queue (`cloud-asr` for a vendor, `gpu_preferred_queue()`
for this server's engine), merges per word or per segment (`merge`), then routes through
`finalize_segments` like every other path. A vendor failure there is NON-fatal: the
transcript stands without speakers and the post stage's task row records
`rediarize_task.CLOUD_DIARIZATION_FAILED_MESSAGE` (a fixed sentence, never vendor text).

**One merge (#1226).** Both cloud paths (the parallel path and the post stage) hand their
turns to `transcription/speaker_assigner.assign_speakers(frame, transcript, granularity=)`
with `diarize_segments_to_frame`; `granularity="segment"` gives each segment its dominant
speaker and its words follow. `utils/diarization_merge.merge_cloud_diarization` (word
midpoint, never labelled words) is deleted, gated by
`tests/unit/test_merge_unification_equivalence.py` (99.69% / 99.91% word agreement on the
real boundary fixtures; every disagreement a two-speaker word the overlap rule gets right
more often).

## Key files

- `asr_adapter.py` — **`ASRDiarizationAdapter` (#1226): another vendor's speech-to-text
  diarization, used only for its speakers.** pyannote.ai is the only hosted diarization-ONLY
  API in reach; every other vendor detects speakers only while transcribing, so this runs a
  saved ASR configuration with `enable_diarization=True` (the user's min/max/num forwarded;
  which ones the module actually sends is the catalog's `speaker_count_hints`) and keeps the
  turns. `speaker_turns` reads times and labels off the provider module's OWN parsed result
  (labels already through the shared `normalize_speaker_label`; never re-parse vendor JSON
  here): each timed word is a piece of its segment's speaker, same-speaker pieces closer than
  `COALESCE_GAP_S` (0.5 s) join, a segment with no timed words is one turn. **The text is
  discarded** (no field for it on `DiarizeResult`, logs carry counts only) and **billed**: the
  vendor charges a full transcription (the plan's `diarizer_double_billing` warning). A local
  or text-only provider is refused at construction. Every catalog model that returns speakers
  is driven through its real module in `tests/unit/test_asr_diarization_adapter.py`.
- `errors.py` — fixed-sentence errors, the ONE definition of each: `DiarizerConfigUnusableError`
  (reason `pipeline_not_runnable`, permanent `CONFIGURATION_REQUIRED`; `factory` re-exports
  it), `DiarizationProviderFailedError` and its retryable `DiarizationRateLimitedError` (an
  `ASRRateLimitedError`). `str()` is never the vendor's text; the adapter logs that, sanitised.
- `factory.py` — `DiarizationProviderFactory.create_for_engine(engine, user_id, db,
  config_uuid=...)` turns the plan's engine (`pipeline_plan.post_stage_engine`: `local`,
  `pyannote_ai`, `asr_config`) into a provider; it never re-derives anything from a stored
  source (`create_for_source` / `create_for_user` are gone). `local` builds
  `LocalDiarizationProvider`; `pyannote_ai` raises **`DiarizationNotConfiguredError`** with no
  usable key (none stored, or one that no longer decrypts); **`asr_config`** builds an
  `ASRDiarizationAdapter` from the saved configuration through `get_diarizer_asr_config` (own
  or tenant-shared, `asr.user_providers` capability; the plan's `load_plan_inputs` uses the
  same lookup) and `ASRProviderFactory.create_from_db_config` (the one decryption path).
  Gone, unshared, local, text-only, untimed or undecryptable raises
  `DiarizerConfigUnusableError`, as does an engine no plan produces. The plan already folds
  "same provider and model as the transcription" into that provider's own speakers, so no
  second, billed call is ever built for it. Building makes no network call. The pyannote
  exception's text is
  the fixed sentence registered in `ErrorCategorizationService` as reason
  `diarization_not_configured` (permanent retry category `CONFIGURATION_REQUIRED`), so the file
  fails visibly instead of the pre-#1204 silent fallback that transcribed with diarization OFF.
- **The credential (issue #1204).** One `UserDiarizationSettings` row per user
  (`provider='pyannote'`, `name='pyannote.ai'`), written ONLY by
  `api/endpoints/diarization_settings.py` (`GET/PUT/DELETE /user-settings/diarization/pyannote`,
  `POST .../test`), gated by the `asr.user_providers` capability and refused (409) while the
  #1109 `transcription.diarization_source` lock is on. Encrypted with `encrypt_api_key`, read
  with `decrypt_api_key` (`get_pyannote_credential` / `has_pyannote_credential` here), and
  returned by no route. `PUT /user-settings/transcription` refuses switching TO `pyannote`
  without a key (409, `detail.code == "pyannote_key_missing"`, decided by the plan); deleting
  the key resets a `pyannote` source to the default. The
  connection test hits only the vendor's free `GET /v1/test`.
- `local_provider.py` — delegates to `ModelManager.get_diarizer()`, which is **native-first**:
  the diar-native sidecar with automatic in-process PyAnnote failover, not PyAnnote directly. It
  runs through `engine.stages._run_diarize` (honouring `DiarizeConfig.sidecar_wav_path`) and
  stuffs native objects (`native_embeddings`, `overlap_info`, `diarize_df`) into
  `DiarizeResult.metadata`, so `DiarizeResult` is **not** a pure DTO. `provider_name` /
  `model_name` come from the diarizer's `last_provider` / `last_model`: the engine that
  actually ran, not a hardcoded name. It is the local post stage's detector.
- `pyannote_provider.py` — pyannote.ai cloud diarization (`precision-3`; `precision-2` is retired and mapped to it). Distinct from
  `../asr/pyannote_provider.py`, which hits the *STT-orchestration* endpoint. Don't confuse them.
- **Provider ids say which engine RAN (#1229).** pyannote.ai reports `provider_name ==
  "pyannote.ai"` (`pipeline_plan.PYANNOTE_AI`); it used to say `pyannote`, the local in-process
  engine's id, so a cloud run was stored and displayed as the local engine. `local_provider`
  reports the served engine (`diarizer.last_provider`: `native` | `pyannote`), not `local`.
  These ids land in `media_file.diarization_provider` and in the per-file provenance
  (`services/file_provenance.py`), which maps each to where it ran.

## How it connects

- The user setting is `UserSetting["transcription_diarization_source"]`, written in
  `api/endpoints/user_settings.py` (validated by the pipeline plan) and read ONLY by
  `services/pipeline_plan_inputs.py`. Frontend: `components/settings/TranscriptionSettings.svelte`.
- Two production call sites of `create_for_engine`, both inside a short read-only session (no
  HTTP inside it): `tasks/transcription/cloud_asr.py`'s parallel path (`parallel_cloud`) and
  `tasks/rediarize_task._build_detector` (the post stage, and a user's manual re-run, which
  plans with the file owner's CURRENT choice: `files/reprocess._dispatch_manual_rediarize`).
- `v434_reset_unusable_pyannote_source` rewrote `pyannote` to `provider` for users with no key
  and local transcription (the combination that used to be silently ignored and would now
  fail every file). A user WITH a key and local ASR now runs, and is billed for, pyannote.ai.
- Migration from the old boolean flag: `alembic/versions/v355_add_diarization_settings.py`.

## Gotchas

- **Label normalization and error sanitization are shared, not duplicated.** `base.py` delegates
  `_normalize_speaker_label` / `_sanitize_error` to the module-level `normalize_speaker_label` /
  `sanitize_provider_error` in `asr/base.py`. Change behavior *there* — both hierarchies use it, and
  `tests/unit/test_speaker_label_normalization.py` asserts they agree. (Until #299 this proxied through
  `object.__new__(ASRProvider)`, which trips the ABC check and raised `TypeError` on **every** call,
  killing the label-normalization call in `pyannote_provider._parse_segments` and its twin in
  `local_provider.diarize`.)
- **Vendor-side failures of a configured key are still non-fatal**:
  `_run_parallel_cloud_asr_and_diarization` returns the speakerless ASR result when the
  pyannote.ai job itself fails. Only a *missing* key fails the file (#1204).
- `test_provider_sdk_compat.py` covers **no** module in this package. `asr_adapter.py`
  imports no SDK (the wrapped ASR module does, and that module is on the ASR list).
- **Deleting a saved ASR config resets every `asr_config` choice naming it** (plan E5):
  `DELETE /asr-settings/config/{uuid}` and `/all` remove the pointer for each holder (owner and
  share recipients) and revert a still-`asr_config` source to the default; the response's
  `diarizer_source_reverted` says whether the caller's was. Unsharing does not reset: the plan
  reports `diarizer_config_missing` instead.
