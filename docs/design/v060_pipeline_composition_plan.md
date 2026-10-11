# Pipeline composition: independently selectable transcription and speaker detection

Status: PLAN ONLY (2026-10-11). Nothing here is implemented. Absorbs issue #1219.
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
| Local CPU transcription + cloud speaker detection (lite-like) | **No** (CPU path forces diarization off; lite has no local ASR at all) |
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

First-class diarization-only providers stay as they are (pyannote.ai,
`PyAnnoteCloudDiarizationProvider`). A new hosted diarization-only vendor would be a new module
in `services/diarization/` plus a factory branch, exactly like pyannote.ai.

Credentials: the adapter is built from a **saved ASR configuration** (`UserASRSettings`) via
`ASRProviderFactory.create_from_db_config(cfg)` (the one decryption path, `asr/CLAUDE.md`). No new
credential store. pyannote.ai keeps its #1204 table.

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

@dataclass(frozen=True)
class SpeakerIdStep:
    runs: bool                 # False when diarization is off
    where: Where               # always "server"
    backend: str               # "native" | "pyannote" (embedding backend, informational)

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
| `local_cpu` | per-file lightweight model (today); user default model if Q7 is accepted; lite once WP6 lands | `cpu-transcribe` | server |
| `cloud` | active ASR config = vendor+model | `cloud-asr` | cloud |

**Speaker detection** (`transcription_diarization_source`, values extended by ONE):

