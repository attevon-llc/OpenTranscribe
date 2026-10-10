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

## Key files

- `factory.py` — `DiarizationProviderFactory`. Sources are `provider | local | pyannote | off`
  (`VALID_DIARIZATION_SOURCES`, duplicated in `core/constants.py` under the same name), default
  `provider`. Returns **`None`** for `provider` (use the ASR provider's own labels) and `off`.
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
  without a key (409); deleting the key resets a `pyannote` source to the default. The
  connection test hits only the vendor's free `GET /v1/test`.
- `local_provider.py` — delegates to `ModelManager.get_diarizer()`, which is **native-first**:
  the diar-native sidecar with automatic in-process PyAnnote failover, not PyAnnote directly. It
  stuffs native objects (`native_embeddings`, `overlap_info`, `diarize_df`) into
  `DiarizeResult.metadata`, so `DiarizeResult` is **not** a pure DTO. Note its hardcoded
  `model_name="pyannote/speaker-diarization-community-1"` reports the *weights*, which both
  engines share — it is not a claim about which engine ran.
- `pyannote_provider.py` — pyannote.ai cloud diarization (`precision-3`; `precision-2` is retired and mapped to it). Distinct from
  `../asr/pyannote_provider.py`, which hits the *STT-orchestration* endpoint. Don't confuse them.

## How it connects

- The user setting is `UserSetting["transcription_diarization_source"]`, written and validated in
  `api/endpoints/user_settings.py`, read in `factory.create_for_user` and in
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
