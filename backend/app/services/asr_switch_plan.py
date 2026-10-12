"""Re-plan the pipeline when the active transcription configuration changes (issue #1225).

``PUT /user-settings/transcription`` refuses a NEW speaker-detection choice the pipeline
plan cannot run. The ASR side can break the same combination from the other direction:
activating a text-only model under a stored "this server" choice is fine, but activating
one under a stored pyannote.ai choice with no key, or clearing a cloud config while lite,
changes what the stored choice resolves to. Every ASR switch (``set-active``,
``clear-active``, first-config auto-activation, the promotion on delete) therefore resolves
the plan before and after and asks this module what to do.

The rule is the PUT's rule, applied to the plan the switch would produce:

* refuse only a blocking issue the switch INTRODUCES (one already present before it is the
  stored state's problem, reported by ``GET /user-settings/pipeline``);
* only on a step in :data:`REFUSED_STEPS` (the diarization step, as the PUT);
* never while the #1109 lock pins the speaker-detection source: the deployment chose it,
  and the user cannot change it to get out of the refusal.

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

#: The plan steps whose NEW blocking issues refuse an ASR switch. Lite's transcription-step
#: refusals (``local_asr_unavailable`` on clearing the last cloud config) are WP6's to add
#: here, with the 409s that go with them; until then they are reported, not refused.
REFUSED_STEPS: frozenset[str] = frozenset({"diarization"})


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
    before = resolve_pipeline_plan(load_plan_inputs(db, user_id, request=request))
    inputs = load_plan_inputs(db, user_id, request=request, active_asr_config=target)
    after = resolve_pipeline_plan(inputs)
    if inputs.source_locked:
        return SwitchOutcome(plan=after, refusal=None)
    existing = {issue.code for issue in before.blocking()}
    introduced = [
        issue
        for issue in after.blocking()
        if issue.step in refused_steps and issue.code not in existing
    ]
    return SwitchOutcome(plan=after, refusal=introduced[0] if introduced else None)