| Stored value | UI label (en) | Engine resolved |
|---|---|---|
| `provider` | Automatic (recommended) | `asr` if the transcription model returns speakers; else see Q1 (recommended: `local`) |
| `local` | This server | `local` (engine = admin `engine.diarizer_backend`, read-only for users) |
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
| **cloud** (text only) | Q1: recommended -> `local` with `info automatic_uses_server`; alternative: no speakers with `warning asr_text_only_automatic` (today's behaviour, #1219) | today | today | **NEW** | today |

Errors (block dispatch AND the settings PUT for a new selection):

| Code | When |
|---|---|
| `pyannote_key_missing` | `pyannote` with no stored key (today's `DiarizationNotConfiguredError`, kept) |
| `diarizer_config_missing` | `asr_config` and the uuid is absent, deleted, or no longer shared with the user |
| `diarizer_config_no_speakers` | `asr_config` and that config's model does not return speakers |
| `local_asr_unavailable` | `local_gpu`/`local_cpu` on lite (today's `ASRConfigurationError`, kept; lifted for `local_cpu` by WP6) |

Warnings/info (never block): `asr_text_only_automatic` or `automatic_uses_server` (Q1),
`speaker_hints_ignored` (engine honours none of the user's hints; native engine included),
`local_diarization_cpu` (lite: local speaker detection runs on CPU, slow), `diarizer_double_billing`
(asr_config), `audio_leaves_server` is not an issue but a per-step `where == "cloud"` flag the UI
renders as the privacy note, `source_locked` (info, #1109).

Topology notes:

- GPU split (`ENGINE_GPU_SPLIT`) only applies to `inline_local`. For any `post_stage_*` the GPU
  task runs the non-split engine path with diarization off; `_resolve_gpu_queue` must not pick
  `gpu-transcribe` for those (it would forward to `diarize_gpu_task`, which diarizes locally).
- `post_stage_cloud` runs on `cloud-asr` (I/O-bound, prefork, concurrency 16), never on the GPU
  worker: holding a GPU slot while polling pyannote.ai is the waste this design avoids. Cost:
  latency = ASR + cloud diarization, sequential. A later optimisation (out of scope) can submit
  the pyannote.ai job from preprocess and poll in the post stage.
- Lite: `post_stage_local` already resolves to `cpu` (`constants.gpu_preferred_queue`, #865);
  `cloud-asr` is staffed on lite (`docker-compose.lite.yml:25-29`). Add both routes to
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
- Embeddings: `local` keeps today's inline native-embedding handling; cloud engines dispatch
  `extract_speaker_embeddings_task` on `cpu` with `pipeline_task_id` (it already closes the
  pipeline task, `speaker_embedding_task.py:_close_pipeline_task`).
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
  plan engine (Q9; `asr` -> `local`).

### C5. Speaker-count hints per engine

Add `speaker_count_hints: list[Literal["min","max","num"]]` to every provider entry (and model
entry where it differs) in `ASR_PROVIDER_CATALOG`, filled from the **code** column of A6, never
from vendor docs. Local: derived from `engine.diarizer_backend` (`native` -> `[]`, `pyannote` ->
all three). pyannote.ai: all three. `resolve_speaker_range` (`tasks/transcription/user_settings.py`)
stays the one precedence for the values; the plan only says which are honoured, so the UI can
show "This engine ignores the speaker range" (`speaker_hints_ignored`). Wiring the
docs-only hints (AssemblyAI min/max, Speechmatics, Gladia) is a follow-up issue, not this plan.

### C6. Speaker identification when diarization is cloud

Unchanged principle: voiceprints are always computed on this server (A9), in the 256-d WeSpeaker
space, from audio + whichever turns won. No vendor voiceprint API (pyannote.ai identify) is used:
it would put voiceprints in a vendor's space, incompatible with the stored index (Q12). Lite:
embeddings on CPU (sidecar CPU EP or in-process). The Pipeline view shows this step as
"This server" always.

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
- **`diarizer_backend` untouched.** It stays the admin's choice of local engine.
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
4. ASR catalog and capabilities: add `speaker_count_hints` (C5); add `supports_diarization` and
   `speaker_count_hints` to `get_model_capabilities` (`asr/factory.py:772-831`) so
   `GET /asr-settings/status.active_model_capabilities` carries them (#1219). Model-level
   `supports_diarization` wins over provider-level (OpenAI).
5. Deleting a `UserASRSettings` row referenced as a diarizer, or unsharing it: the plan reports
   `diarizer_config_missing`; additionally `DELETE /asr-settings/config/{uuid}` resets that user's
   `asr_config` source to `provider` (mirror of `diarization_settings.py:220`'s
   `source_reverted`), returning `diarizer_source_reverted: bool`.
6. Per-file overrides: unchanged (only "skip speaker detection"). No per-file engine (Q5).
7. OpenAPI: regenerate whatever the repo's frontend type source is (check
   `frontend/src/lib/api/*.ts` are hand-written; they are, so update by hand).
8. Alembic: only the D data migration. No schema change (UserSetting is key/value).
9. Task context: `preprocess_context["pipeline_plan"] = plan.to_context()`; downstream tasks read
   `PipelinePlan.from_context`. Re-runs recompute the plan at dispatch (settings may have changed;
   same rule as today, where the source is re-read from the user setting).

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
- Transcription card: badge **Speakers included** / **Text only** (from `returns_speakers`).
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
  model-level first).
- `SpeakerDetectionProviderCard.svelte`:
  - options per C2, labels re-worded; `asr_config` reveals a select of the user's saved ASR
    configs that return speakers (excluding the active transcription config), plus the
    double-billing + privacy note;
  - `pyannote` keeps `PyannoteCredentialForm`;
  - warning when Automatic + text-only transcription model (Q1 decides wording: "Speakers will be
    detected on this server" info vs "No speakers will be detected" warning), with a link to the
    Provider tab;
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
| `settings.pipeline.issue.asr_text_only_automatic` | Your transcription model returns text only, so Automatic detects no speakers. |
| `settings.pipeline.issue.automatic_uses_server` | Your transcription model returns text only, so speakers are detected on this server. |
| `settings.pipeline.issue.speaker_hints_ignored` | This speaker detection engine ignores the minimum and maximum speaker count. |
| `settings.pipeline.issue.local_diarization_cpu` | Speaker detection runs on this server's CPU, which is slower than a GPU. |
| `settings.pipeline.issue.diarizer_double_billing` | That provider charges for a full transcription even though only its speakers are used. |
| `settings.pipeline.issue.diarizer_same_as_transcription` | This is your transcription provider, so its own speaker detection is used. |
| `settings.pipeline.issue.source_locked` | Your administrator manages speaker detection for this deployment. |
| `settings.speakerIdentification.source.asrConfig` | Another provider's speaker detection |
| `settings.speakerIdentification.source.asrConfigDesc` | Use a saved cloud transcription provider only for its speaker labels. Its text is discarded. |
| `settings.speakerIdentification.source.asrConfigSelect` | Provider for speaker detection |
| `settings.speakerIdentification.source.asrConfigNone` | No saved provider returns speakers. Add one under Transcription > Provider. |
| `settings.asrProvider.speakersIncluded` | Speakers included |
| `settings.asrProvider.textOnly` | Text only |

Keys whose en TEXT changes (key unchanged, all 12 locales): `source.provider`/`providerDesc`
(per Q1), `source.local` -> "This server", `source.localDesc` (no "GPU" claim: lite runs it on
CPU), `source.pyannoteDesc` (drop "when a cloud provider transcribes" in WP3),
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
`backend/app/services/asr/factory.py` (`speaker_count_hints` in catalog;
`resolve_active_config`; `get_model_capabilities` gains `supports_diarization` +
`speaker_count_hints`), `backend/app/schemas/asr_settings.py` (capabilities typing),
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
- `ASRSettings` badge test: whisper-1 "Text only", gpt-4o-transcribe-diarize "Speakers included".
- E2E `backend/tests/e2e/test_settings_pipeline.py` (marker `settings`): open the section,
  screenshot light/dark/`ar` with `DISPLAY=:11` when headed; no persisted changes (cancel path).

### WP6: local CPU transcription as a first-class engine (owner-gated, Q6/Q7). **M, ~2 days + image build**

- Q7: `UserSetting["transcription_default_whisper_model"]` (gated by `transcription.model_choice`,
  resolved by `effective_whisper_model`), so a user can stand on `local_cpu`; plan maps
  lightweight -> `local_cpu`.
- Q6 (lite): add `ctranslate2` (CPU wheel) + `faster-whisper` (`--no-deps`, as in
  `requirements-nodeps.txt`) to `backend/requirements-lite.txt`/`Dockerfile.lite`; relax
  `_guard_local_allowed` to allow lightweight models on lite (plan error
  `local_asr_unavailable` stays for GPU models); measure the image delta and record it.
- Fix A7's unverified hazard first with a red test
  (`backend/tests/unit/test_lite_lightweight_model_route.py`).
- Docs: `docs-site/docs/operations/deployment-configuration.md:30` stale PyAnnote claim; lite
  section of `docs-site/docs/features/transcription.md`.
- Locales: only if a model-picker string changes.

### G1. Order, parallelism, collisions

```
WP1 ──► (merged)            WP2 ──┬──► WP3 ──► WP4(local rows) ──┐
                                  ├──► WP4(parallel_cloud half) ─┤
                                  ├──► WP6 (backend, owner-gated)│
                                  └──► WP5 (UI, built on WP2 API; lands last)
```

- WP1 first, alone (it is the stopgap).
- WP2 then WP3 serially (WP3 consumes WP2's types).
- WP4 and WP6 can run in parallel worktrees after WP2; WP4's local-ASR rows need WP3 merged.
- WP5 can be developed in parallel from WP2's contract but must rebase last.
- **Locale JSON collisions**: WP1, WP3, WP5 (and WP6 if it touches copy) all edit all 12
  `frontend/src/lib/i18n/locales/*.json`. Never run two of them concurrently on unmerged
  branches; merge in order WP1 -> WP3 -> WP5. Backend-only collisions: WP3 and WP4 both edit
  `services/diarization/factory.py`, `tasks/transcription/cloud_asr.py`,
  `api/endpoints/user_settings.py`, so WP4 rebases on WP3.

---

## H. Effort, risk, open questions

Effort: WP1 0.5 d, WP2 2 d, WP3 4 d, WP4 2-3 d, WP5 3-4 d, WP6 2 d + image build. Total about
2.5-3 weeks for one implementer; WP4/WP6 parallelism saves ~3 days.

Risks:
- **Billing surprise**: users with a pyannote.ai key and local ASR start being billed after WP3
  (Q2). Mitigation: CHANGELOG + the Pipeline view chip.
- **Latency** of post stages (sequential ASR then cloud diarization). Measured, not assumed:
  record per-stage timings via the existing `benchmark_timing` markers on one real file.
- **Metering double count**: exactly one terminus per run (post stage when present). Covered by
  extending the existing rediarize metering tests.
- **Dead routes on lite** (#865 shape): every new route must be in
  `test_lite_mode_queue_consumers.py`.
- **Session leaks** in the new cloud branch of `rediarize_task` (HTTP inside a session):
  `scripts/audit-session-lifetime.py` gate.
- **Merge regression** when unifying on `assign_speakers`: the equivalence test is the gate.
- **Milestone fit**: v0.6.0's milestone says "No new subsystems" (due 2026-10-31). See Q0.

Open questions (each has a recommendation, so work is not blocked):

| # | Question | Recommendation |
|---|---|---|
| Q0 | Which milestone? | v0.6.0: WP1 + the #1219 part of WP5 (badges + Automatic warning, works on today's semantics). v0.6.x/v0.7.0: WP2-WP4, rest of WP5, WP6. |
| Q1 | Automatic + text-only transcription model: no speakers (today) or detect on this server? | Detect on this server (`automatic_uses_server`, info). "Automatic (recommended)" should not produce a speakerless transcript; no extra audio leaves the server. |
| Q2 | Users with a pyannote.ai key + local ASR: honour after WP3 (they will be billed)? | Honour; CHANGELOG "Changed"; the Pipeline view shows the cloud chip. |
| Q3 | Should users pick the LOCAL engine (native vs in-process PyAnnote)? | No. Keep `engine.diarizer_backend` admin-only; per-user switching would thrash the `ModelManager` singleton's cached diarizer (`model_manager.py:_diarizer_current`). Show it read-only. |
| Q4 | Is the Pipeline view editable? | Read-only with deep links; one place to edit each setting. |
| Q5 | Per-file speaker-detection engine at upload/reprocess? | Not now; keep "skip speaker detection" only. |
| Q6 | Ship local CPU Whisper in the lite image? | Yes (lite means "no GPU", not "small"); measure and record the image delta. |
| Q7 | A user-level default local model (so CPU can be a standing choice)? | Yes, gated by `transcription.model_choice`. |
| Q8 | Allow another vendor as diarizer despite double billing? | Yes behind `asr.user_providers`, with the billing + privacy note on selection. |
| Q9 | Manual "rediarize" uses the user's current speaker-detection choice? | Yes; `asr` falls back to this server (re-running a cloud ASR just for speakers would re-bill). |
| Q10 | Admin-configurable deployment default for speaker detection when locked (#1109)? | Defer; the lock keeps meaning `provider`. File an issue. |
| Q11 | Unify merges on `assign_speakers`? | Yes, gated by the equivalence test (WP4). |
| Q12 | Use vendor voiceprints (pyannote.ai identify)? | No; voiceprints stay local in the WeSpeaker 256-d space. |

Out-of-scope findings to file as issues (per repo rule, fold into the open branch if trivial):
1. `docs-site/docs/operations/deployment-configuration.md:30` contradicts the code about
   in-process PyAnnote on lite (folded into WP6).
2. Possible lite failure for per-file lightweight models (A7; red test first, WP6).
3. Docs-only speaker-count hints not wired for AssemblyAI min/max, Speechmatics, Gladia (C5).
4. `preprocess.py:269`'s `False -> provider` mapping is a latent trap until WP3 deletes it.

## Verification checklist for the implementer

- [ ] Every C3 cell has a passing unit test that failed on HEAD (WP2/WP3/WP4).
- [ ] `rg -n '== "off"|== "pyannote"|diarization_source ==' backend/app/tasks` returns only plan
      code after WP3.
- [ ] `rg -n "merge_cloud_diarization|create_for_source|_run_diarization" backend/app` is empty
      after WP4/WP3.
- [ ] `./scripts/run-dev-tests.sh --full` green on the WP branch with the stack up via
      `./opentr.sh start dev` (or `--fresh` when other agents are active).
- [ ] One real file per new combination run on the dev stack (local GPU + pyannote.ai only with
      an owner-provided key), Processing details show the diarization provider that ran.
- [ ] Settings > Processing pipeline opened in a browser in light, dark and `ar`; screenshots
      attached to the PR.
- [ ] `cd frontend && npm run check:i18n` and `npm run check` and `npm run build` pass.
