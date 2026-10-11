"""``GET /user-settings/pipeline``: how the caller's next recording would be processed.

Read-only (issue #1224). The answer comes from ``services/pipeline_plan``, the one
resolver the pipeline itself uses, so the view cannot disagree with what runs. DB reads
only: no provider is built, no vendor or LLM endpoint is contacted.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from sqlalchemy.orm import Session

from app import models
from app.api.endpoints.auth import get_current_active_user
from app.db.base import get_db
from app.schemas.pipeline import PipelineIssueOut
from app.schemas.pipeline import PipelineStepOut
from app.schemas.pipeline import PipelineView
from app.services.asr.factory import ASR_PROVIDER_CATALOG
from app.services.pipeline_plan import PYANNOTE_AI
from app.services.pipeline_plan import PipelinePlan
from app.services.pipeline_plan import resolve_pipeline_plan
from app.services.pipeline_plan_inputs import load_plan_inputs

router = APIRouter()

#: Display names for the engines that are not ASR catalog entries.
_ENGINE_DISPLAY = {
    "native": "diar-native engine",
    "pyannote": "PyAnnote engine",
    PYANNOTE_AI: "pyannote.ai",
}


def _display(provider: str | None) -> str | None:
    if provider is None:
        return None
    if provider in ASR_PROVIDER_CATALOG:
        return str(ASR_PROVIDER_CATALOG[provider]["display_name"])
    return _ENGINE_DISPLAY.get(provider, provider)


def _plan_steps(plan: PipelinePlan) -> list[PipelineStepOut]:
    t, d, s = plan.transcription, plan.diarization, plan.speaker_id
    return [
        PipelineStepOut(
            step="transcription",
            enabled=True,
            where=t.where,
            engine=t.engine,
            provider=t.provider,
            provider_display=_display(t.provider),
            model=t.model,
            returns_speakers=t.returns_speakers,
            timing=t.timing,
            audio_leaves_server=t.where == "cloud",
            settings_section="asr-provider",
        ),
        PipelineStepOut(
            step="diarization",
            enabled=d.engine != "off",
            where=d.where,
            engine=d.engine,
            execution=d.execution,
            provider=d.provider,
            provider_display=_display(d.provider),
            model=d.model,
            adapter_billing=d.engine == "asr_config",
            speaker_hints=sorted(d.speaker_hints),
            audio_leaves_server=d.where == "cloud",
            settings_section="speaker-identification",
        ),
        PipelineStepOut(
            step="speaker_id",
            enabled=s.runs,
            where=s.where if s.runs else None,
            engine=s.execution,
            provider=s.backend if s.runs else None,
            provider_display=_display(s.backend) if s.runs else None,
            model=None,
            audio_leaves_server=False,
            settings_section="speaker-identification",
        ),
    ]


def _llm_step(db: Session, user_id: int) -> PipelineStepOut:
    from app.services.llm_service import LLMService
    from app.services.redaction.llm_guard import is_local_provider

    described = LLMService.describe_for_user(db, user_id)
    if described is None:
        return PipelineStepOut(
            step="llm",
            enabled=False,
            where=None,
            engine="off",
            provider=None,
            provider_display=None,
            model=None,
            audio_leaves_server=False,
            settings_section="llm-provider",
        )
    provider, model, base_url = described
    local = is_local_provider(SimpleNamespace(provider=provider, base_url=base_url))
    return PipelineStepOut(
        step="llm",
        enabled=True,
        where="server" if local else "cloud",
        engine="llm",
        provider=provider,
        provider_display=provider,
        model=model,
        # The LLM receives transcript text, never audio.
        audio_leaves_server=False,
        settings_section="llm-provider",
    )


@router.get("/pipeline", response_model=PipelineView)
def get_pipeline(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> PipelineView:
    """The four processing steps for the caller's next file, with where each one runs."""
    inputs = load_plan_inputs(db, current_user.id, request=request)
    plan = resolve_pipeline_plan(inputs)
    return PipelineView(
        steps=[*_plan_steps(plan), _llm_step(db, current_user.id)],
        issues=[
            PipelineIssueOut(
                code=issue.code, severity=issue.severity, step=issue.step, message=issue.message
            )
            for issue in plan.issues
        ],
        deployment_mode="lite" if inputs.deployment_mode == "lite" else "full",
        diarization_source_locked=inputs.source_locked,
    )
