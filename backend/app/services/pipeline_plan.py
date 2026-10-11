"""The one decision point for how a recording is processed (issue #1224).

Transcription and speaker detection are chosen independently, each local or cloud. Which
combinations are valid, what runs where, on which queue, and how speaker turns reach the
text is decided HERE, by one pure function over plain inputs, and nowhere else.
``services/pipeline_plan_inputs.load_plan_inputs`` gathers those inputs from the DB.

Owner decisions this encodes (``docs/design/v060_pipeline_composition_plan.md`` C3, H):

- lite runs no local transcription and no local speaker detection;
- "Automatic" with a text-only model detects speakers on this server (full image only);
- no forced alignment: a model whose text has no times cannot receive speakers;
- voiceprints are always computed on this server, after whichever detector ran.

The module imports nothing but constants, so a test can drive every cell of the matrix
without a database.
"""

from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from typing import Literal

from app.core.constants import CeleryQueues
from app.core.constants import gpu_preferred_queue

TranscriptionEngine = Literal["local_gpu", "local_cpu", "cloud"]
DiarizationEngine = Literal["asr", "local", "pyannote_ai", "asr_config", "off"]
Execution = Literal[
    "inline_asr",  # the cloud ASR call returns the speakers itself
    "inline_local",  # the local engine diarizes inside the GPU task
    "parallel_cloud",  # cloud ASR + a cloud diarizer in one cloud-asr task
    "post_stage_local",  # rediarize after transcription, on this server
    "post_stage_cloud",  # rediarize after transcription, by a cloud diarizer
    "none",
]
Where = Literal["server", "cloud"]
Timing = Literal["word", "segment", "none"]
Severity = Literal["error", "warning", "info"]
Step = Literal["transcription", "diarization", "speaker_id"]
SpeakerHint = Literal["min", "max", "num"]

ALL_HINTS: frozenset[SpeakerHint] = frozenset({"min", "max", "num"})
PYANNOTE_AI = "pyannote.ai"

#: The fixed user-facing sentence for every issue code. Nothing else (no exception text,
#: no vendor message) is ever put on the wire beside a code.
ISSUE_MESSAGES: dict[str, str] = {
    "pyannote_key_missing": (
        "pyannote.ai speaker detection needs a pyannote.ai API key. Save your key first, "
        "then choose this option."
    ),
    "diarizer_config_missing": (
        "The provider chosen for speaker detection no longer exists or is no longer "
        "shared with you."
    ),
    "diarizer_config_no_speakers": (
        "The provider chosen for speaker detection does not return speakers."
    ),
    "local_asr_unavailable": (
        "This deployment cannot transcribe on this server. Choose a cloud provider."
    ),
    "local_diarization_unavailable": (
        "This deployment has no local speaker detection. Choose pyannote.ai or a provider "
        "that returns speakers."
    ),
    "automatic_needs_speaker_provider": (
        "Your transcription model returns text only and this deployment cannot detect "
        "speakers itself. Choose pyannote.ai or another provider for speaker detection."
    ),
    "asr_no_timing_for_speakers": (
        "Your transcription model returns no timestamps, so speakers cannot be assigned. "
        "Choose a model with word or segment timing, or turn speaker detection off."
    ),
    "automatic_uses_server": (
        "Your transcription model returns text only, so speakers are detected on this server."
    ),
    "segment_timing_only": (
        "Your transcription model returns segment timing only, so a speaker change inside "
        "a segment is not detected."
    ),
    "speaker_hints_ignored": (
        "This speaker detection engine ignores the minimum and maximum speaker count."
    ),
    "diarizer_double_billing": (
        "That provider charges for a full transcription even though only its speakers are used."
    ),
    "diarizer_same_as_transcription": (
        "This is your transcription provider, so its own speaker detection is used."
    ),
    "source_locked": "Your administrator manages speaker detection for this deployment.",
}


@dataclass(frozen=True)
class PlanIssue:
    code: str  # stable; also the i18n key suffix settings.pipeline.issue.<code>
    severity: Severity
    step: Step

    @property
    def message(self) -> str:
        return ISSUE_MESSAGES[self.code]


@dataclass(frozen=True)
class DiarizerConfigInfo:
    """A saved ASR configuration named as the speaker detector (``asr_config``)."""

    provider: str
    model: str
    uuid: str
    returns_speakers: bool
    speaker_hints: frozenset[SpeakerHint] = frozenset()


