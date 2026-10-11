# Pipeline composition: independently selectable transcription and speaker detection

Status: PLAN ONLY (2026-10-11). Nothing here is implemented. Absorbs issue #1219.
**Amended 2026-10-11 (owner corrections):** lite is fully cloud-only for transcription AND
speaker detection (WP6 rewritten, #1228); timestamp granularity is a first-class capability
(A10, C7); a separate local voiceprint stage runs after every diarization source (C6); the
open questions are now decided items (H); per-file provenance is WP7 (A11, new); the docs-site
architecture diagrams are WP8 (new, last). All eight work packages ship in v0.6.0.
Branch this was planned against: `feat/v0.6.0-frontend-ux` @ `8ec69ebc`
(worktree `.claude/worktrees/v0.6.0`). Line numbers are from that commit; re-check them with
`rg -n` before editing, the tree moves fast.

Related plan (read its premise corrections PC4-PC6 first, they still hold):
`docs/design/v060_transcription_speaker_settings_plan.md`.

## 0. The request (owner intent)

Transcription (ASR) and speaker detection (diarization) must be **independently selectable**,
each **local or cloud**, each **on or off**, mix-and-match:

| Wanted combination | Works today? |
|---|---|
| Local CPU transcription + cloud speaker detection | **No** (CPU path forces diarization off). Full image only: the per-file lightweight model. Lite stays cloud-only by owner decision (WP6). |
| Local GPU transcription + pyannote.ai speaker detection | **No, silently**: pyannote.ai is ignored and the local diarizer runs |
| Cloud ASR + local speaker detection | Yes (`source=local`) |
| One cloud ASR that does both | Yes (`source=provider`) |
| Cloud ASR text + a DIFFERENT cloud vendor's speakers | Only for pyannote.ai; any other vendor: **no** |

Plus: Settings must show **where** each model/provider runs and **what it is used for**
(issue #1219: per-provider "speakers included / not included" badge; warn when Automatic speaker
detection is paired with a text-only provider).

---

## A. Current-state map (verified in code)

### A1. The two existing axes

| Axis | Who sets it | Storage | Values | Reader |
|---|---|---|---|---|
| **Where** speakers are detected | each user | `UserSetting["transcription_diarization_source"]` | `provider` \| `local` \| `pyannote` \| `off`, default `provider` (`backend/app/core/constants.py:812-813`, duplicated at `backend/app/services/diarization/factory.py:19-20`) | see A3 |
| **Which local engine** runs when "local" | super_admin | `SystemSettings["engine.diarizer_backend"]` -> env `ENGINE_DIARIZER_BACKEND` -> `native` (`backend/app/transcription/config.py:358-399`, `backend/app/api/endpoints/engine_settings.py:36,46,202-214`) | `native` (diar-native sidecar, in-process PyAnnote failover) \| `pyannote` | `backend/app/transcription/model_manager.py:99-180` |
| **Which transcription engine** | each user | `UserSetting["active_asr_config_id"]` -> `UserASRSettings` row (own or shared) -> env `ASR_PROVIDER` -> `local` (`backend/app/services/asr/factory.py:540-614`) | `local` or one of 9 vendors | `dispatch.py:_resolve_gpu_queue`, `core.py:_resolve_asr_provider_or_none` |
| Local Whisper model | super_admin, deployment-wide | `SystemSettings["asr.local_model"]` | any faster-whisper model | `transcription/config.py` |
| Per-file model | per upload/reprocess | `media_file.requested_whisper_model` | admin model, or `tiny`/`tiny.en`/`base`/`base.en` (`backend/app/transcription/config.py:17`) | `dispatch.py:349` routes lightweight models to CPU |

The pyannote.ai credential is its own table, one row per user: `user_diarization_settings`
(`provider='pyannote'`), read by `get_pyannote_credential` / `has_pyannote_credential`
(`services/diarization/factory.py:43-61`), written only by
`api/endpoints/diarization_settings.py` (#1204).

### A2. Validation and locks on the user setting

- `PUT /api/user-settings/transcription` maps `diarization_source` ->
  `transcription_diarization_source` (`api/endpoints/user_settings.py:854`), rejects unknown
  values with 422 (`:857-868`), and 409s a NEW selection of `pyannote` without a stored key
  (`_require_pyannote_key_for_new_selection`, `:617-636`, detail constant `:611`).
- It does **not** look at the user's ASR provider. Nothing stops `pyannote` + local ASR.
- Lock #1109: capability `transcription.diarization_source` (`core/locked_settings.py:25,31`,
  `core/capabilities.py:122,177`). When off, every reader drops the stored value and uses the
  constant default `provider` (`tasks/transcription/user_settings.py:83-89`,
  `services/diarization/factory.py:141-145`). There is no admin-configurable deployment default;
  the lock always means `provider`.

### A3. How the choice is resolved and dispatched (end to end)

1. **Dispatch** `tasks/transcription/dispatch.py:268-435`.
   - `resolve_dispatch_options` (`requested_options.py:51-114`) merges per-file options; the only
     per-file diarization option stored is `requested_disable_diarization` (bool,
     `models/media.py:296`); there is **no per-file source**. Re-runs replay "off" (`:99-107`).
   - Queue: lightweight model -> `cpu-transcribe` (`dispatch.py:349,375-379`); otherwise
     `_resolve_gpu_queue` (`:104-156`): any non-`local` ASR provider -> `cloud-asr`, else
     `gpu-transcribe` (split topology, only with a live consumer) or `gpu`.
   - CPU route forces `diarization_source="off"` (`dispatch.py:397-398`).
2. **Preprocess** (CPU) `tasks/transcription/preprocess.py:265-278`: explicit source > legacy
   bool (`disable_diarization=False` is mapped to **`provider`**, `:269`, which would override a
   `local`/`pyannote` choice; every current caller avoids passing `False`, e.g.
   `api/endpoints/files/reprocess.py:183-185`, `management.py:779-780`, but the trap remains) >
   user setting.
3. **GPU/cloud task** `tasks/transcription/core.py:395-647` (`transcription.gpu_transcribe`, runs
   on `gpu` OR `cloud-asr`):
   - `diarization_source` read at `:470`, only `== "off"` derived (`:471`).
   - Local ASR, shared WAV: `_run_engine_pipeline` (`pipelines.py:194-322`) with
     `enable_diarization = source != "off"` (`pipelines.py:224,256`). Split topology:
     `_run_transcribe_only_stage` (`:325-`) + `diarize_gpu_task` (`diarize_task.py:78-79`, same
     `== "off"` check).
   - Local ASR, MinIO fallback: `_run_transcription_pipeline` (`pipelines.py:98-191`,
     `enable_diarization=not disable_diarization` `:141`).
   - Cloud ASR: `_run_cloud_asr_pipeline` (`cloud_asr.py:184-313`):
     `enable_diarization = (source == "provider" and provider.supports_diarization())` (`:251`);
     `source == "pyannote"` -> `_run_parallel_cloud_asr_and_diarization` (`:265-275`, body
     `:67-181`): ASR and pyannote.ai in a 2-thread pool, merged by
     `utils/diarization_merge.merge_cloud_diarization`. pyannote.ai failure is non-fatal
     (speakerless transcript, `:166-169`); a missing key is fatal before either leg starts
     (`:89-93`, #1204).
4. **CPU lightweight task** `tasks/transcription/cpu_task.py`: docstring "Diarization is skipped
   (PyAnnote requires CUDA)" (`:1-4`) and `min_speakers=max_speakers=1` (`:59-60`). The CUDA claim
   is stale: lite runs PyAnnote on CPU and the native sidecar has a CPU EP.
5. **Postprocess** (CPU) `tasks/transcription/postprocess.py:214-344`:
   - cloud ASR + `local` -> dispatch `rediarize_task` on `gpu_preferred_queue()` (`:218-265`;
     resolves to `cpu` under lite, #865), task stays open, rediarize is the completion + metering
     terminus.
   - cloud ASR + any other non-off source -> `extract_speaker_embeddings_task` on `cpu`
     (`:268-294`), which closes the pipeline task.
   - local ASR -> native embeddings inline (`:295-310`).

### A4. THE silent-ignore bug (confirmed)

`DiarizationProviderFactory` has exactly one production call site:
`cloud_asr.py:85,93`. Every local-ASR path checks only `== "off"` (`core.py:471`,
`pipelines.py:224,256,353`, `diarize_task.py:79`). So `pyannote` + local Whisper runs the
**local** diarizer and never contacts pyannote.ai. The recorded provenance is truthful (finalize
records the diarizer that actually ran, `finalize.py:228,362`), but the settings UI offered the
choice and the run ignored it. The UI copy partly admits it
(`frontend/src/lib/i18n/locales/en.json:4568` "...when a cloud provider transcribes"), the
select does not (`frontend/src/components/settings/SpeakerDetectionProviderCard.svelte:29-50`).
The previous plan recorded the same fact as PC5. Same shape, second instance: `provider` +
a text-only cloud model (OpenAI `whisper-1`/`gpt-4o-transcribe`) yields no speakers with no
warning (`cloud_asr.py:251`), which is #1219.

Also dead: the factory's `local` branch (`factory.py:91-92,147-148`) and
`services/diarization/local_provider.py` (`LocalDiarizationProvider`, delegates to
`ModelManager.get_diarizer()`); `rediarize_task` calls the diarizer directly
(`tasks/rediarize_task.py:176-207`).

### A5. Merging speakers into words: two implementations doing one job

| Implementation | Used by | Algorithm |
|---|---|---|
| `utils/diarization_merge.merge_cloud_diarization` (`:21-113`) | cloud ASR + pyannote.ai | word midpoint lookup (bisect), segment = majority vote; never splits a segment |
| `transcription/speaker_assigner.assign_speakers` (`:27-`) | local pipeline, `rediarize_task` (`:336-338`) | vectorised overlap matrix per word and segment |

Repo rule: one path per job. This plan unifies on `assign_speakers` (WP4).

### A6. Which ASR providers return speakers, and which honour hints (from code)

`ASR_PROVIDER_CATALOG` (`services/asr/factory.py:47-480`) and each provider's
`supports_diarization()`:

| Provider | Speakers | Count hints actually sent (code) | Evidence |
|---|---|---|---|
| local (Whisper + native/PyAnnote) | yes | PyAnnote engine: min/max/num; **native engine ignores them** (`transcription/diarizer_native.py:948`, plan PC6) | |
| deepgram | yes | none | `deepgram_provider.py:148` |
| assemblyai | yes | `num` only (`speakers_expected`) | `assemblyai_provider.py:131-132` |
| openai | **only `gpt-4o-transcribe-diarize`** | none | `openai_provider.py:58-61`; catalog provider-level `False` `factory.py:231`, model `True` `:250-255` |
| google | yes | min/max | `google_provider.py:115-119` |
| azure | yes (ConversationTranscriber) | none | `azure_provider.py:129` |
| aws | yes | max (clamped 2-30) | `aws_provider.py:220-223` |
| speechmatics | yes | none | `speechmatics_provider.py:127` |
| gladia | yes | none | `gladia_provider.py:272` |
| pyannote (STT orchestration) | yes | min/max/num | `asr/pyannote_provider.py:249-255` |
| pyannote.ai diarization-only | yes | min/max/num | `diarization/pyannote_provider.py:335-340` |

`get_model_capabilities` (`asr/factory.py:772-831`) returns translation/language only, so
`/asr-settings/status.active_model_capabilities` carries **no** `supports_diarization`. The
`/asr-settings/providers` catalog does (`asr_settings.py:306-331`; typed in
`frontend/src/lib/api/asrSettings.ts:40,65,192,216`).

### A7. Lite mode, exactly

- `docker-compose.lite.yml:9-18` scales both GPU workers to 0; every backend service uses the lite
  image with `DEPLOYMENT_MODE=lite`.
- `backend/requirements-lite.txt` has **no faster-whisper / ctranslate2 / whisperx** (rg finds
  none; they live in `requirements.txt:112-123` and `requirements-nodeps.txt:23`). It does install
  `torch==2.11.0+cpu`, `torchaudio`, `pyannote.audio==4.0.7`, `onnxruntime` (`:70-134`).
- Local ASR is refused by name: `_guard_local_allowed` (`asr/factory.py:16-33`) raises
  `ASRConfigurationError`, and dispatch marks the file ERROR (`dispatch.py:357-366`).
  **So lite has no local transcription, CPU or otherwise.**
- Local **diarization** works on lite: cloud ASR + `local` dispatches `rediarize_task` to
  `gpu_preferred_queue()` = `cpu` (`postprocess.py:255-260`), served by in-process PyAnnote on CPU
  or the diar-native sidecar's CPU EP (`--with-diar-native`). Speaker embeddings likewise
  (`services/speaker_embedding_service.py:105-117`).
- Doc drift found: `docs-site/docs/operations/deployment-configuration.md:30` says "the lite image
  runs no in-process PyAnnote (issue #660)"; the code and `requirements-lite.txt` say it does.
  Fix in WP6.
- Unverified hazard (write a red test before trusting): a per-file lightweight model (`tiny`) on
  lite bypasses `_resolve_gpu_queue` (`dispatch.py:357`), routes to `cpu-transcribe` (consumed by
  `celery-cpu-worker`, `docker-compose.yml:526`) and would fail on the missing faster-whisper
  instead of the clear `ASRConfigurationError`. No lite guard on that branch was found.
- Why lite keeps `pyannote.audio` + `torch+cpu`: the voiceprint path. The diar-native sidecar must
  export its own ONNX graphs from the PyAnnote/WeSpeaker weights (`requirements-lite.txt:73-120`
  comment block), and the in-process WeSpeaker fallback is the lite degrade path
  (`speaker_embedding_service.py:105-117`); `tests/unit/test_lite_export_requirements_declared.py`,
  `test_requirements_lite_torch_guard.py`, `test_diar_native_lite_image_pairing.py` and
  `test_opentranscribe_lite_diar_native_image.py` pin that. The owner's rule "lite means no GPU,
  not small" (root `CLAUDE.md`, memory "Capability over image size") forbids stripping these.

**Owner decision (2026-10-11): lite is cloud-only for transcription AND speaker detection.** Lite
runs no local transcription (already true) and, after WP6, no local speaker detection. Voiceprint
embeddings still run locally on CPU in lite. The existing lite rediarize-on-CPU path
(`postprocess.py:224-265`, whose comment at `:255-259` names lite as its reason for routing via
`gpu_preferred_queue()`) is removed for lite; see WP6 for exactly what remains.

### A8. Full image local CPU transcription

Exists only as a **per-file** choice of a lightweight model (upload/reprocess model picker,
`frontend/src/lib/api/transcriptionSettings.ts:227-231`), with diarization forced off. There is no
user-level "transcribe on CPU" setting and no user-level default model.

### A9. Speaker identification across files (voiceprints)

Always computed **on this server** from the audio plus speaker turns, whatever diarized:
local runs reuse the diarizer's native embeddings (`postprocess.py:301-310`); cloud runs dispatch
`extract_speaker_embeddings_task` on `cpu` (`postprocess.py:268-294`,
`tasks/speaker_embedding_task.py:1-9`). v4 vectors come from the diar-native `/embed_window` or
in-process WeSpeaker (`services/CLAUDE.md` "Speaker embeddings: two backends"). Nothing is sent
to a vendor for voiceprints.

How today's cloud path gets voiceprints, precisely: `_run_cloud_asr_pipeline` returns only
segments + provider/model (`cloud_asr.py:305-313`), no vectors. The transcript is saved, then
`finalize_transcription` dispatches `extract_speaker_embeddings_task` on `cpu`
(`postprocess.py:269-292`). That task reads the stored segments' `start_time`/`end_time`/
`speaker_id` from Postgres (`speaker_embedding_task.py:66-108`), stages the preprocessed WAV
(`:134-152`), calls `embedding_service.extract_embeddings_for_segments` per speaker
(`:169-195`), then matches against profiles/clusters (`_match_and_finalize_embeddings`, `:213`).
The pyannote.ai diarization provider requests turns only (`transcription: False`, no
embedding/voiceprint option, `diarization/pyannote_provider.py:325-340`) and its
`DiarizeResult.metadata` holds only `job_id`/`elapsed_ms` (`:216-222`). So the vendor contributes
turns; the voiceprint is always ours. The rule this plan generalises in C6.

### A10. Timestamp granularity each provider/model returns (from code)

Assigning speakers to text needs times on the text. What each provider module actually parses:

| Provider / model | Granularity | Evidence | Notes |
|---|---|---|---|
| local faster-whisper | **word** | `transcription/transcriber.py:401` (`word_timestamps=True`) | cross-attention word times, no wav2vec2 alignment |
| deepgram (all) | **word** | `deepgram_provider.py:209-230` | |
| assemblyai (all) | **word** | `assemblyai_provider.py:177-192,213-228` | ms -> s |
| openai `whisper-1` | **segment** | `openai_provider.py:127-136` (`verbose_json`, no `timestamp_granularities`) | The API can return word times with `timestamp_granularities=["word"]` (vendor docs, **unverified**, not requested here). A cheap upgrade to word, tracked as a WP4 sub-step. |
| openai `gpt-4o-transcribe`, `gpt-4o-mini-transcribe` | **none** | `openai_provider.py:146-153` (`json` only), fallback `:247-248` makes ONE segment with `start=end=0.0` | The code comment records that `verbose_json` is rejected for these models. |
| openai `gpt-4o-transcribe-diarize` | **segment** | `openai_provider.py:138-145` (`diarized_json` segments with start/end) | speakers included, at segment level |
| google (all) | **word** | `google_provider.py:128` (`enable_word_time_offsets=True`), `:183-235` | |
| azure (all) | **segment, word only if returned** | `azure_provider.py:125` (`OutputFormat.Detailed`), `:182-196,281-297` parse `NBest[0].Words` | Word-level timestamps are not explicitly requested (`request_word_level_timestamps()` is absent), so `Words` may be empty (Azure SDK behaviour: **unverified**). Treat as segment until a live test proves word. |
| aws (all) | **word** | `aws_provider.py:369,393` (items with start/end) | |
| speechmatics | **word** | `speechmatics_provider.py:233` | |
| gladia | **word** | `gladia_provider.py:397-404` | |
| pyannote (STT orchestration) | **word** | `asr/pyannote_provider.py:432-468` (`wordLevelTranscription`) | |

There is **no forced-alignment stage** in the pipeline today. `whisperx==3.8.6` is installed in the
full image (`requirements-nodeps.txt:22`, it ships wav2vec2 alignment), but nothing under
`backend/app` calls its `align`/`load_align_model` (rg finds no caller). Lite has no whisperx.

### A11. Per-file provenance recorded today

| Step | What is recorded | Where written | Where shown |
|---|---|---|---|
| Transcription | `media_file.asr_provider`, `asr_model`, `whisper_model` (`models/media.py:285-310`); also `FilePipelineTiming.asr_provider/asr_model` (`models/pipeline_timing.py:144-145`) | `tasks/transcription/storage.py:201-213` | `MediaFile` schema `schemas/media.py:700-709`; `components/MetadataDisplay.svelte:239-275` (hardcoded English label "Whisper:"); `/my-files/{uuid}/status` adds `whisper_model`/`requested_whisper_model` (`api/endpoints/user_files.py:301,342-347`) |
| Speaker detection | `diarization_provider`, `diarization_model`, `diarization_disabled` | `storage.py:204-216`; values come from the engine that ran (`finalize.py:228,362`) or `merge_cloud_diarization` metadata (`diarization_merge.py:108-111`) | `MetadataDisplay.svelte:255-262` |
| Voiceprints | `embedding_mode` only (`v3`/`v4`); not the backend (native vs in-process) nor the model | `storage.py:207-208` | `MetadataDisplay.svelte:269-274` |
| Summary | `media_file.summary_data["metadata"]["provider"/"model"]` | `services/llm_service.py:1510-1513` (also `:1472-1473` partial path) | not shown as provenance |
| Topics | nothing (`models/topic.py:58-100` has no provider/model) | `tasks/topic_extraction.py` | — |
| Speaker-name suggestions | `speaker.suggestion_source` (`llm_analysis`/`voice_match`/`profile_match`, `models/media.py:630-632`), no provider/model | `tasks/speaker_identification_task.py` | — |

Defects found while mapping this (fixed in WP7):
- **Ambiguous id**: the local engine and the pyannote.ai cloud provider both report
  `diarization_provider = "pyannote"` (`diarization/pyannote_provider.py:220`; local PyAnnote
  engine via `finalize.py`), and `MetadataDisplay.svelte:258-260` renders anything that is not
  `native` as the local PyAnnote engine. A pyannote.ai run, or after WP4 a Deepgram-as-diarizer
  run, displays as "PyAnnote engine (this server)". Historical rows cannot be disambiguated.
- `local_provider.py:119` reports `provider_name="local"`, a third spelling.

---

## B. Which cloud vendors can act as a diarizer

Legend: **code** = verified in this repo; **docs (unverified)** = from vendor documentation as of
the planner's knowledge (cutoff mid-2026), not re-checked live; treat as unverified until a
provider test passes.

| Vendor | Speaker labels | Diarization-ONLY product? | Count hints (vendor API) | Notes |
|---|---|---|---|---|
| pyannote.ai | code | **Yes** (`POST /v1/diarize`, `precision-3`) | min/max/num (code) | Also STT orchestration (separate ASR provider here) and voiceprint/identify endpoints (docs, unverified). Job-based upload + poll; poll timeout 300 s on the ASR side (`asr/CLAUDE.md`). |
| Deepgram | code (`diarize=true`) | No | none (docs, unverified) | Billed as transcription. |
| AssemblyAI | code (`speaker_labels`) | No | `speakers_expected` (code); `speaker_options` min/max (docs, unverified, not wired) | |
| Speechmatics | code | No | `max_speakers`, `speaker_sensitivity` (docs, unverified, not wired) | |
| Gladia | code | No | `diarization_config` number/min/max (docs, unverified, not wired) | |
| Google STT v2 | code | No | min/max (code) | Diarization availability varies by model/region (docs, unverified). |
| Azure Speech | code (ConversationTranscriber) | No | none in code; Fast Transcription API has `maxSpeakers` (docs, unverified) | |
| AWS Transcribe | code | No | `MaxSpeakerLabels` 2-30 (code) | Round-trips via S3. |
| OpenAI | code, `gpt-4o-transcribe-diarize` only | No | none; known-speaker references (docs, unverified) | 25 MB limit. |
| Others not integrated | ElevenLabs Scribe, Soniox, Rev.ai: ASR with diarization (docs, unverified). Picovoice Falcon, NVIDIA NeMo/Sortformer: diarization-only but **local SDK/models**, not cloud APIs. | | | No other hosted diarization-ONLY API is known to the planner; this was not exhaustively verified. |

**Conclusion.** pyannote.ai is the only hosted diarization-only service in reach. Every other
vendor can diarize only by transcribing, so using vendor X for speakers alongside vendor Y (or
local Whisper) for text means paying X for a transcription whose text is thrown away.

### B1. Adapter design (recommended): `ASRDiarizationAdapter`

A `DiarizationProvider` that wraps any `ASRProvider` whose `supports_diarization()` is true:

```python
# backend/app/services/diarization/asr_adapter.py  (new, ~120 lines)
class ASRDiarizationAdapter(DiarizationProvider):
    """Speaker turns from a speech-to-text vendor's own diarization; its text is discarded."""

    def __init__(self, asr_provider: ASRProvider) -> None: ...
    # provider_name -> f"{asr_provider.provider_name}" ; model_name from the ASR provider
    def supports_speaker_count(self) -> bool:  # from catalog speaker_count_hints (C5)
    def validate_connection(self): return self._asr.validate_connection()
    def diarize(self, audio_path, config: DiarizeConfig, progress_callback=None) -> DiarizeResult:
        result = self._asr.transcribe(audio_path, ASRConfig(
            language=None, min_speakers=config.min_speakers, max_speakers=config.max_speakers,
            num_speakers=config.num_speakers, enable_diarization=True,
            translate_to_english=False, vocabulary=None), progress_callback)
        # Word-level turns: each word with a speaker -> DiarizeSegment(start, end, speaker);
        # coalesce consecutive same-speaker words (gap <= 0.5 s) into one segment.
        # Words absent -> fall back to segment-level (start, end, speaker).
        # Never keep text: the returned DiarizeResult has no text field, and the ASRResult is
        # dropped before return so no caller can reach it.
```

Trade-offs, stated in UI copy and docs:

- **Double billing**: the diarizer vendor bills a full transcription.
- **Privacy**: audio goes to a second vendor. With local Whisper text + vendor speakers, audio
  still leaves the server.
- **Text is discarded**, never stored, never logged (log only counts).
- Quality depends on the vendor's word timestamps; turns are derived from words, so overlap is
  lost (same as today's provider path).

Badges (decided, owner 2026-10-11): every provider/model shows exactly one of
**Speakers included** (model returns speaker labels), **Text only** (it does not), and, where it
is chosen as a diarizer for a different transcription provider, **Speakers via adapter (extra
billing)**. A second badge shows timing: **Word timing / Segment timing / No timing** (A10, C7).

### B2. Do vendors return speaker embeddings? (no usable ones)

| Vendor | Returns vectors? | Form | Usable in our space? |
|---|---|---|---|
| pyannote.ai `/v1/diarize` (precision-2/3) | No vectors in the diarize output as requested here (code: turns only, `diarization/pyannote_provider.py:216-222,325-340`). | — | — |
| pyannote.ai voiceprint / identify (`/v1/voiceprint`, identify jobs) | A "voiceprint" per enrolled sample (vendor docs, **unverified**: an opaque encoded string, built from a short reference clip, consumed only by their identify endpoint). | Opaque vendor token, model and dimension undocumented. | **No.** Our index is 256-d WeSpeaker ResNet34 cosine (`services/CLAUDE.md`, `opensearch_service`); an opaque token from another model has no linear map into it (the same reason v3 512-d cannot be routed at v4). |
| Speechmatics speaker identification, OpenAI `known_speaker_references`, Azure speaker recognition | Enrollment-style identifiers or reference audio, not embeddings returned per speaker (vendor docs, **unverified**; Azure Speaker Recognition was announced for retirement, **unverified**). | Opaque / reference-based. | **No.** |
| Deepgram, AssemblyAI, Gladia, Google, AWS | No speaker vectors (vendor docs, **unverified**; none parsed in code). | — | — |

Confidence: high that nothing usable exists (no vendor exposes raw vectors in the
pyannote/WeSpeaker 256-d space, and the code consumes none); medium on the exact shape of each
vendor's identification features (unverified docs). **Decided (Q12): vendor voiceprints stay
unused.** Voiceprints come from the local embedding stage (C6) for every combination.

(B1 continued, placed here for reading order:) first-class diarization-only providers stay as
they are (pyannote.ai, `PyAnnoteCloudDiarizationProvider`); a new hosted diarization-only vendor
would be a new module in `services/diarization/` plus a factory branch. The adapter's
credentials come from a **saved ASR configuration** (`UserASRSettings`) via
`ASRProviderFactory.create_from_db_config(cfg)` (the one decryption path, `asr/CLAUDE.md`); no
new credential store; pyannote.ai keeps its #1204 table. The adapter keeps `ASRWord` times when
the vendor returns them; a vendor with segment-only timing yields segment-granular turns (C7).

---

## C. Target architecture

### C1. One decision point: `PipelinePlan`

Today the composition is decided in seven places by string comparison (A3). Replace that with one
pure resolver, computed once per dispatch and carried in the task context.

New module `backend/app/services/pipeline_plan.py` (pure, no DB, no imports of model code;
target < 300 lines) plus a loader:

```python
TranscriptionEngine = Literal["local_gpu", "local_cpu", "cloud"]
DiarizationEngine = Literal["asr", "local", "pyannote_ai", "asr_config", "off"]
Execution = Literal[
    "inline_asr",        # the ASR call returns speakers (cloud ASR + provider)
    "inline_local",      # the local engine diarizes inside the GPU task (today's local path)
    "parallel_cloud",    # cloud ASR + cloud diarizer in one cloud-asr task (today's pyannote path)
    "post_stage_local",  # rediarize_task, engine=local, queue gpu_preferred_queue()
    "post_stage_cloud",  # rediarize_task, engine=cloud, queue cloud-asr
    "none",              # off
]
Where = Literal["server", "cloud"]

@dataclass(frozen=True)
class PlanIssue:
    code: str            # stable, i18n key suffix: settings.pipeline.issue.<code>
    severity: Literal["error", "warning", "info"]

@dataclass(frozen=True)
class TranscriptionStep:
    engine: TranscriptionEngine
    provider: str              # "local" or vendor id
    model: str | None
    where: Where
    queue: str                 # CeleryQueues value
    returns_speakers: bool     # model-level supports_diarization
    timing: Literal["word", "segment", "none"]   # catalog timestamp_granularity (A10, C7)
    config_uuid: str | None

@dataclass(frozen=True)
class DiarizationStep:
    source: str                # the stored value, unchanged
    engine: DiarizationEngine  # what will actually run
    provider: str | None       # "native" | "pyannote" (local engines) | "pyannote.ai" | vendor id
    model: str | None
    where: Where | None        # None when off
    execution: Execution
    queue: str | None
    speaker_hints: frozenset[str]   # subset of {"min","max","num"} the engine honours
    config_uuid: str | None         # the ASR config used as a diarizer (asr_config only)
    merge: Literal["word", "segment", "none"]   # how turns reach the text (C7)

@dataclass(frozen=True)
class SpeakerIdStep:
    runs: bool                 # False when diarization is off
    where: Where               # always "server" (C6), lite included
    backend: str               # "native" | "pyannote" (embedding backend, informational)
    execution: Literal["inline_native", "embedding_stage", "none"]   # C6
    queue: str | None          # cpu for embedding_stage; None when inline/none

@dataclass(frozen=True)
class PipelinePlan:
    transcription: TranscriptionStep
    diarization: DiarizationStep
    speaker_id: SpeakerIdStep
    issues: tuple[PlanIssue, ...]
    def blocking(self) -> tuple[PlanIssue, ...]: ...   # severity == "error"
    def to_context(self) -> dict: ...                  # JSON-safe, carried in preprocess_context
    @classmethod
    def from_context(cls, d: dict) -> "PipelinePlan": ...

@dataclass(frozen=True)
class PlanInputs:
    deployment_mode: str                 # "full" | "lite"
    asr_provider: str                    # resolved like ASRProviderFactory.create_for_user
    asr_model: str | None
    asr_config_uuid: str | None
    asr_returns_speakers: bool
    asr_speaker_hints: frozenset[str]
    asr_timing: Literal["word", "segment", "none"]
    whisper_model_override: str | None   # per-file; lightweight -> local_cpu
    diarization_source: str              # after lock + per-file "off"
    source_locked: bool
    diarizer_backend: str                # engine.diarizer_backend, informational
    has_pyannote_key: bool
    diarization_config: DiarizerConfigInfo | None   # provider, model, uuid, returns_speakers
    gpu_split: bool

def resolve_pipeline_plan(inputs: PlanInputs) -> PipelinePlan: ...      # pure
def load_plan_inputs(db, user_id, *, whisper_model=None,
                     disable_diarization: bool | None = None) -> PlanInputs: ...
```

`load_plan_inputs` reuses, never duplicates: ASR resolution via
`ASRProviderFactory.create_for_user` logic (extract the "which config row" half into
`ASRProviderFactory.resolve_active_config(user_id, db) -> (provider, model, cfg|None)` so neither
the factory nor the plan re-implements the precedence), locks via
`locked_transcription_fields()`, key presence via `has_pyannote_credential`.

### C2. The selectors

**Transcription engine** (unchanged storage, clarified meaning):

| Engine | Chosen by | Queue | Where |
|---|---|---|---|
| `local_gpu` | active ASR = local (and model not lightweight) | `gpu` / `gpu-transcribe` | server |
| `local_cpu` | per-file lightweight model, **full image only** (lite has no local transcription, decided) | `cpu-transcribe` | server |
| `cloud` | active ASR config = vendor+model | `cloud-asr` | cloud |

**Speaker detection** (`transcription_diarization_source`, values extended by ONE):

| Stored value | UI label (en) | Engine resolved |
|---|---|---|
| `provider` | Automatic (recommended) | `asr` if the transcription model returns speakers; else `local` on the full image when the model has word or segment timing (decided Q1, C7); else an error (no timing, or lite) |
| `local` | This server | `local` (engine = admin `engine.diarizer_backend`, chosen at setup, read-only for users, decided Q3). **Not offered on lite.** |
| `pyannote` | pyannote.ai cloud service | `pyannote_ai` |
| `asr_config` **(new)** | Another provider's speaker detection | `asr_config`, using `UserSetting["transcription_diarization_asr_config_uuid"]` **(new key)** |
| `off` | Off | `off` |

`asr_config` pointing at the same provider+model as the active transcription config resolves to
`asr` with an `info` issue `diarizer_same_as_transcription` (no double billing).

### C3. Validity matrix (what the pipeline does for each)

Rows: transcription engine. Columns: resolved diarization engine.

| | `asr` | `local` | `pyannote_ai` | `asr_config` (vendor V) | `off` |
|---|---|---|---|---|---|
| **local_gpu** | = `local` (local's own is local) -> `inline_local` in GPU task (today) | `inline_local` (today) | **NEW** GPU task runs Whisper with diarization OFF; postprocess dispatches `rediarize_task(engine=pyannote_ai)` on `cloud-asr` -> `assign_speakers` -> embeddings on `cpu` | **NEW** same as pyannote_ai with `ASRDiarizationAdapter(V)` | today |
| **local_cpu** | = `local` -> `post_stage_local` (rediarize on `gpu_preferred_queue()`) **NEW** (today forced off) | `post_stage_local` **NEW** | `post_stage_cloud` **NEW** | `post_stage_cloud` **NEW** | today |
| **cloud** (returns speakers) | `inline_asr` (today) | `post_stage_local` (today, rediarize) | `parallel_cloud` (today) | `parallel_cloud` **NEW** via adapter | today |
| **cloud** (text only, word or segment timing) | decided Q1: -> `local` (`post_stage_local`) with `info automatic_uses_server` | `post_stage_local` | `parallel_cloud` | `parallel_cloud` **NEW** | today |
| **cloud** (no timing: `gpt-4o-transcribe`, `gpt-4o-mini-transcribe`) | **refused** `asr_no_timing_for_speakers` | **refused** (same) | **refused** (same) | **refused** (same) | today (the only valid choice) |

The timing column is applied on top of every cell (C7): `word` -> per-word merge; `segment` ->
segment-level merge with a `segment_timing_only` warning; `none` -> any diarization is refused.

**Lite rows (decided: lite is cloud-only for transcription and speaker detection).**

| Lite transcription | `asr` | `local` | `pyannote_ai` | `asr_config` | `off` |
|---|---|---|---|---|---|
| `local_gpu` / `local_cpu` | **refused** `local_asr_unavailable` | refused | refused | refused | refused |
| cloud (returns speakers) | `inline_asr` | **refused** `local_diarization_unavailable` | `parallel_cloud` | `parallel_cloud` | ok |
| cloud (text only, timed) | **refused** `automatic_needs_speaker_provider` (steer: pick pyannote.ai or a provider that returns speakers) | refused | `parallel_cloud` | `parallel_cloud` | ok |
| cloud (no timing) | refused `asr_no_timing_for_speakers` | refused | refused | refused | ok |

On every lite row that diarizes, the voiceprint stage still runs locally on CPU (C6).
Impossible on lite because there is no local alignment and no local diarizer: any diarization
with a no-timing model; Automatic with any text-only model; `local` with anything.

Errors (block dispatch AND the settings PUT for a new selection; stable codes, each with a fixed
user sentence registered in `ErrorCategorizationService`):

| Code | When |
|---|---|
| `pyannote_key_missing` | `pyannote` with no stored key (today's `DiarizationNotConfiguredError`, kept) |
| `diarizer_config_missing` | `asr_config` and the uuid is absent, deleted, or no longer shared with the user |
| `diarizer_config_no_speakers` | `asr_config` and that config's model does not return speakers |
| `local_asr_unavailable` | `local_gpu`/`local_cpu` on lite (today's `ASRConfigurationError`, kept) |
| `local_diarization_unavailable` | **new**, lite + `local` |
| `automatic_needs_speaker_provider` | **new**, lite + Automatic + text-only model |
| `asr_no_timing_for_speakers` | **new**, any diarization with a `timing == "none"` model (full and lite) |

Warnings/info (never block): `automatic_uses_server` (Q1), `segment_timing_only` (C7),
`speaker_hints_ignored` (engine honours none of the user's hints; native engine included),
`diarizer_double_billing` (asr_config), `source_locked` (info, #1109). `audio_leaves_server` is
not an issue but a per-step `where == "cloud"` flag the UI renders as the privacy note.
(`local_diarization_cpu` from the first draft is deleted: lite no longer diarizes locally.)

Topology notes:

- GPU split (`ENGINE_GPU_SPLIT`) only applies to `inline_local`. For any `post_stage_*` the GPU
  task runs the non-split engine path with diarization off; `_resolve_gpu_queue` must not pick
  `gpu-transcribe` for those (it would forward to `diarize_gpu_task`, which diarizes locally).
- `post_stage_cloud` runs on `cloud-asr` (I/O-bound, prefork, concurrency 16), never on the GPU
  worker: holding a GPU slot while polling pyannote.ai is the waste this design avoids. Cost:
  latency = ASR + cloud diarization, sequential. A later optimisation (out of scope) can submit
  the pyannote.ai job from preprocess and poll in the post stage.
- Lite: `post_stage_local` is never produced (refused above). `cloud-asr` is staffed on lite
  (`docker-compose.lite.yml:25-29`) and the embedding stage's `cpu` queue is too; add both to
  `tests/unit/test_lite_mode_queue_consumers.py`.

### C4. The post stage: one task, engine-parametrised

Generalise `tasks/rediarize_task.py` instead of adding a sibling (one path per job):

```python
def rediarize_task(self, file_uuid, min_speakers=None, max_speakers=None, num_speakers=None,
                   downstream_tasks=None, pipeline_completion=False, pipeline_task_id=None,
                   engine: str = "local", diarizer_config_uuid: str | None = None): ...
```

- `engine` in {`local`, `pyannote_ai`, `asr_config`}. The task builds a `DiarizationProvider`
  through `DiarizationProviderFactory.create_for_engine(engine, user_id, db,
  config_uuid=...)` (new; replaces `create_for_user`'s source switch and `create_for_source`):
  `local` -> `LocalDiarizationProvider` (finally used; replaces `rediarize_task._run_diarization`
  `:176-207`), `pyannote_ai` -> `PyAnnoteCloudDiarizationProvider`, `asr_config` ->
  `ASRDiarizationAdapter(ASRProviderFactory.create_from_db_config(cfg))`.
- Merge: `assign_speakers` on the stored transcript (today's rediarize behaviour). For cloud
  results convert `DiarizeResult.segments` to the frame `assign_speakers` expects via a small
  helper `diarize_segments_to_frame()` in `transcription/speaker_assigner.py`.
- Embeddings: `local` keeps today's inline native-embedding handling; cloud engines hand off to
  the voiceprint stage (C6), dispatching `extract_speaker_embeddings_task` on `cpu` with
  `pipeline_task_id` (it already closes the pipeline task,
  `speaker_embedding_task.py:_close_pipeline_task`).
- Merge granularity follows C7 (`plan.diarization.merge`).
- Lite: `engine == "local"` raises the `local_diarization_unavailable` configuration error at the
  top of the task (defence in depth behind the plan and the API).
- Session lifetime: read phase (plain data incl. decrypted key resolved into the provider object
  only, never an ORM row), HTTP/model work with **no session open**, short write phase.
  `scripts/audit-session-lifetime.py` must stay clean.
- Failure semantics: a missing key/config fails the file with the fixed sentence (reuse
  `DiarizationNotConfiguredError`; add `DiarizerConfigUnusableError` registered in
  `ErrorCategorizationService` with retry category `CONFIGURATION_REQUIRED`). A vendor-side
  failure of a configured engine stays non-fatal (transcript kept, no speakers), matching
  `cloud_asr.py:166-169`, and records `diarization_provider=None` so the UI shows nothing ran.
- Routing: `queue = gpu_preferred_queue()` for `local`, `CeleryQueues.CLOUD_ASR` otherwise.
  Confirm `celery-cloud-asr-worker`'s import list includes `app.tasks.rediarize_task`
  (`core/celery.py` include) and that the module has no model-loading top-level import.
- Manual "rediarize" reprocess stage (`api/endpoints/files/reprocess.py`): uses the user's current
  plan engine (decided Q9; `asr` -> `local` on the full image; on lite `asr`/`local` -> 409
  `local_diarization_unavailable`, a cloud engine runs on `cloud-asr`).

### C5. Speaker-count hints per engine

Add `speaker_count_hints: list[Literal["min","max","num"]]` to every provider entry (and model
entry where it differs) in `ASR_PROVIDER_CATALOG`, filled from the **code** column of A6, never
from vendor docs. Local: derived from `engine.diarizer_backend` (`native` -> `[]`, `pyannote` ->
all three). pyannote.ai: all three. `resolve_speaker_range` (`tasks/transcription/user_settings.py`)
stays the one precedence for the values; the plan only says which are honoured, so the UI can
show "This engine ignores the speaker range" (`speaker_hints_ignored`). Wiring the
docs-only hints (AssemblyAI min/max, Speechmatics, Gladia) is a follow-up issue, not this plan.

### C6. Voiceprints: one local embedding stage after ANY speaker detection

Principle (decided, Q12): voiceprints are always computed on this server, in the 256-d WeSpeaker
space, from the audio plus whichever speaker turns won (A9, B2). Vendor voiceprints are never
used. Today this is already true for cloud ASR (A9, "How today's cloud path gets voiceprints");
the target makes it an explicit, plan-driven stage so cross-file speaker identification and
clustering behave identically for every combination, lite included.

Rule: after the transcript carries its final speaker labels, exactly one of these runs
(`plan.speaker_id.execution`):

| Execution | When | Where it runs | Cost |
|---|---|---|---|
| `inline_native` | `inline_local` diarization on the full image (the diarizer already produced per-speaker vectors in the same pass; `postprocess.py:301-310`) | GPU worker, no extra pass | ~0 (already paid by the diarizer) |
| `embedding_stage` | every other diarizing combination: `inline_asr`, `parallel_cloud`, `post_stage_cloud`, and `post_stage_local` when the local engine returned no native vectors | `extract_speaker_embeddings_task` on `cpu` (`postprocess.py:269-292`; `speaker_embedding_task.py:295`), backend = diar-native `/embed_window` (CPU EP on lite, GPU/CPU on full) or in-process WeSpeaker fallback | One WAV staging + one embedding per speaker over its segments (`speaker_embedding_task.py:169-195`); per-segment work bounded by `NATIVE_EMBEDDING_MAX_TILES`. Wall time is NOT measured for this plan (do not quote a number): WP3 records the stage timing via `benchmark_timing` on one real file. |
| `none` | diarization off | — | 0 |

Inputs are the stored segments (start/end/speaker), so the stage works for word- or
segment-timed transcripts; it cannot run for `timing == "none"`, which is why that combination is
refused (C3). The stage is the one place matching/clustering happens
(`_match_and_finalize_embeddings`, `speaker_embedding_task.py:213`), so every combination feeds
the same profiles and clusters. Lite: the stage is the reason lite keeps `pyannote.audio`,
`torch+cpu` and the diar-native sidecar's embedding graph (A7). The Pipeline view shows this step
as "This server" always.

### C7. Timing-aware merge (decided strategy by capability)

A diarizer returns speaker **turns** `(start, end, speaker)`. Text needs times to receive them.
`ASR_PROVIDER_CATALOG` gains `timestamp_granularity: "word" | "segment" | "none"` per provider and
per model (model wins), filled from A10's code evidence only; Azure is `segment` until a live
test proves `Words` are populated; `whisper-1` is `segment` unless WP4's sub-step requests word
granularity and a test proves it.

| Granularity | Merge | What is lost | Plan issue |
|---|---|---|---|
| `word` | `assign_speakers` per word (overlap), segment speaker = dominant word speaker; then `utils/segment_postprocess.finalize_segments` (boundary smoothing, re-segment at word-speaker boundaries, merge same-speaker; `finalize.py:156-165`) splits segments at turn changes. ⚠️ `rediarize_task` today skips `finalize_segments` (`rediarize_task.py:336-366` goes straight from `assign_speakers` to `save_transcript_segments`), so today's cloud ASR + local path never re-splits; WP3's post stage must call `finalize_segments` (the documented single chokepoint, `finalize.py:157`) | nothing beyond the diarizer's own error | none |
| `segment` | `assign_speakers` at segment level: each segment gets the speaker whose turns overlap it most (the same overlap rule, applied to the segment interval); words, if any, inherit the segment speaker | speaker changes **inside** a segment (whisper-1 segments run up to ~30 s): the shorter speaker's words are attributed to the dominant one; backchannels vanish; voiceprints for a segment mixing two voices are slightly contaminated | `segment_timing_only` (warning, shown in Settings and the Pipeline view) |
| `none` | impossible | everything | `asr_no_timing_for_speakers` (error; UI warning on the model badge and in the speaker card) |

Options for `none`, considered:
1. **Refuse** with the stable error and a UI warning (decided default; works identically on full
   and lite).
2. **Local forced alignment**: run wav2vec2 alignment (`whisperx.align`, already installed in the
   full image, `requirements-nodeps.txt:22`, but unused today, A10) on a GPU worker to obtain word
   times for the vendor's text, then merge per word. Costs: a new pipeline stage, per-language
   wav2vec2 model downloads (offline deployments must pre-provision them), languages without an
   alignment model still fail, the text must first be split into the vendor's segment-less blob.
   Not possible on lite (no whisperx, no GPU). Recorded as a follow-up issue, not in v0.6.0
   scope (see H, "remaining owner decision").

`merge_cloud_diarization`'s midpoint rule (A5) is deleted in WP4; `assign_speakers` covers both
the `word` and `segment` rows, with the equivalence test in WP4 as the gate.

---

## D. Back-compat and migration

- **No value renames.** The four stored values keep their meaning except `pyannote` + local
  ASR, which starts doing what it says (the bug fix). One value (`asr_config`) and one key
  (`transcription_diarization_asr_config_uuid`) are added.
- **Data migration** (WP3, Alembic data-only revision, next free number after
  `v433_add_requested_transcription_options`; read `backend/alembic/CLAUDE.md` and
  `backend/app/db/CLAUDE.md` first for lane/numbering rules and the detection guard):
  rewrite `transcription_diarization_source = 'pyannote'` -> `'provider'` for users who have
  **no** `user_diarization_settings` row with `provider='pyannote' AND is_active AND api_key IS
  NOT NULL` **and** whose effective ASR is local (no `active_asr_config_id`, or it points to a
  `user_asr_settings.provider='local'` row; the no-config case additionally requires
  `os.getenv("ASR_PROVIDER","local") == "local"` at migration time). Those users silently got
  local diarization; after the migration they get the same thing, instead of every file failing
  with `pyannote_key_missing`. Cloud-ASR users keep #1204's fail-visible behaviour (deliberate,
  do not "fix"). Downgrade: no-op (document it). Users with a key + local ASR are left alone and
  start using pyannote.ai (Q2; CHANGELOG "Changed").
- **Lite data migration** (WP6, Python data revision; only acts when
  `os.getenv("DEPLOYMENT_MODE","full").lower() == "lite"` at migration time, otherwise a no-op):
  users whose `transcription_diarization_source` is `local` (or `provider` with a text-only
  active model) can no longer run on lite. For each such user: if the active ASR config's model
  returns speakers, rewrite the value to `provider` (they keep getting speakers, now from their
  transcription vendor). Otherwise leave the value untouched: the plan reports
  `local_diarization_unavailable` / `automatic_needs_speaker_provider` in Settings and dispatch
  refuses with that fixed sentence, so the user sees exactly what to choose. The migration
  decides "returns speakers" from a **frozen copy** of the (provider, model) -> diarizes table
  inside the revision file (a migration must not import the live catalog, which will change).
  Downgrade: no-op. No transcript, speaker, voiceprint or profile data changes: files diarized on
  lite's CPU before the change keep their speakers and embeddings. A deployment that switches
  from full to lite later is not migrated; the runtime refusal covers it with a clear error.
- Migration numbering: three revisions are planned (WP3 data, WP6 lite data, WP7 schema). They
  land from different worktrees, so take numbers in merge order from the live head
  (`scripts/release-tests/lib/alembic-head.py`) at rebase time, never from this document, and
  follow the lane rules in `backend/alembic/CLAUDE.md` and `backend/app/db/CLAUDE.md` (the
  detection guard in `app/db/migrations.py` needs an entry for the WP7 column).
- **`diarizer_backend` untouched.** It stays the admin's choice of local engine, made at setup
  (decided Q3), shown read-only to users.
- **Delete, do not shim** (owner rule), all in the WP that replaces them:
  - WP3: `preprocess.py:265-278` bool->source mapping (the plan carries the resolved source);
    every `== "off"` derivation in `core.py:471`, `pipelines.py:224,353`, `diarize_task.py:79`
    (read `plan.diarization.execution`); `DiarizationProviderFactory.create_for_source` and
    `create_for_user` (replaced by `create_for_engine`); the factory's duplicate
    `VALID_DIARIZATION_SOURCES`/`DEFAULT_DIARIZATION_SOURCE` (`factory.py:19-20`; keep
    `core/constants.py:812-813`, re-point importers incl. `api/endpoints/user_settings.py`);
    `rediarize_task._run_diarization`; WP1's selection-time refusal.
  - WP4: `utils/diarization_merge.py` and `tests/unit/test_diarization_merge.py` (after the
    parallel path moves to `assign_speakers`, with the equivalence test below proving no
    regression).
  - `disable_diarization` stays only where it is a per-file input (`requested_disable_diarization`,
    upload/reprocess requests). Internal threading of the derived bool (`preprocess_context
    ["disable_diarization"]`, `result["diarization_disabled"]` derivation) is replaced by the plan;
    `media_file.diarization_disabled` is still written (it is read by the UI and re-runs).

---

## E. API and schema changes

1. `GET /api/user-settings/pipeline` (new router module
   `backend/app/api/endpoints/pipeline_settings.py`, registered in `backend/app/api/router.py`
   beside `diarization_settings`; role user; capability `transcription.prefs`). Response
   `PipelineView` (new `backend/app/schemas/pipeline.py`):

   ```python
   class PipelineIssueOut(BaseModel):
       code: str; severity: Literal["error","warning","info"]; step: Literal["transcription","diarization","speaker_id","llm"]
   class PipelineStepOut(BaseModel):
       step: Literal["transcription","diarization","speaker_id","llm"]
       enabled: bool
       where: Literal["server","cloud"] | None
       engine: str                  # stable id for i18n: local_gpu|local_cpu|cloud|asr|local|pyannote_ai|asr_config|off|...
       provider: str | None         # "local"|"native"|"pyannote"|"pyannote.ai"|vendor id
       provider_display: str | None # pre-formatted display name from the catalog (fat backend)
       model: str | None
       returns_speakers: bool | None          # transcription step only
       timing: Literal["word","segment","none"] | None   # transcription step: badge 'Word timing / Segment timing / No timing'
       adapter_billing: bool        # diarization via ASRDiarizationAdapter: 'Speakers via adapter (extra billing)'
       speaker_hints: list[Literal["min","max","num"]]
       audio_leaves_server: bool
       settings_section: str        # deep-link target (alias id), e.g. "asr-provider", "speaker-identification"
   class PipelineView(BaseModel):
       steps: list[PipelineStepOut]            # always 4, in order
       issues: list[PipelineIssueOut]
       deployment_mode: Literal["full","lite"]
       diarization_source_locked: bool
   ```

   The `llm` step: provider from `LLMService` config resolution, `where` from
   `services/redaction/llm_guard.is_local_provider` (`:207`), `enabled =
   LLMService.is_configured_for_user(db, user_id)`. No network calls.

2. `PUT /api/user-settings/transcription`: `diarization_source` accepts `asr_config`; new optional
   field `diarization_asr_config_uuid: UUID | None` (schemas `TranscriptionSettings`,
   `TranscriptionSettingsUpdate` in `backend/app/schemas/transcription_settings.py:118,219`;
   mapping in `user_settings.py:854`). Validation runs `resolve_pipeline_plan` on the would-be
   state and returns **409** with a stable `detail.code` for a NEW selection that produces an
   `error` issue (generalises `_require_pyannote_key_for_new_selection`; keep its "only a change is
   refused" rule so a stale stored value never makes other fields unsaveable). 422 stays for an
   unknown value. `TranscriptionSystemDefaults.valid_diarization_sources` gains `asr_config`.
3. Lock #1109: add `"diarization_asr_config_uuid": DIARIZATION_SOURCE` to
   `TRANSCRIPTION_FIELD_CAPABILITY` (`core/locked_settings.py:30-38`). `asr_config` additionally
   requires capability `asr.user_providers` (mirror of the pyannote key gate in
   `diarization_settings.py`).
4. ASR catalog and capabilities: add `speaker_count_hints` (C5) and `timestamp_granularity` (C7)
   per provider and model; add `supports_diarization`, `speaker_count_hints` and
   `timestamp_granularity` to `get_model_capabilities` (`asr/factory.py:772-831`) so
   `GET /asr-settings/status.active_model_capabilities` carries them (#1219). Model-level
   `supports_diarization` wins over provider-level (OpenAI).
5. Deleting a `UserASRSettings` row referenced as a diarizer, or unsharing it: the plan reports
   `diarizer_config_missing`; additionally `DELETE /asr-settings/config/{uuid}` resets that user's
   `asr_config` source to `provider` (mirror of `diarization_settings.py:220`'s
   `source_reverted`), returning `diarizer_source_reverted: bool`.
6. Per-file overrides: unchanged (only "skip speaker detection"). No per-file engine (decided Q5).
7. OpenAPI: regenerate whatever the repo's frontend type source is (check
   `frontend/src/lib/api/*.ts` are hand-written; they are, so update by hand).
8. Alembic: the two D data migrations (WP3, WP6) and the WP7 provenance column. No schema change
   for the settings themselves (UserSetting is key/value).
9. Task context: `preprocess_context["pipeline_plan"] = plan.to_context()`; downstream tasks read
   `PipelinePlan.from_context`. Re-runs recompute the plan at dispatch (settings may have changed;
   same rule as today, where the source is re-read from the user setting).
10. Lite refusals (WP6), all 409 with stable `detail.code` and a fixed sentence: `PUT
   /user-settings/transcription` (`local_diarization_unavailable`,
   `automatic_needs_speaker_provider`, `asr_no_timing_for_speakers`), `POST /asr-settings/set-active`
   and `/clear-active` (`asr_settings.py:700,759`) when the resulting plan is blocking
   (`local_asr_unavailable` for clear-active on lite, which today silently points at local), the
   super-admin local-model endpoints (`asr_settings.py:887-1026`: `local_asr_unavailable`), and
   `PUT /admin/engine-settings` for `diarizer_backend` (`engine_settings.py:202-214`:
   `local_diarization_unavailable`). `GET /user-settings/transcription/system-defaults` drops
   `local` from `valid_diarization_sources` on lite.
11. Per-file provenance (WP7): `MediaFileDetail` and `/my-files/{uuid}/status` gain
   `processing_steps: list[ProcessingStepOut]` (see WP7).

---

## F. Settings UI

### F1. Placement

New sidebar row **"Processing pipeline"** (section id `processing-pipeline`) at the top of the
Transcription group (`frontend/src/components/SettingsModal.svelte:352-359`), visible when
`transcription.prefs` is on. It is **read-only** (Q4): each step has a "Change" link that deep-
links to the single place that edits it (`settingsModalStore.open('asr-provider')` ->
Transcription > Provider tab; `'speaker-identification'` -> Speakers > Detection tab;
LLM -> its existing section). Aliases in `frontend/src/lib/settings/sectionAliases.ts`, search
entries in `frontend/src/lib/search/settingsSearchIndex.ts`.

### F2. The diagram

`frontend/src/components/settings/PipelineOverview.svelte` (new) renders four step cards joined
by arrows, laid out with logical properties so `ar` mirrors (arrows flip with `dir`):

```
Audio -> [1 Transcription] -> [2 Speaker detection] -> [3 Speaker ID / voiceprints] -> [4 AI features]
```

Each card:
- Title + what it is used for (one line).
- Engine line: provider display name + model.
- Chip **This server** (neutral) or **Cloud: <Vendor>** (accent). Off steps show **Off** and are
  dimmed, not hidden.
- Transcription card: badge **Speakers included** / **Text only** (from `returns_speakers`) and
  timing badge **Word timing** / **Segment timing** / **No timing** (from `timing`).
- Speaker detection card: badge **Speakers via adapter (extra billing)** when `adapter_billing`.
- Lite: a banner line "This deployment has no GPU: transcription and speaker detection use cloud
  providers; voiceprints are computed on this server"; local options never appear as choices,
  and a stored local selection renders its blocking issue with the deep link that fixes it.
- Speaker detection card: hint line "Uses your speaker range" / "Ignores the speaker range".
- Privacy note when `audio_leaves_server`: "Audio is sent to <Vendor>." Two cloud vendors ->
  both named.
- Issues for that step (error red, warning amber, info neutral), text from
  `settings.pipeline.issue.<code>`, each with the deep link that fixes it.

Pure helper `frontend/src/lib/settings/pipelineSteps.ts` maps `PipelineView` -> view models (no
fetches); unit-tested. API client `frontend/src/lib/api/pipeline.ts`. Refresh on
`settingsModalStore` save events of the transcription/ASR/diarization sections.

### F3. Changes to existing cards (#1219 lives here)

- `ASRSettings.svelte` config list and `ASRConfigModal.svelte` model select: badge
  **Speakers included** / **Text only** per provider and per model (`supports_diarization`,
  model-level first), plus the timing badge and info text per provider (e.g. "Returns speaker
  labels with word timing"; "Text only, no timing: speakers cannot be added"). On lite the local
  model card (`ASRSettings.svelte` A1-A3 rows in the settings plan) is hidden, not just disabled.
- `EngineSettings.svelte` (admin): on lite the panel shows a read-only note that local speaker
  detection is unavailable; the sidebar row stays for the boundary-smoothing controls only if
  they still apply (they do: smoothing runs on any diarized transcript, `finalize.py`).
- `SpeakerDetectionProviderCard.svelte`:
  - options per C2, labels re-worded; `asr_config` reveals a select of the user's saved ASR
    configs that return speakers (excluding the active transcription config), plus the
    double-billing + privacy note;
  - `pyannote` keeps `PyannoteCredentialForm`;
  - Automatic + text-only transcription model (decided Q1): full image, timed model -> info
    "Speakers will be detected on this server" (`automatic_uses_server`); no-timing model -> error
    `asr_no_timing_for_speakers`; lite -> error `automatic_needs_speaker_provider` that steers to
    pyannote.ai / another provider; each with a link to the tab that fixes it;
  - on lite the `local` option is not rendered at all;
  - "where" chip under the select;
  - when the source is locked (#1109) the card is read-only and says the deployment chose it.
- `SpeakerDetectionSettings.svelte`: min/max inputs show `speaker_hints_ignored` note.
- `EngineSettings.svelte`: unchanged except copy that names this axis "the engine used when
  speakers are detected on this server".
- Catalog copy fix: local provider `display_name`/`description` "Local (GPU)" / "faster-whisper +
  PyAnnote" (`asr/factory.py:50-60`) -> "This server" / "faster-whisper; speakers by the
  diar-native engine (PyAnnote fallback)". Backend-sent string; make it an i18n key on the
  frontend if the panel renders it.

### F4. i18n (all 12 locales, real translations, `ar` checked RTL)

Keys to ADD (flat keys, `frontend/src/lib/i18n/locales/*.json`), en text:

| Key | en |
|---|---|
| `settings.pipeline.title` | Processing pipeline |
| `settings.pipeline.description` | What happens to a recording, step by step, and where each step runs. |
| `settings.pipeline.step.transcription` | Transcription |
| `settings.pipeline.step.transcriptionPurpose` | Turns speech into text with word timings. |
| `settings.pipeline.step.diarization` | Speaker detection |
| `settings.pipeline.step.diarizationPurpose` | Works out who spoke when. |
| `settings.pipeline.step.speakerId` | Speaker identification |
| `settings.pipeline.step.speakerIdPurpose` | Builds voiceprints to recognise the same person across recordings. |
| `settings.pipeline.step.llm` | AI features |
| `settings.pipeline.step.llmPurpose` | Summaries, chat and speaker name suggestions. |
| `settings.pipeline.where.server` | This server |
| `settings.pipeline.where.cloud` | Cloud: {{vendor}} |
| `settings.pipeline.off` | Off |
| `settings.pipeline.change` | Change |
| `settings.pipeline.speakersIncluded` | Speakers included |
| `settings.pipeline.textOnly` | Text only |
| `settings.pipeline.hintsUsed` | Uses your speaker range |
| `settings.pipeline.hintsIgnored` | Ignores the speaker range |
| `settings.pipeline.audioLeaves` | Audio is sent to {{vendor}}. |
| `settings.pipeline.issue.pyannote_key_missing` | pyannote.ai is selected but no API key is saved. |
| `settings.pipeline.issue.diarizer_config_missing` | The provider chosen for speaker detection no longer exists or is no longer shared with you. |
| `settings.pipeline.issue.diarizer_config_no_speakers` | The provider chosen for speaker detection does not return speakers. |
| `settings.pipeline.issue.local_asr_unavailable` | This deployment cannot transcribe on this server. Choose a cloud provider. |
| `settings.pipeline.issue.automatic_uses_server` | Your transcription model returns text only, so speakers are detected on this server. |
| `settings.pipeline.issue.speaker_hints_ignored` | This speaker detection engine ignores the minimum and maximum speaker count. |
| `settings.pipeline.issue.diarizer_double_billing` | That provider charges for a full transcription even though only its speakers are used. |
| `settings.pipeline.issue.diarizer_same_as_transcription` | This is your transcription provider, so its own speaker detection is used. |
| `settings.pipeline.issue.source_locked` | Your administrator manages speaker detection for this deployment. |
| `settings.speakerIdentification.source.asrConfig` | Another provider's speaker detection |
| `settings.speakerIdentification.source.asrConfigDesc` | Use a saved cloud transcription provider only for its speaker labels. Its text is discarded. |
| `settings.speakerIdentification.source.asrConfigSelect` | Provider for speaker detection |
| `settings.speakerIdentification.source.asrConfigNone` | No saved provider returns speakers. Add one under Transcription > Provider. |
| `settings.asrProvider.speakersIncluded` | Speakers included |
| `settings.asrProvider.textOnly` | Text only |
| `settings.asrProvider.speakersViaAdapter` | Speakers via adapter (extra billing) |
| `settings.asrProvider.timing.word` | Word timing |
| `settings.asrProvider.timing.segment` | Segment timing |
| `settings.asrProvider.timing.none` | No timing |
| `settings.asrProvider.timing.noneInfo` | This model returns text without timestamps, so speakers cannot be added to it. |
| `settings.pipeline.issue.local_diarization_unavailable` | This deployment has no local speaker detection. Choose pyannote.ai or a provider that returns speakers. |
| `settings.pipeline.issue.automatic_needs_speaker_provider` | Your transcription model returns text only and this deployment cannot detect speakers itself. Choose pyannote.ai or another provider for speaker detection. |
| `settings.pipeline.issue.asr_no_timing_for_speakers` | Your transcription model returns no timestamps, so speakers cannot be assigned. Choose a model with word or segment timing, or turn speaker detection off. |
| `settings.pipeline.issue.segment_timing_only` | Your transcription model returns segment timing only, so a speaker change inside a segment is not detected. |
| `settings.pipeline.liteBanner` | This deployment has no GPU: transcription and speaker detection use cloud providers. Voiceprints are computed on this server. |

Keys whose en TEXT changes (key unchanged, all 12 locales): `source.provider`/`providerDesc`
(per decided Q1: "Uses your transcription provider's speaker labels, or detects speakers on
this server when the provider returns text only"), `source.local` -> "This server",
`source.localDesc` (no "GPU" claim; the option is hidden on lite), `source.pyannoteDesc` (drop "when a cloud provider transcribes" in WP3),
`credential.local` (drop "GPU").

---

## G. Work packages

Rules for every WP: worktree under `.claude/worktrees/<name>`, one writer per worktree; red-first
(watch each new test fail on a `git archive HEAD` tree, root `CLAUDE.md`); `scripts/safe-
precommit.sh run --all-files` + `--hook-stage pre-push`; `python3 scripts/audit-tests.py
backend/tests` at 0 open findings; `cd frontend && npm run check:i18n` for locale edits; frontend
changes opened in a browser (light, dark, `ar`) before reporting done.

### WP1 (interim, smallest correct fix): refuse pyannote.ai with local transcription. **S, ~0.5 day**

Decision: **refuse/warn now, implement in WP3.** Implementing needs the post stage (WP3); a
refusal is correct, tiny, and WP3 deletes it.

- `api/endpoints/user_settings.py`: in the PUT, when the NEW `diarization_source == "pyannote"`
  and the user's effective ASR provider is `local` (use
  `ASRProviderFactory.get_active_model_capabilities(user_id, db)["provider"]`), raise 409
  `detail = {"code": "pyannote_requires_cloud_transcription", "message": <fixed sentence>}`.
  Only a change is refused (same rule as `_require_pyannote_key_for_new_selection`).
- `tasks/transcription/core.py` local branch: when `diarization_source == "pyannote"`, log one
  WARNING naming file id and "pyannote.ai is only used with cloud transcription; detected
  speakers locally" (no behaviour change; makes the stale-selection case visible).
- `SpeakerDetectionProviderCard.svelte`: accept `asrIsLocal: boolean` (parent reads
  `/asr-settings/status`); when true, the `pyannote` option is `disabled` and a hint explains why;
  a stored stale `pyannote` stays displayed with the warning text.
- `frontend/src/lib/api/transcriptionSettings.ts`: map the new 409 code to an i18n key.
- Locales (12): add `settings.speakerIdentification.source.pyannoteNeedsCloud` ("Available only
  when a cloud provider transcribes. With this server's model, speakers are detected on this
  server.").
- Tests (red first):
  - `backend/tests/api/test_transcription_settings_pyannote_local_asr.py`: PUT pyannote with no
    active ASR config -> 409 code; with a cloud ASR config + key -> 200; stored stale pyannote +
    PUT of another field -> 200.
  - `frontend/src/components/settings/SpeakerDetectionProviderCard.test.ts`: option disabled +
    hint when `asrIsLocal`.
- Known hole, closed by WP3: switching the active ASR to local AFTER choosing pyannote is not
  refused (`asr_settings.py:700,759`); the runtime WARNING covers it.

### WP2: `PipelinePlan` resolver + `GET /user-settings/pipeline` + capability fields. **M, ~2 days, backend only**

Files: new `backend/app/services/pipeline_plan.py`, new `backend/app/schemas/pipeline.py`, new
`backend/app/api/endpoints/pipeline_settings.py`, `backend/app/api/router.py`,
`backend/app/services/asr/factory.py` (`speaker_count_hints` and `timestamp_granularity` in
catalog; `resolve_active_config`; `get_model_capabilities` gains `supports_diarization`,
`speaker_count_hints`, `timestamp_granularity`), `backend/app/schemas/asr_settings.py` (capabilities typing),
`backend/app/services/diarization/CLAUDE.md` + `backend/app/services/asr/CLAUDE.md` (document the
plan as the one decision point).
No pipeline consumer changes yet (pure addition), so it cannot regress transcription.

Tests (red first):
- `backend/tests/unit/test_pipeline_plan.py`: table-driven over the full C3 matrix (every cell,
  incl. lite rows and every error/warning code), asserting `engine`, `execution`, `queue`,
  `where`, `speaker_hints`, issues. Include must-differ controls (e.g. `pyannote`+local_gpu must
  NOT resolve to `inline_local`), so an "always local" resolver fails.
- `backend/tests/unit/test_asr_catalog_speaker_hints.py`: every provider has
  `speaker_count_hints`; the values equal what each provider module actually sends (assert by
  driving each provider's request builder with a sentinel config, not by re-reading the catalog).
- `backend/tests/api/test_pipeline_settings_endpoint.py`: 4 steps in order; cloud step has
  `audio_leaves_server`; locked source reports `diarization_source_locked`; no network calls
  (monkeypatch requests to raise).
- `backend/tests/api/test_asr_status_capabilities.py`: OpenAI `whisper-1` -> `supports_diarization
  False`, `gpt-4o-transcribe-diarize` -> True.
- `backend/tests/unit/test_asr_catalog_timestamp_granularity.py`: every provider/model has
  `timestamp_granularity`; for each provider, feed its parser a recorded fixture response (the
  provider unit tests already hold these) and assert the parsed `ASRResult` matches the declared
  granularity (words with times present for `word`; segments with `end > start` for `segment`;
  `gpt-4o-transcribe` -> one `0.0/0.0` segment for `none`). A catalog that says `word` for a
  parser that produces none must fail.
- `test_pipeline_plan.py` also covers: `asr_no_timing_for_speakers` for every diarizing column
  with `gpt-4o-transcribe`; `segment_timing_only` for `whisper-1`; every lite row of C3.

### WP3: execute the plan (post stage, local x cloud, CPU path diarizes). **L, ~4 days**

Depends on WP2. Files:
- `backend/app/tasks/transcription/dispatch.py`: compute the plan once (`load_plan_inputs` +
  `resolve_pipeline_plan`); refuse on `plan.blocking()` exactly as `ASRConfigurationError` is
  handled today (`:357-366`: mark ERROR with the fixed sentence, no task record); queue from
  `plan.transcription.queue`; never `gpu-transcribe` unless `execution == "inline_local"`; drop
  `diarization_source="off" if use_cpu` (`:398`).
- `preprocess.py`: carry `pipeline_plan` in the context; delete `:265-278`.
- `core.py`, `pipelines.py`, `diarize_task.py`, `cloud_asr.py`, `cpu_task.py`: read the plan.
  Local engine diarizes only when `execution == "inline_local"`. `cloud_asr.py`:
  `enable_diarization = execution == "inline_asr"`; `parallel_cloud` builds the diarizer via
  `create_for_engine` (pyannote_ai now; asr_config in WP4). `cpu_task.py`: fix the docstring;
  stop forcing one speaker when a post stage will diarize.
- The post stage calls `finalize_segments` after `assign_speakers` (C7; today's
  `rediarize_task.py:336-366` skips it) and merges at `plan.diarization.merge` granularity.
- Voiceprint stage (C6): `plan.speaker_id.execution` decides inline native vs
  `extract_speaker_embeddings_task`; replace the `is_cloud_asr` branching in
  `postprocess.py:217-331` with it.
- `postprocess.py:214-344`: branch on `plan.diarization.execution`: `post_stage_local` and
  `post_stage_cloud` dispatch `rediarize_task(engine=..., pipeline_completion=True,
  pipeline_task_id=task_id)` on the plan's queue and leave the task open (today's
  `needs_local_diarization` branch, generalised); `inline_asr`/`parallel_cloud` -> CPU embeddings
  (today); `inline_local` -> inline embeddings (today); `none` -> complete.
- `tasks/rediarize_task.py`: C4 (engine kwarg, `create_for_engine`, delete `_run_diarization`,
  cloud -> embeddings task).
- `services/diarization/factory.py`: `create_for_engine`; delete `create_for_source`,
  `create_for_user`, duplicate constants; `local_provider.py` becomes live.
- `services/error_categorization_service.py`: register `DiarizerConfigUnusableError`'s sentence
  (and keep `scripts/audit-error-disclosure.py` green).
- `api/endpoints/user_settings.py`: generalised plan-based 409 (E2); delete WP1's refusal.
- Alembic data migration (D).
- Locales: revert WP1's key, update `source.pyannoteDesc`, `source.localDesc`, `credential.local`.
- Docs: `backend/app/services/diarization/CLAUDE.md` ("Only `source == pyannote` reaches this
  factory" is no longer true), `backend/app/tasks/CLAUDE.md` (queue table), `docs-site/docs/
  features/transcription.md` (composition table).

Tests (red first; each must fail on HEAD):
- `backend/tests/unit/test_local_asr_cloud_diarization.py`: local GPU + `pyannote` with key ->
  GPU stage called with `enable_diarization=False`, postprocess dispatches `rediarize_task` with
  `engine="pyannote_ai"` on `cloud-asr`; and the pyannote.ai provider's `diarize` is invoked
  (stub at the HTTP boundary, assert the request was made). On HEAD this fails: the local
  diarizer runs.
- `backend/tests/unit/test_cpu_transcription_diarizes.py`: `whisper_model="tiny"` + `local` ->
  `post_stage_local` dispatched to `gpu_preferred_queue()`.
- `backend/tests/unit/test_rediarize_engine.py`: `engine` in {local, pyannote_ai}: provider built
  through `create_for_engine`; cloud result merged with `assign_speakers`; embeddings task
  dispatched with `pipeline_task_id`; missing key -> file ERROR with the fixed sentence.
- `backend/tests/unit/test_task_session_lifetime.py`: add `rediarize_task` cloud branch (HTTP
  with no open scope, `>= 2` scopes opened).
- `backend/tests/unit/test_lite_mode_queue_consumers.py`: new routes (`cloud-asr` for the post
  stage) have lite consumers.
- `backend/tests/unit/test_gpu_split_skips_post_stage.py`: split enabled + `pyannote` -> queue
  `gpu`, not `gpu-transcribe`.
- Migration test (follow the existing pattern under `backend/tests/` for data migrations; see
  `backend/app/db/CLAUDE.md`): the four user shapes (no key + local -> rewritten; key + local ->
  kept; no key + cloud -> kept; other values -> untouched).
- `backend/tests/unit/test_requested_options.py`: a re-run of a file whose stored source is
  `pyannote` keeps it (no bool->provider mapping left).
- `backend/tests/unit/test_post_stage_resegments.py`: a word-timed transcript whose single
  segment spans two turns comes out of the post stage as two segments (red on HEAD: rediarize
  skips `finalize_segments`).
- `backend/tests/unit/test_voiceprint_stage_every_combination.py`: for each diarizing C3 cell the
  plan yields `inline_native` or `embedding_stage`, and postprocess / the post stage dispatches
  the embedding task exactly once (never zero, never twice).
- Live: `--with-mock-asr` + a stubbed pyannote endpoint is not available; run the unit layer plus
  one real pyannote.ai file only if the owner provides a key (do not bill unasked).

### WP4: other vendors as diarizers + one merge. **M, ~2-3 days**

Depends on WP3 (post stage) for the local-ASR rows; the `parallel_cloud` half could start after
WP2. Files: new `backend/app/services/diarization/asr_adapter.py`;
`services/diarization/factory.py` (`asr_config` branch); `schemas/transcription_settings.py`,
`api/endpoints/user_settings.py`, `core/locked_settings.py` (E2, E3);
`api/endpoints/asr_settings.py` delete endpoint (E5); `tasks/transcription/cloud_asr.py` parallel
path -> `assign_speakers` (via a dict conversion of `ASRResult`, done once in
`_convert_asr_result_to_segments`'s neighbourhood); delete `utils/diarization_merge.py`.
Sub-step: `openai_provider.py` requests `timestamp_granularities=["word"]` for `whisper-1`
(vendor docs say it is supported with `verbose_json`, **unverified**); flip the catalog to `word`
only with a recorded real response fixture proving word times; otherwise leave `segment`.

Tests (red first):
- `backend/tests/unit/test_asr_diarization_adapter.py`: words with speakers -> coalesced turns;
  no words -> segment turns; returned object exposes no text; `enable_diarization=True` and hints
  forwarded; a text-only provider is refused at construction.
- `backend/tests/unit/test_merge_unification_equivalence.py`: for a corpus of fixture
  (ASR words, diarization turns) pairs, `assign_speakers` agrees with the old midpoint merge on
  >= 99% of words (measure on HEAD first and record the number in the test docstring; any
  difference must be explained, e.g. overlap-aware wins at turn edges). Delete the old merge only
  after this passes.
- `backend/tests/api/test_diarizer_asr_config_selection.py`: PUT `asr_config` with a speakers
  config -> 200; with a text-only config -> 409 `diarizer_config_no_speakers`; with an unshared
  config -> 409 `diarizer_config_missing`; locked -> ignored; deleting the config resets the
  source and reports it.
- Integration (`--with-mock-asr`, Gladia stand-in, `backend/tests/integration/
  test_lite_mode_mocked_providers.py` pattern): cloud ASR text-only (mock without speakers) +
  `asr_config` = mock Gladia with speakers -> transcript has speakers, no mock text stored.

### WP5: Settings UI (#1219 + new options + Pipeline view). **L, ~3-4 days, frontend + locales**

Depends on WP2 (API contract); options for `asr_config` need WP4's PUT, warnings need WP3's
semantics. Can be built against WP2 and land last. Files: F1-F4 (new
`PipelineOverview.svelte`, `lib/api/pipeline.ts`, `lib/settings/pipelineSteps.ts`; edits to
`SettingsModal.svelte`, `stores/settingsModalStore.ts` (section id), `lib/settings/
sectionAliases.ts`, `lib/search/settingsSearchIndex.ts`, `ASRSettings.svelte`,
`ASRConfigModal.svelte`, `SpeakerDetectionProviderCard.svelte`, `SpeakerDetectionSettings.svelte`,
`lib/api/asrSettings.ts`, `lib/api/transcriptionSettings.ts`; all 12 locale files).
Close #1219 from this PR ("Closes #1219" on its own line).

Tests (red first):
- `frontend/src/lib/settings/pipelineSteps.test.ts`: every engine/where/issue maps to a key that
  exists in `en.json`; two-vendor privacy note names both.
- `frontend/src/components/settings/PipelineOverview.test.ts`: 4 cards in order, chips, off step
  dimmed, issue links call `settingsModalStore.open` with the right alias.
- `SpeakerDetectionProviderCard.test.ts`: Automatic + text-only warning (#1219); `asr_config`
  sub-select lists only configs that return speakers and excludes the active one.
- `ASRSettings` badge test: whisper-1 "Text only" + "Segment timing", gpt-4o-transcribe "Text
  only" + "No timing", gpt-4o-transcribe-diarize "Speakers included" + "Segment timing"; a config
  chosen as diarizer shows "Speakers via adapter (extra billing)".
- Lite render test: with `deployment_mode: "lite"` no local option/card is rendered anywhere
  (provider tab, speaker card, Pipeline view) and the lite banner shows.
- E2E `backend/tests/e2e/test_settings_pipeline.py` (marker `settings`): open the section,
  screenshot light/dark/`ar` with `DISPLAY=:11` when headed; no persisted changes (cancel path).

### WP6 (#1228): lite is cloud-only for transcription and speaker detection. **M, ~2 days, backend + frontend + locales**

Decided (owner, 2026-10-11): lite runs **no** local transcription and **no** local speaker
detection; both are cloud providers only. Voiceprint embeddings still run locally on CPU (C6),
so the lite image keeps `pyannote.audio`, `torch+cpu`, `onnxruntime` and the diar-native
embedding graph (A7). **No image change** in this package; "lite means no GPU, not small" rules
out stripping anything the voiceprint path needs.

Depends on WP2 (plan) and WP3 (post stage); parallel with WP4/WP7. Work:
1. **Plan** (`services/pipeline_plan.py`): the lite rows of C3; `local_diarization_unavailable`,
   `automatic_needs_speaker_provider`, `asr_no_timing_for_speakers` errors; lite never yields
   `post_stage_local` or `inline_local`.
2. **Dispatch refusal** (`tasks/transcription/dispatch.py`): every blocking lite issue marks the
   file ERROR with the fixed sentence and creates no task record (same shape as
   `ASRConfigurationError` at `:357-366`). Close A7's hazard: the `use_cpu` branch (`:349,375`)
   goes through the plan, so a per-file `tiny` on lite gets `local_asr_unavailable` instead of a
   `ModuleNotFoundError` in `cpu-transcribe`. Upload/reprocess endpoints refuse a lightweight
   `whisper_model` on lite with 409 `local_asr_unavailable` before dispatch.
3. **The existing lite rediarize-on-CPU path** (`postprocess.py:224-265`, comment `:255-259`
   explicitly written for lite; queue via `gpu_preferred_queue()` = `cpu`,
   `core/constants.gpu_preferred_queue`, `core/celery.py:131-145`): **removed for local engines
   on lite, kept for cloud engines.** Justification: under the decision there is no local
   speaker detection on lite, and the plan can no longer produce `post_stage_local` there; the
   same task stays the post stage for `pyannote_ai`/`asr_config` on `cloud-asr` (C4), which lite
   staffs. `rediarize` stays in `GPU_PREFERRED_TASKS` (`celery.py:132`) because that list is about
   the full image's GPU preference; on lite it is reachable only for cloud engines, which route
   explicitly to `cloud-asr`. `rediarize_task(engine="local")` on lite raises the configuration
   error (defence in depth, C4). Rewrite the `:255-259` comment to say so. The embedding tasks in
   `GPU_PREFERRED_TASKS` (`update_speaker_embedding_on_reassignment`, `extract_v4_embeddings*`,
   `speaker.recluster_all`, ...) stay on `cpu` in lite: they are the voiceprint path.
4. **API 409s** (E10): transcription settings PUT, ASR set-active/clear-active, super-admin
   local-model endpoints, engine-settings `diarizer_backend`; `system-defaults` omits `local` on
   lite.
5. **UI** (same PR, all 12 locales): hide every local option on lite (provider tab local model
   card and admin model pin/restart, speaker card `local` option, EngineSettings diarizer
   backend select replaced by a read-only note), Pipeline view lite banner (F2), stored stale
   selections render their blocking issue with a fix link.
6. **Lite data migration** (D): `local` -> `provider` where the active model returns speakers;
   otherwise untouched + visible blocking issue. No transcript/speaker/voiceprint data changes.
7. **Docs**: `docs-site/docs/operations/deployment-configuration.md:30` becomes: "Lite (cloud
   only): no GPU. Transcription and speaker detection are done by cloud providers you configure;
   lite has no local Whisper and no local speaker detection. Voiceprints for cross-file speaker
   identification are still computed on this server's CPU, by the diar-native sidecar's CPU
   build (`--with-diar-native`) or, as a fallback, the in-process WeSpeaker model; that is why
   the lite image still ships `pyannote.audio` and a CPU `torch`." Also
   `docs-site/docs/features/transcription.md:315` (API-Lite paragraph), `docs-site/docs/faq.md:60`
   and `:291`, and `docs-site/docs/features/speaker-diarization.md:287-292` (says "transcription
   remains local" and "falls back to local diarization if the cloud service is unavailable"; both
   are wrong today and after this plan: a vendor failure gives a speakerless transcript, C4).

Tests (red first):
- `backend/tests/unit/test_lite_lightweight_model_route.py`: lite + `whisper_model="tiny"` ->
  dispatch refuses with `local_asr_unavailable`, nothing published to `cpu-transcribe` (red on
  HEAD if A7's hazard is real; if it is already green, record that and keep it as a guard).
- `backend/tests/unit/test_lite_no_local_diarization.py`: lite + cloud ASR + `local` -> dispatch
  refusal, no `rediarize_task(engine="local")` published; lite + `pyannote` -> `rediarize_task`
  on `cloud-asr`; lite + any diarizing source -> embedding task on `cpu` (voiceprints still run).
- `backend/tests/api/test_lite_settings_refusals.py`: every endpoint in E10 returns its 409 code
  on lite and 200 on full (control).
- `backend/tests/unit/test_lite_mode_queue_consumers.py`: unchanged consumer set still covers
  every route (no exemption added).
- Migration test: lite + `local` + speakers model -> `provider`; lite + `local` + text-only ->
  untouched; full -> no-op.
- Frontend: lite render test (WP5's) extended to EngineSettings and the upload model picker.

### WP7 (#1229): per-file provenance on File details and File status. **M-L, ~3 days**

Requirement (owner, 2026-10-11): show which provider/model completed each step for a file:
transcription (provider, model, local/cloud), speaker detection (engine/provider, local/cloud),
voiceprints (embedding backend/model), and the LLM features (summary, topics, speaker-name
suggestions: provider and model), on the file details page and the file status page.

Today (A11): transcription and diarization provider/model columns exist, voiceprints only
`embedding_mode`, summary provider/model inside `summary_data.metadata`, topics and speaker-name
suggestions record nothing; `diarization_provider` is ambiguous between the local PyAnnote engine
and pyannote.ai.

**Data (decided shape: one JSONB column, one write path).**
- New column `media_file.processing_provenance JSONB NULL` (Alembic schema revision, number per
  D; add the detection-guard entry in `backend/app/db/migrations.py` like v433's at `:558-607`;
  model field in `backend/app/models/media.py` beside `asr_provider`). `NULL` = predates
  recording.
- Shape (`schemas/processing_provenance.py`, new; validated on write and read):
  ```json
  {"v": 1, "steps": {
    "transcription": {"provider": "openai", "model": "whisper-1", "where": "cloud",
                       "engine": "cloud", "timing": "segment", "completed_at": "...", "task_id": "..."},
    "diarization":   {"provider": "pyannote.ai", "model": "precision-3", "where": "cloud",
                       "engine": "pyannote_ai", "execution": "post_stage_cloud", "merge": "segment", ...},
    "voiceprints":   {"provider": "native", "model": "pyannote/wespeaker-voxceleb-resnet34-LM",
                       "dims": 256, "where": "server", ...},
    "summary":       {"provider": "vllm", "model": "gemma-4-e4b", "where": "server", ...},
    "topics":        {...}, "speaker_names": {...}
  }}
  ```
  Provider ids are canonical and unambiguous: the local engines report `native` / `pyannote`,
  the cloud diarizer reports `pyannote.ai` (change `diarization/pyannote_provider.py:220`;
  `local_provider.py:119`'s `local` becomes the engine actually used). `where` for LLM steps
  comes from `redaction/llm_guard.is_local_provider` (`:207`). Never stored: config UUIDs, keys,
  base URLs (a private LLM endpoint URL must not reach a shared viewer).
- One writer: `backend/app/services/processing_provenance.py` (new) `record_step(db, file_id,
  step, entry)` issuing a single atomic statement
  `UPDATE media_file SET processing_provenance = jsonb_set(coalesce(processing_provenance,
  '{"v":1,"steps":{}}'::jsonb), ARRAY['steps', :step], CAST(:entry AS jsonb), true) WHERE id =
  :id` (summary, topics and speaker-name tasks run concurrently; no read-modify-write). The
  existing scalar columns (`asr_provider`, `asr_model`, `whisper_model`, `diarization_provider`,
  `diarization_model`, `embedding_mode`) stay as the queryable index (read in 19+ backend
  modules), but `storage.py:201-216`'s direct assignments move into `record_step`, so there is
  one write path for both.
- Hook sites: transcription + diarization at `storage.py:201-216` (save) and at the post stage's
  save in `rediarize_task`; voiceprints in `speaker_embedding_task._match_and_finalize_embeddings`
  (`:213`) and the inline native path (`postprocess.py:301-310`); summary in
  `tasks/summarization.py` after `generate_summary` (`:540-566`, from `llm_service.config`);
  topics in `tasks/topic_extraction.py`; speaker names in `tasks/speaker_identification_task.py`.
  Each hook writes in its existing short write phase (session-lifetime rule).
- **Early vs late hooks**: summary, topics, speaker names, voiceprints and the transcription half
  do not depend on the plan and can land right after WP2 (or independently). The diarization
  entry's `engine`/`execution`/`merge` and the post-stage hook need WP3's plan and post stage.

**Exposure (fat backend).** `build_processing_steps(media_file) -> list[ProcessingStepOut]` in the
same service, used by `MediaFileDetail` (`schemas/media.py:722`, served by
`api/endpoints/files/__init__.py:509`) and by `GET /my-files/{uuid}/status`
(`api/endpoints/user_files.py:253-370`). `ProcessingStepOut`: `step`, `status`
(`done` | `off` | `not_run` | `not_recorded`), `provider_display` (pre-formatted from the
catalogs), `model`, `where` (`server` | `cloud` | null), `recorded` (`full` | `legacy` | `none`).
Historical files (`processing_provenance IS NULL`): transcription/diarization/voiceprints are
composed from the legacy columns with `recorded: "legacy"`; `where` only when unambiguous
(`asr_provider == "local"` -> server, a vendor id -> cloud, `diarization_provider == "native"` ->
server, `"pyannote"` -> null "not recorded" because local PyAnnote and pyannote.ai are
indistinguishable); summary from `summary_data.metadata.provider/model`; topics and speaker
names -> `not_recorded`.

**Permissions / redaction.** Provenance is not transcript content: no masking. It travels inside
the file responses, so it inherits the file read gate, sharing and quarantine
(`takedown_service.exclude_quarantined`) unchanged. No URLs, keys or config ids (above).

**UI.** New `frontend/src/components/ProcessingProvenance.svelte` (one row per step: step name,
provider + model, This server / Cloud chip, "Not recorded" for historical gaps), used in:
- file details: replace the models block in `frontend/src/components/MetadataDisplay.svelte:239-275`
  (delete it, including the hardcoded "Whisper:" label and the `native`-else-PyAnnote ternary,
  A11), rendered on `frontend/src/routes/files/[id]/+page.svelte`;
- file status: `frontend/src/components/fileStatus/FileDetailModal.svelte` (data from
  `UserFileStatus.svelte:235`'s `/my-files/{id}/status`; children do not fetch, per
  `fileStatus/CLAUDE.md`).
Light/dark parity, `ar` RTL, 12 locales. Keys: `provenance.title` ("How this file was
processed"), `provenance.step.transcription|diarization|voiceprints|summary|topics|speakerNames`,
`provenance.server` ("This server"), `provenance.cloud` ("Cloud: {{vendor}}"), `provenance.off`
("Off"), `provenance.notRun` ("Not run"), `provenance.notRecorded` ("Not recorded (processed
before this was tracked)").

Tests (red first):
- `backend/tests/unit/test_processing_provenance_record.py`: concurrent `record_step` for three
  steps from three sessions leaves all three (red against a read-modify-write implementation);
  entry rejects `base_url`/`api_key` fields.
- `backend/tests/unit/test_processing_provenance_build.py`: historical row -> legacy/none mapping
  incl. ambiguous `pyannote` -> `where: null`; new row -> full.
- `backend/tests/unit/test_diarization_provider_ids.py`: pyannote.ai reports `pyannote.ai`, local
  engines report `native`/`pyannote` (red on HEAD: both say `pyannote`).
- `backend/tests/api/test_file_detail_processing_steps.py` and
  `backend/tests/api/test_my_files_status_processing_steps.py`: the six steps present, shared
  viewer sees them, quarantined file still 404s for non-admins.
- Per-hook tests: each task writes its step (summary/topics/speaker names with a mock LLM,
  `--with-mock-llm` fixtures; voiceprints; transcription; diarization incl. post stage).
- Frontend: `ProcessingProvenance.test.ts` (chips, not-recorded text, RTL class), and
  `MetadataDisplay` test asserting the old hardcoded "Whisper:" label is gone.
- E2E (`backend/tests/e2e/`, read-only): open a file's details and the file-status modal, assert
  the provenance rows; screenshots light/dark/`ar`.

### WP8 (#1230): docs-site architecture diagrams and pages describe what shipped. **M, ~1.5 days, LAST**

Runs after WP1-WP7 are merged so it documents the shipped flow, not the plan.
- Specs (`docs-site/architecture-diagrams/specs/`, hand-authored Archify JSON, README there):
  `transcription-pipeline.workflow.json` (plan resolution at dispatch, inline vs post-stage
  diarization, voiceprint stage), `cloud-asr.workflow.json` (parallel_cloud with any diarizer,
  adapter, timing-aware merge, refusal of no-timing models), `transcription-enrichment.workflow.json`
  (provenance recording per step), `speaker-clustering.workflow.json` (embedding stage after every
  source), `celery-queues.architecture.json` (post stage on `cloud-asr`/`gpu`; lite consumers),
  `system-overview.architecture.json` (lite cloud-only boundary). Every node cites real code
  (as the README requires), re-verified on the merged tree.
- Regenerate with `scripts/generate-architecture-diagrams.sh` (requires the Archify skill:
  `npx skills add tt-a1i/archify -g`); commit `docs-site/static/architecture/*.html`. Never
  hand-edit the HTML.
- **Drift check (none exists today**: no hit for `architecture` in `.pre-commit-config.yaml` or
  `.github/workflows/*.yml`). Add: the generator appends `<!-- spec-sha256: <hash> -->` to each
  rendered HTML; new `scripts/check-architecture-diagram-drift.py` (stdlib only, so CI can run it
  without Archify) fails when a spec's hash differs from its HTML's comment or a spec has no
  HTML; wire it as a pre-commit hook (`files: ^docs-site/(architecture-diagrams/specs|static/architecture)/`)
  and it then runs in CI's existing pre-commit job. Self-test: a must-fire case (edited spec) and
  a must-stay-clean case.
- Pages: `docs-site/docs/developer-guide/architecture.md`, `docs-site/docs/features/
  speaker-diarization.md` (composition matrix, adapter billing, timing rules, voiceprints always
  local), `docs-site/docs/features/transcription.md` (provider table with Speakers / Timing
  columns generated from the catalog facts in this plan), `docs-site/docs/operations/
  deployment-configuration.md` (lite row, already fixed by WP6, re-checked), a "Processing
  pipeline" user-guide page with screenshots (use the `docs-screenshots` skill), and
  `docs-site/src/pages/architecture.tsx` only if a diagram is added or renamed.
- Verification: `cd docs-site && npm run build`; the drift check; open `/architecture` locally.

### G1. Order, parallelism, collisions

```
WP1 ─► WP2 ─┬─► WP3 ─┬─► WP4 (local-ASR rows; parallel_cloud half may start after WP2) ─┐
            │        ├─► WP6 (lite cloud-only) ──────────────────────────────────────────┤
            │        └─► WP7 late hooks (diarization/post stage) ────────────────────────┤
            ├─► WP7 early hooks + column + UI (after WP2, parallel with WP3) ────────────┤
            └─► WP5 (UI on WP2's API; rebases after WP3, WP4, WP6) ──────────────────────┴─► WP8 (last)
```

- WP1 first, alone (stopgap). WP2, then WP3 (consumes WP2's types).
- After WP3: WP4, WP6 and WP7-late in parallel worktrees. WP7-early (column, record_step, LLM and
  voiceprint hooks, UI) can start right after WP2.
- WP5 is built against WP2's contract and merges after WP3/WP4/WP6 (it renders their options and
  lite rules). WP8 strictly last.
- **Locale JSON collisions** (all 12 `frontend/src/lib/i18n/locales/*.json`): WP1, WP3, WP5, WP6,
  WP7 (and WP8 only if it adds UI copy, it should not). Merge order: **WP1 -> WP3 -> WP6 -> WP7 ->
  WP5**; never two of them with unmerged locale edits at the same time; each rebases and re-runs
  `npm run check:i18n` before merge.
- Backend collisions: WP3/WP4/WP6 all edit `services/pipeline_plan.py`, `dispatch.py`,
  `postprocess.py`, `rediarize_task.py`, `user_settings.py`; WP4 and WP6 rebase on WP3, the
  second of them on the first. WP7 touches `storage.py`, `rediarize_task.py` (post-stage hook)
  and `models/media.py`; its late hooks rebase on WP3.
- Alembic: WP3 (data), WP6 (data), WP7 (schema) take numbers in merge order (D).

---

## H. Effort, risk, decisions

Effort: WP1 0.5 d, WP2 2 d, WP3 4 d, WP4 2-3 d, WP5 3-4 d, WP6 2 d, WP7 3 d, WP8 1.5 d. Total
about 18-20 implementer days (~4 weeks serial); with WP4/WP6/WP7 in parallel worktrees about 3
weeks elapsed. All in v0.6.0 (due 2026-10-31), which is tight: WP3 and WP5 are the critical path.

Risks:
- **Billing surprise**: users with a pyannote.ai key and local ASR start being billed after WP3
  (decided: honour). Mitigation: CHANGELOG "Changed" + the Pipeline view chip.
- **Timestamp-less providers** (`gpt-4o-transcribe`, `gpt-4o-mini-transcribe`): any diarization
  is refused; users who had Automatic + these models today got a speakerless transcript silently,
  after WP3 they get a settings error until they pick a timed model or turn speakers off. Mitigate
  with the badge and a CHANGELOG note. Azure's real granularity is unverified (A10): treated as
  `segment` until proven.
- **Segment-only merge quality** (`whisper-1`, `gpt-4o-transcribe-diarize`, Azure): turn changes
  inside a segment are lost; the `segment_timing_only` warning makes it visible.
- **Lite cloud-only migration**: lite users on `local` with a text-only model are blocked until
  they choose a cloud diarizer; the blocking issue and the dispatch error name the fix. Their
  existing transcripts and voiceprints are untouched.
- **Post stage skipped `finalize_segments` today** (C7): WP3 adds it; transcripts re-split
  differently from today's cloud+local output. Covered by `test_post_stage_resegments.py`.
- **Latency** of post stages (sequential ASR then cloud diarization): record per-stage timings via
  `benchmark_timing` on one real file.
- **Metering double count**: exactly one terminus per run; extend the rediarize metering tests.
- **Dead routes on lite** (#865 shape): `test_lite_mode_queue_consumers.py`.
- **Session leaks** in the cloud post stage and the provenance hooks: `audit-session-lifetime.py`.
- **Merge regression** when unifying on `assign_speakers`: the WP4 equivalence test.
- **Concurrent provenance writes**: the atomic `jsonb_set` statement and its concurrency test.
- **Three Alembic revisions from parallel worktrees**: numbers taken in merge order (D).

Decided items (owner 2026-10-11 unless marked "adopted", meaning the planner default stands with
no owner objection; these are no longer open):

| # | Decision |
|---|---|
| Q0 | v0.6.0 includes ALL work packages WP1-WP8 (new subsystems allowed in 0.6.0). |
| Q1 | Automatic with a text-only model detects speakers on this server, on the full image, when the model has word or segment timing (C7). No timing -> refused. Lite -> refused and steered to pyannote.ai / another provider. |
| Q2 | Honour a pyannote.ai key + local ASR (CHANGELOG "Changed"). |
| Q3 | The LOCAL engine (native vs in-process PyAnnote) is an ADMIN choice made at setup (`engine.diarizer_backend`), shown read-only to users. |
| Q4 | (adopted) The Pipeline view is read-only with deep links. |
| Q5 | (adopted) No per-file speaker-detection engine; per-file "skip speaker detection" only. |
| Q6 | Lite: NO local transcription and NO local speaker detection; voiceprints stay local on CPU (WP6). Supersedes the earlier "CPU Whisper on lite" recommendation. |
| Q7 | (adopted) No user-level default local model; the per-file lightweight model remains the only `local_cpu` entry point, full image only. |
| Q8 | Other vendors as diarizers: yes, behind `asr.user_providers`, labelled "Speakers via adapter (extra billing)". |
| Q9 | (adopted) Manual rediarize uses the current choice; `asr` -> this server on full, refused on lite. |
| Q10 | (adopted) No admin-configurable default under the #1109 lock in this plan; follow-up issue. |
| Q11 | (adopted) One merge (`assign_speakers`), gated by the equivalence test. |
| Q12 | Vendor voiceprints are never used; one local embedding stage after every source (C6, B2). |

Remaining owner decision (genuinely open, default chosen so nothing blocks): for no-timing models
(`gpt-4o-transcribe`, `gpt-4o-mini-transcribe`), v0.6.0 **refuses** speaker detection. Building a
local forced-alignment stage (wav2vec2 via the installed but unused whisperx, full image only,
C7 option 2) is a follow-up issue; say if it should be scheduled.

Out-of-scope findings to file as issues (per repo rule, fold into the open branch if trivial):
1. Docs-only speaker-count hints not wired for AssemblyAI min/max, Speechmatics, Gladia (C5).
2. Forced alignment for no-timing models (above).
3. Azure: request word-level timestamps explicitly and verify with a live call (A10).
Folded into packages instead of filed: the lite docs line and A7 hazard (WP6),
`speaker-diarization.md:291-292` (WP6/WP8), `preprocess.py:269` trap (WP3), ambiguous
`diarization_provider` and the hardcoded "Whisper:" label (WP7), rediarize skipping
`finalize_segments` (WP3).

## Verification checklist for the implementer

- [ ] Every C3 cell, full and lite, has a passing unit test that failed on HEAD (WP2/WP3/WP4/WP6).
- [ ] The catalog's `timestamp_granularity` is proven per provider by parser fixtures (WP2), and
      the badges render it (WP5).
- [ ] Every diarizing combination dispatches the voiceprint stage exactly once, including lite
      (WP3/WP6).
- [ ] `rg -n '== "off"|== "pyannote"|diarization_source ==' backend/app/tasks` returns only plan
      code after WP3.
- [ ] `rg -n "merge_cloud_diarization|create_for_source|_run_diarization" backend/app` is empty
      after WP4/WP3.
- [ ] On a lite stack (`./opentr.sh start dev --lite --fresh <name>`): no local option visible
      anywhere, every E10 endpoint 409s, a cloud ASR + pyannote.ai (or `--with-mock-asr` adapter)
      file completes with speakers and voiceprints.
- [ ] `./scripts/run-dev-tests.sh --full` green on each WP branch with the stack up via
      `./opentr.sh start dev` (or `--fresh` when other agents are active).
- [ ] One real file per new combination on the dev stack (local GPU + pyannote.ai only with an
      owner-provided key); its file details and file-status modal show the provenance of every
      step (WP7).
- [ ] A file processed before WP7 shows "Not recorded" where nothing was recorded, never a guess.
- [ ] Settings > Processing pipeline, file details and file status opened in a browser in light,
      dark and `ar`; screenshots attached to the PRs.
- [ ] `cd frontend && npm run check:i18n && npm run check && npm run build` pass.
- [ ] WP8: diagrams regenerated with the script, drift check green, `docs-site` builds.
