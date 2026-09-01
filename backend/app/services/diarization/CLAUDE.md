# backend/app/services/diarization — pluggable diarization providers

> ⚠️ **Several line references below are stale — verify before following one.** Measured
> 2026-09-01: `VALID_DIARIZATION_SOURCES` is at `core/constants.py:648`, not `:508`; the factory's
> only production caller is `tasks/transcription/cloud_asr.py:83-89` guarded at `:265`, not
> `tasks/transcription/core.py:1804` (that file is 353 lines). `local_provider.py` is described as
> delegating to PyAnnote — `ModelManager` is **native-first with PyAnnote as fallback**. Correction
> tracked in **#671**; the wider diarization roadmap in **#572** and
> [this gist](https://gist.github.com/attevon-admin/a99819c7ec5e8ab8df0eb8e3e8e668e8).

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

- **Label normalization and error sanitization are shared, not duplicated.** `base.py` delegates
  `_normalize_speaker_label` / `_sanitize_error` to the module-level `normalize_speaker_label` /
  `sanitize_provider_error` in `asr/base.py`. Change behavior *there* — both hierarchies use it, and
  `tests/unit/test_speaker_label_normalization.py` asserts they agree. (Until #299 this proxied through
  `object.__new__(ASRProvider)`, which trips the ABC check and raised `TypeError` on **every** call,
  killing `pyannote_provider.py:476` and `local_provider.py:91`.)
- `factory.py:95` still says "UserDiarizationSettings model will be created in a separate task". The
  model exists (`models/user_diarization_settings.py`); the comment is stale.
- `test_provider_sdk_compat.py` covers **no** module in this package.