@dataclass(frozen=True)
class PlanInputs:
    deployment_mode: str  # "full" | "lite"
    asr_provider: str  # "local" or a vendor id, resolved like create_for_user
    asr_model: str | None
    asr_config_uuid: str | None
    asr_returns_speakers: bool
    asr_speaker_hints: frozenset[SpeakerHint]
    asr_timing: Timing
    diarization_source: str  # after the #1109 lock and a per-file "skip"
    source_locked: bool = False
    diarizer_backend: str = "native"  # engine.diarizer_backend (admin choice)
    has_pyannote_key: bool = False
    pyannote_model: str | None = None
    diarization_config: DiarizerConfigInfo | None = None  # asr_config only
    cpu_whisper_model: str | None = None  # per-file lightweight model -> local_cpu
    gpu_split: bool = False


@dataclass(frozen=True)
class TranscriptionStep:
    engine: TranscriptionEngine
    provider: str
    model: str | None
    where: Where
    queue: str
    returns_speakers: bool
    timing: Timing
    config_uuid: str | None


@dataclass(frozen=True)
class DiarizationStep:
    source: str  # the stored value, unchanged
    engine: DiarizationEngine
    provider: str | None
    model: str | None
    where: Where | None
    execution: Execution
    queue: str | None
    speaker_hints: frozenset[SpeakerHint]
    config_uuid: str | None
    merge: Timing


@dataclass(frozen=True)
class SpeakerIdStep:
    runs: bool
    where: Where
    backend: str
    execution: Literal["inline_native", "embedding_stage", "none"]
    queue: str | None


@dataclass(frozen=True)
class PipelinePlan:
    transcription: TranscriptionStep
    diarization: DiarizationStep
    speaker_id: SpeakerIdStep
    issues: tuple[PlanIssue, ...] = field(default_factory=tuple)

    def blocking(self) -> tuple[PlanIssue, ...]:
        return tuple(i for i in self.issues if i.severity == "error")

    def to_context(self) -> dict:
        """JSON-safe form, carried between pipeline stages."""
        data = asdict(self)
        data["diarization"]["speaker_hints"] = sorted(self.diarization.speaker_hints)
        return data

    @classmethod
    def from_context(cls, data: dict) -> PipelinePlan:
        diar = dict(data["diarization"])
        diar["speaker_hints"] = frozenset(diar["speaker_hints"])
        return cls(
            transcription=TranscriptionStep(**data["transcription"]),
            diarization=DiarizationStep(**diar),
            speaker_id=SpeakerIdStep(**data["speaker_id"]),
            issues=tuple(PlanIssue(**i) for i in data["issues"]),
        )


def _local_hints(diarizer_backend: str) -> frozenset[SpeakerHint]:
    # The diar-native engine runs community-1 auto speaker counting and drops the hints.
    return ALL_HINTS if diarizer_backend == "pyannote" else frozenset()


def _diarization_engine(i: PlanInputs, local_asr: bool, add) -> DiarizationEngine:
    """Which detector runs for the stored source, recording why when it is not usable."""
    lite = i.deployment_mode == "lite"
    source = i.diarization_source
    if source == "off":
        return "off"
    if source == "provider":
        if local_asr or i.asr_returns_speakers:
            return "local" if local_asr else "asr"
        if lite:
            add("automatic_needs_speaker_provider", "error")
        else:
            add("automatic_uses_server", "info")
        return "local"
    if source == "local":
        if lite and not local_asr:
            add("local_diarization_unavailable", "error")
        return "local"
    if source == "pyannote":
        if not i.has_pyannote_key:
            add("pyannote_key_missing", "error")
        return "pyannote_ai"
    if source != "asr_config":
        raise ValueError(f"unknown diarization source {source!r}")
    cfg = i.diarization_config
    if cfg is None:
        add("diarizer_config_missing", "error")
    elif not cfg.returns_speakers:
        add("diarizer_config_no_speakers", "error")
    elif not local_asr and (cfg.provider, cfg.model) == (i.asr_provider, i.asr_model):
        add("diarizer_same_as_transcription", "info")
        return "asr"
    else:
        add("diarizer_double_billing", "warning")
    return "asr_config"


def _execution(engine: DiarizationEngine, transcription: TranscriptionEngine) -> Execution:
    if engine == "off":
        return "none"
    if engine == "asr":
        return "inline_asr"
    if engine == "local":
        return "inline_local" if transcription == "local_gpu" else "post_stage_local"
    return "parallel_cloud" if transcription == "cloud" else "post_stage_cloud"


