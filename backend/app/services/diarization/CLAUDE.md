# backend/app/services/diarization — pluggable diarization providers

## Purpose

Speaker segmentation decoupled from ASR. Same skeleton as `../asr/` (`base.py` + `types.py` +
`factory.py` + one module per vendor) — read **`../asr/CLAUDE.md`** for the shared pattern and the
"register in the factory, never branch at a call site" rule. This file covers only what differs.

## Key files

- `factory.py` — `DiarizationProviderFactory`. Sources are `provider | local | pyannote | off`
  (`VALID_DIARIZATION_SOURCES`, duplicated at `core/constants.py:508`), default `provider`. Returns
  **`None`** for `provider` (use the ASR provider's own labels) and `off`, and **raises `ValueError`**
  for an unknown source or a missing pyannote key — unlike the ASR factory, which silently degrades to
  local. Credentials come from `UserDiarizationSettings` and are decrypted with `decrypt_value`
  (ASR uses `decrypt_api_key` for the same job).
- `local_provider.py` — delegates to `ModelManager.get_diarizer()` → `app/transcription/diarizer.py`;
  no duplicated pipeline. It stuffs native objects (`native_embeddings`, `overlap_info`, `diarize_df`)
  into `DiarizeResult.metadata`, so `DiarizeResult` is **not** a pure DTO.
- `pyannote_provider.py` — pyannote.ai cloud diarization (`precision-2`). Distinct from
  `../asr/pyannote_provider.py`, which hits the *STT-orchestration* endpoint. Don't confuse them.

## How it connects

- The user setting is `UserSetting["transcription_diarization_source"]`, written and validated in
  `api/endpoints/user_settings.py:778`, read at `factory.py:79` and `tasks/transcription/core.py:179`.
  Frontend: `components/settings/TranscriptionSettings.svelte`.
- **Only `source == "pyannote"` actually reaches this factory** (`tasks/transcription/core.py:1804`,
  guarded at `:1971`), where it runs in parallel with cloud ASR in a 2-thread pool and merges via
  `utils/diarization_merge`. `source == "local"` is served instead by `rediarize_task` on the GPU queue
  (`tasks/transcription/postprocess.py:92-128`), so the factory's `local` branch is effectively dead.
- Migration from the old boolean flag: `alembic/versions/v355_add_diarization_settings.py:44-79`.

## Gotchas

- **`base.py:57-59` is broken at runtime.** `_normalize_speaker_label` proxies through
  `object.__new__(ASRProvider)`, but `object.__new__` performs the ABC check — so **every** call raises
  `TypeError: Can't instantiate abstract class ASRProvider` (verified). That kills
  `pyannote_provider.py:476` (the cloud-diarization happy path) and `local_provider.py:91`. Nothing
  tests it — `tests/unit/test_pyannote_provider.py` covers the *ASR* provider. Fix by making the ASR
  helper a module-level function or `@staticmethod`.
- `base.py:61-73` copy-pastes `_sanitize_error` from `asr/base.py` instead of reusing it — keep both in
  sync if you touch either.
- `factory.py:95` still says "UserDiarizationSettings model will be created in a separate task". The
  model exists (`models/user_diarization_settings.py`); the comment is stale.
- `test_provider_sdk_compat.py` covers **no** module in this package.
