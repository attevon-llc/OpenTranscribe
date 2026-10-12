"""Lite is cloud-only for transcription and speaker detection (issue #1228).

A ``DEPLOYMENT_MODE=lite`` deployment has no GPU and ships no local Whisper; by the owner's
decision (2026-10-11) it runs no local speaker detection either. Voiceprints are still
computed on this server's CPU. Every API surface that offers a local option answers it with
the same 409 the pipeline plan uses: ``detail = {"code", "message"}``, a stable code and its
fixed sentence from ``services/pipeline_plan.ISSUE_MESSAGES``, never exception text.
"""

from __future__ import annotations

from fastapi import HTTPException
from fastapi import status


def is_lite_deployment() -> bool:
    """Whether this deployment is lite (read at call time, like the plan's own input)."""
    from app.core.config import settings

    return str(settings.DEPLOYMENT_MODE).strip().lower() == "lite"


def plan_refusal(code: str) -> HTTPException:
    """The 409 for a pipeline-plan issue code: ``{"code", "message"}``."""
    from app.services.pipeline_plan import ISSUE_MESSAGES

    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": code, "message": ISSUE_MESSAGES[code]},
    )


def refuse_on_lite(code: str) -> None:
    """Raise :func:`plan_refusal` for ``code`` when this deployment is lite."""
    if is_lite_deployment():
        raise plan_refusal(code)
