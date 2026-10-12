"""Re-plan the pipeline when the active transcription configuration changes (issue #1225).

``PUT /user-settings/transcription`` refuses a NEW speaker-detection choice the pipeline
plan cannot run. The ASR side can break the same combination from the other direction:
activating a text-only model under a stored "this server" choice is fine, but activating
one under a stored pyannote.ai choice with no key, or clearing a cloud config while lite,
changes what the stored choice resolves to. Every ASR switch (``set-active``,
``clear-active``, first-config auto-activation, the promotion on delete) therefore resolves
the plan before and after and asks this module what to do.

The rule is the PUT's rule, applied to the plan the switch would produce:

* a speaker-detection (diarization-step) error is refused only when the switch INTRODUCES
  it (one already present before it is the stored state's problem, reported by
  ``GET /user-settings/pipeline``), and never while the #1109 lock pins the source: the
  deployment chose it, and the user cannot change it to get out of the refusal;
* a transcription-step error is always refused (#1228). The switch IS the transcription
  choice, so there is no stored state to protect: on lite every switch onto local Whisper
  (a ``local`` config, or clearing back to a local default) answers
  ``local_asr_unavailable``, whatever it pointed at before and whatever the lock says.

DB reads only; nothing is built, decrypted or contacted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.services.pipeline_plan import PipelinePlan
from app.services.pipeline_plan import PlanIssue
from app.services.pipeline_plan import resolve_pipeline_plan
from app.services.pipeline_plan_inputs import load_plan_inputs

if TYPE_CHECKING:
    from fastapi import Request
    from sqlalchemy.orm import Session

    from app.models.user_asr_settings import UserASRSettings

#: The plan steps whose blocking issues refuse an ASR switch (rules in the module docstring).
REFUSED_STEPS: frozenset[str] = frozenset({"diarization", "transcription"})

#: Refused whenever the resulting plan has them: the switch itself chose the transcription.
_ALWAYS_REFUSED_STEPS: frozenset[str] = frozenset({"transcription"})


@dataclass(frozen=True)
class SwitchOutcome:
    """What a switch would do. ``refusal`` is ``None`` when it may go ahead."""

    plan: PipelinePlan
    refusal: PlanIssue | None


def assess_asr_switch(
    db: Session,
    user_id: int,
    target: UserASRSettings | None,
    *,
    request: Request | None = None,
    refused_steps: frozenset[str] = REFUSED_STEPS,
) -> SwitchOutcome:
    """Plan the user's next file as if ``target`` were the active config.

    Args:
        db: An open session (reads only).
        user_id: Whose pipeline.
        target: The config about to become active; ``None`` for "no active config" (the
            env / local fallback, as ``clear-active`` leaves it).
        request: The HTTP request, for the #1109 lock and capability resolution.
        refused_steps: Steps whose new blocking issues refuse the switch.
    """
    inputs = load_plan_inputs(db, user_id, request=request, active_asr_config=target)
    after = resolve_pipeline_plan(inputs)
    chosen = [
        issue
        for issue in after.blocking()
        if issue.step in refused_steps and issue.step in _ALWAYS_REFUSED_STEPS
    ]
    if chosen:
        return SwitchOutcome(plan=after, refusal=chosen[0])
    if inputs.source_locked:
        return SwitchOutcome(plan=after, refusal=None)
    before = resolve_pipeline_plan(load_plan_inputs(db, user_id, request=request))
    existing = {issue.code for issue in before.blocking()}
    introduced = [
        issue
        for issue in after.blocking()
        if issue.step in refused_steps and issue.code not in existing
    ]
    return SwitchOutcome(plan=after, refusal=introduced[0] if introduced else None)


def _transcription_refusal(
    db: Session, user_id: int, target: UserASRSettings | None, request: Request | None
) -> PlanIssue | None:
    """The transcription-step refusal a switch to ``target`` would get, if any."""
    refusal = assess_asr_switch(db, user_id, target, request=request).refusal
    return refusal if refusal is not None and refusal.step in _ALWAYS_REFUSED_STEPS else None


def active_delete_refusal(
    db: Session,
    user_id: int,
    remaining: list[UserASRSettings],
    *,
    request: Request | None = None,
) -> PlanIssue | None:
    """Why deleting the user's ACTIVE config must be refused, or ``None`` (issue #1228).

    Ask BEFORE deleting. Only lite ever refuses: there, with no remaining config that can
    transcribe, clearing the pointer would mean local Whisper (``local_asr_unavailable``).
    Deleting a config that is not active is never refused.
    """
    cleared = _transcription_refusal(db, user_id, None, request)
    if cleared is None:
        return None
    if any(_transcription_refusal(db, user_id, c, request) is None for c in remaining):
        return None
    return cleared


def choose_after_active_delete(
    db: Session,
    user_id: int,
    remaining: list[UserASRSettings],
    *,
    request: Request | None = None,
) -> UserASRSettings | None:
    """What becomes active once the user's active config is deleted; ``None``: clear.

    1. The first remaining config the switch rules accept (issue #1225).
    2. Otherwise clear the pointer, unless clearing would mean local Whisper on lite
       (#1228): then the first remaining config whose transcription can run, even with a
       stored speaker-detection problem (reported, as for any stored state).
    """
    for candidate in remaining:
        if assess_asr_switch(db, user_id, candidate, request=request).refusal is None:
            return candidate
    if _transcription_refusal(db, user_id, None, request) is None:
        return None
    return next(
        (c for c in remaining if _transcription_refusal(db, user_id, c, request) is None), None
    )