def _diarizer_identity(
    i: PlanInputs, engine: DiarizationEngine
) -> tuple[str | None, str | None, frozenset[SpeakerHint]]:
    """``(provider, model, honoured speaker hints)`` of the detector that runs."""
    if engine == "asr":
        return i.asr_provider, i.asr_model, i.asr_speaker_hints
    if engine == "local":
        return i.diarizer_backend, None, _local_hints(i.diarizer_backend)
    if engine == "pyannote_ai":
        return PYANNOTE_AI, i.pyannote_model, ALL_HINTS
    cfg = i.diarization_config
    if engine == "asr_config" and cfg is not None:
        return cfg.provider, cfg.model, cfg.speaker_hints
    return None, None, frozenset()


def _queues(
    i: PlanInputs, transcription: TranscriptionEngine, execution: Execution
) -> tuple[str, str | None]:
    """``(transcription queue, diarization queue)``."""
    if execution in ("inline_asr", "parallel_cloud", "post_stage_cloud"):
        d_queue: str | None = CeleryQueues.CLOUD_ASR
    elif execution == "inline_local":
        d_queue = CeleryQueues.GPU_DIARIZE if i.gpu_split else CeleryQueues.GPU
    elif execution == "post_stage_local":
        d_queue = gpu_preferred_queue(i.deployment_mode)
    else:
        d_queue = None

    if transcription == "local_cpu":
        return CeleryQueues.CPU_TRANSCRIBE, d_queue
    if transcription == "cloud":
        return CeleryQueues.CLOUD_ASR, d_queue
    # The split topology only diarizes inline; a post stage must not be routed into the
    # transcribe-only worker, which forwards to the local diarizer.
    split = i.gpu_split and not execution.startswith("post_stage")
    return (CeleryQueues.GPU_TRANSCRIBE if split else CeleryQueues.GPU), d_queue


def resolve_pipeline_plan(i: PlanInputs) -> PipelinePlan:
    """Every combination's plan, valid or not; ``blocking()`` names what refuses it."""
    issues: list[PlanIssue] = []

    def add(code: str, severity: Severity, step: Step = "diarization") -> None:
        issues.append(PlanIssue(code, severity, step))

    lite = i.deployment_mode == "lite"
    local_asr = i.asr_provider == "local" or i.cpu_whisper_model is not None
    t_engine: TranscriptionEngine = (
        "local_cpu" if i.cpu_whisper_model else ("local_gpu" if local_asr else "cloud")
    )
    if local_asr and lite:
        add("local_asr_unavailable", "error", "transcription")
    timing: Timing = "word" if local_asr else i.asr_timing

    d_engine = _diarization_engine(i, local_asr, add)
    execution = _execution(d_engine, t_engine)
    diarizing = d_engine != "off"
    if diarizing and timing == "none":
        add("asr_no_timing_for_speakers", "error")
    if diarizing and timing == "segment" and execution != "inline_asr":
        add("segment_timing_only", "warning")
    if i.source_locked:
        add("source_locked", "info")

    cfg = i.diarization_config if d_engine == "asr_config" else None
    provider, model, hints = _diarizer_identity(i, d_engine)
    if diarizing and not hints:
        add("speaker_hints_ignored", "warning")
    t_queue, d_queue = _queues(i, t_engine, execution)

    if not diarizing:
        id_execution: Literal["inline_native", "embedding_stage", "none"] = "none"
    elif execution == "inline_local":
        id_execution = "inline_native"
    else:
        id_execution = "embedding_stage"

    return PipelinePlan(
        transcription=TranscriptionStep(
            engine=t_engine,
            provider="local" if local_asr else i.asr_provider,
            model=i.cpu_whisper_model or i.asr_model,
            where="server" if local_asr else "cloud",
            queue=t_queue,
            returns_speakers=local_asr or i.asr_returns_speakers,
            timing=timing,
            config_uuid=i.asr_config_uuid,
        ),
        diarization=DiarizationStep(
            source=i.diarization_source,
            engine=d_engine,
            provider=provider,
            model=model,
            where=None if not diarizing else ("server" if d_engine == "local" else "cloud"),
            execution=execution,
            queue=d_queue,
            speaker_hints=hints,
            config_uuid=cfg.uuid if cfg is not None else None,
            merge=timing if diarizing else "none",
        ),
        speaker_id=SpeakerIdStep(
            runs=diarizing,
            where="server",
            backend=i.diarizer_backend,
            execution=id_execution,
            queue=CeleryQueues.CPU if id_execution == "embedding_stage" else None,
        ),
        issues=tuple(issues),
    )
