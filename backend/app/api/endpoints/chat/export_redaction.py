"""Whose policy masks a chat export, and which field of it needed masking (issue #863).

``chat/export.py`` resolved NO redaction policy at all until this landed, which made it the
one transcript-bearing export surface #673 and #85 never reached. Of the three fields it
emits, exactly one needed fixing:

* ``content`` / ``reasoning_content`` are already masked **at persist time** by
  ``chat/output_redactor.OutputRedactor`` — the stream and the stored row carry the same
  masked text (``services/chat/CLAUDE.md``: "the persisted answer is the masked one") — so
  re-masking them here would be a second masker over the same bytes.
* ``citations[].snippet`` was NOT. A snippet is a slice of ``chat/redactor.MaskedChunk``'s
  content, whose masking answers the **egress** question ("may this text be sent to a
  provider"), and a deployment running a **local** model answers that "no masking needed" by
  design (``redaction/llm_guard.is_local_provider``). So a persisted snippet can be raw
  transcript text, and the export wrote it into a file the user keeps.

**Egress and export are different decisions and must not share an answer.** Nothing here
consults ``redact_before_llm`` or the local-provider exemption; it resolves the REQUESTING
USER's config — the read-surface subject argued at length in ``redaction/export_policy.py``
— with the admin ``export_locked`` floor already folded in by ``resolve_effective_config``.

⚠️ **A chat export never reveals, and must not grow a ``?redact=false``.** The transcript
exports beside it take that parameter and hand it to ``cfg.reveal_categories(requested,
is_owner)``, which ``export_locked`` overrides. There is no single owner here: one chat
answer quotes across every recording the user can reach, so ``is_owner`` has nothing to be
true *of*, and a reveal switch would be a per-file authorization question asked once for a
cross-file artifact.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.services.redaction.config import EffectiveRedactionConfig
from app.services.redaction.config import resolve_effective_config
from app.services.redaction.summary_redaction import SummaryMaskingUnavailableError
from app.services.redaction.summary_redaction import mask_summary_leaf

logger = logging.getLogger(__name__)


def resolve_export_policy(db: Session, user_id: int) -> EffectiveRedactionConfig:
    """The requesting user's effective policy, or refuse the export.

    Same fail-closed shape as ``files/transcript_export._resolve_export_redaction`` and
    ``files/subtitles._resolve_subtitle_redaction``: returning ``None`` here would read as
    "redaction is off", which is indistinguishable from "the caller forgot", and is exactly
    the value that let two of the three export paths in issue #85 ship unmasked.

    Raises:
        HTTPException: 503 when the redaction policy cannot be resolved.
    """
    try:
        return resolve_effective_config(db, user_id)
    except Exception as e:
        logger.exception("Failed to resolve redaction config; refusing the chat export")
        raise HTTPException(
            status_code=503,
            detail="Redaction policy is temporarily unavailable; export withheld.",
        ) from e


def mask_citations(citations: list[dict], cfg: EffectiveRedactionConfig) -> list[dict]:
    """Return the citations with every ``snippet`` masked under ``cfg``.

    ``mask_summary_leaf`` is the right masker and neither of chat's own two is:
    ``mask_chunks`` addresses text by **time range** and ``mask_digests`` by **provenance**,
    and a snippet has neither — it is a whitespace-normalised, truncated slice, so no stored
    offset addresses it. Detecting live over one string is exactly what that function already
    does for the other derived-prose surfaces (summaries, recurrence items), and reusing it
    keeps one masker rather than a fourth.

    ``title`` and ``speaker`` are deliberately left alone, for the reason
    ``search/CLAUDE.md`` gives for ``title_highlighted``/``speaker_highlighted``: both are
    user-assigned labels rather than transcript text, and masking a speaker name is what the
    speaker plane exists to let people *set*.

    A NEW dict is built per citation. Mutating the loaded rows in place is the trap
    ``services/CLAUDE.md`` records against the subtitle masker: an export path that happened
    to commit would flush masked text over the stored citations, destroying the original that
    read-time masking exists to preserve.

    Raises:
        HTTPException: 503 when a detector feeding an enabled category could not run —
            withheld rather than served half-masked, matching every sibling surface.
    """
    if not citations:
        return []
    try:
        return [
            {**citation, "snippet": mask_summary_leaf(citation.get("snippet") or "", cfg)}
            for citation in citations
        ]
    except SummaryMaskingUnavailableError as e:
        logger.warning("Chat export withheld: citation masking unavailable (%s)", e)
        raise HTTPException(
            status_code=503,
            detail="Content redaction is temporarily unavailable; export withheld.",
        ) from e
