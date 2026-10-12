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

⚠️ **The pipeline stages do not read the plan yet (WP3).** Until they do, the runtime still
behaves as below: `pyannote` + local Whisper runs the LOCAL diarizer, `asr_config` produces no
speakers, a per-file lightweight model forces diarization off, and Automatic + a text-only
cloud model yields a speakerless transcript. The plan already describes what WP3 makes run.

## Key files

- `factory.py` — `DiarizationProviderFactory`. Returns **`None`** for `provider` (use the ASR
  provider's own labels) and `off`.
  `create_for_source` raises `ValueError` for an unknown source or a missing key;
  `create_for_user` raises **`DiarizationNotConfiguredError`** when the user chose `pyannote`
  with no usable key (none stored, or one that no longer decrypts). That exception's text is
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
  stuffs native objects (`native_embeddings`, `overlap_info`, `diarize_df`) into
  `DiarizeResult.metadata`, so `DiarizeResult` is **not** a pure DTO. Its `provider_name` /
  `model_name` are the served engine's `last_provider` / `last_model` (#1229);
  `pyannote/speaker-diarization-community-1` is only the fallback when the engine reports no
  model (the *weights* both engines share).
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
  `api/endpoints/user_settings.py` (validated by the pipeline plan), read in
  `services/pipeline_plan_inputs.py`, `factory.create_for_user` and in
  `tasks/transcription/user_settings.py` (which also derives `disable_diarization` from
  `source == "off"`). Frontend: `components/settings/TranscriptionSettings.svelte`.
- **Only `source == "pyannote"` actually reaches this factory.** The single production call site
  is `tasks/transcription/cloud_asr.py`'s `_run_parallel_cloud_asr_and_diarization`, guarded by
  `if diarization_source == "pyannote":` in `_run_cloud_asr_pipeline`; it runs cloud ASR
  and cloud diarization in a 2-thread pool and merges via `utils/diarization_merge`.
  `source == "local"` is served instead by `rediarize_task` on the GPU queue (dispatched from
  `tasks/transcription/postprocess.py`), so the factory's `local` branch is effectively dead.
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
- `test_provider_sdk_compat.py` covers **no** module in this package.
