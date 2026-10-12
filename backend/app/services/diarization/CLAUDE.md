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
behaves as below: `pyannote` or `asr_config` + local Whisper runs the LOCAL diarizer, a per-file
lightweight model forces diarization off, and Automatic + a text-only cloud model yields a
speakerless transcript. Cloud ASR + `asr_config` already runs (#1226): the parallel cloud path
builds the adapter below. The plan already describes what WP3 makes run.

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
- `errors.py` — fixed-sentence errors: `DiarizerConfigUnusableError` (registered in
  `ErrorCategorizationService` as reason `diarizer_config_unusable`, permanent
  `CONFIGURATION_REQUIRED`), `DiarizationProviderFailedError` and its retryable
  `DiarizationRateLimitedError` (an `ASRRateLimitedError`). `str()` is never the vendor's text;
  the adapter logs that, sanitised.
- `factory.py` — `DiarizationProviderFactory`. Returns **`None`** for `provider` (use the ASR
  provider's own labels) and `off`. **`asr_config`** builds an `ASRDiarizationAdapter` from the
  saved configuration via `ASRProviderFactory.create_from_db_config` (the one decryption path):
  `create_for_source("asr_config", asr_config=row)`, or `create_for_user`, which reads the
  `transcription_diarization_asr_config_uuid` pointer through `get_diarizer_asr_config` (own
  or tenant-shared, `asr.user_providers` capability; the plan's `load_plan_inputs` uses the
  same lookup) and returns `None` when that config has the user's transcription provider and
  model (its own speakers; no second, billed call). Gone, unshared, local, text-only, untimed
  or undecryptable raises `DiarizerConfigUnusableError`. Building makes no network call.
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
  `DiarizeResult.metadata`, so `DiarizeResult` is **not** a pure DTO. Note its hardcoded
  `model_name="pyannote/speaker-diarization-community-1"` reports the *weights*, which both
  engines share — it is not a claim about which engine ran.
- `pyannote_provider.py` — pyannote.ai cloud diarization (`precision-3`; `precision-2` is retired and mapped to it). Distinct from
  `../asr/pyannote_provider.py`, which hits the *STT-orchestration* endpoint. Don't confuse them.

## How it connects

- The user setting is `UserSetting["transcription_diarization_source"]`, written in
  `api/endpoints/user_settings.py` (validated by the pipeline plan), read in
  `services/pipeline_plan_inputs.py`, `factory.create_for_user` and in
  `tasks/transcription/user_settings.py` (which also derives `disable_diarization` from
  `source == "off"`). Frontend: `components/settings/TranscriptionSettings.svelte`.
- **Only `pyannote` and `asr_config` actually reach this factory.** The single production
  call site is `tasks/transcription/cloud_asr.py`'s `_run_parallel_cloud_asr_and_diarization`,
  guarded by `diarization_source in ("pyannote", "asr_config")` in `_run_cloud_asr_pipeline`;
  it runs cloud ASR and cloud diarization in a 2-thread pool and returns the turns, which
  `_run_cloud_asr_pipeline` merges with **`transcription/speaker_assigner.assign_speakers`**
  at the transcription model's `timestamp_granularity` (word, else segment). That is the ONE
  merge: `utils/diarization_merge.merge_cloud_diarization` (word midpoint, never labelled
  words) was deleted in #1226, gated by `tests/unit/test_merge_unification_equivalence.py`
  (99.69% / 99.91% word agreement on the real boundary fixtures; every disagreement a
  two-speaker word the overlap rule gets right more often). WP3 replaces this call site with
  the plan-driven post stage.
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
- `test_provider_sdk_compat.py` covers **no** module in this package. `asr_adapter.py`
  imports no SDK (the wrapped ASR module does, and that module is on the ASR list).
- **Deleting a saved ASR config resets every `asr_config` choice naming it** (plan E5):
  `DELETE /asr-settings/config/{uuid}` and `/all` remove the pointer for each holder (owner and
  share recipients) and revert a still-`asr_config` source to the default; the response's
  `diarizer_source_reverted` says whether the caller's was. Unsharing does not reset: the plan
  reports `diarizer_config_missing` instead.
