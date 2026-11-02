"""The SSE frames a chat turn emits: the encoder, the failure texts and the builders.

``sse`` formats one frame. The builders turn the turn's state into the frames
``service.py`` yields: the context warnings, the ``sources`` frame and the grounding
warnings, one ``delta``/``reasoning`` frame per text event, the provider ``error``
frame, and the closing ``usage``/``done`` pair. The event names and warning codes
are a frozen contract with the frontend; see ``service.py``'s module docstring.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.services.chat import citations as citations_mod
from app.services.chat.language import warning_payload as language_warning_payload
from app.services.chat.output_redactor import OutputRedactor
from app.services.chat.redactor import MaskedChunk
from app.services.chat.settings import ChatSettings
from app.services.chat.streaming import _redact_delta
from app.services.chat.turn import ChatTurn

logger = logging.getLogger(__name__)


#: The only failure text a chat turn ever puts on the wire or in the database. The
#: client renders the translated string for the frame's ``code``; these English
#: literals are what an API consumer without i18n sees (issue #1049).
PROVIDER_UNAVAILABLE_MESSAGE = (
    "The AI provider is temporarily unavailable. Please try again in a moment."
)


PROVIDER_ERROR_MESSAGE = "The AI provider could not generate a response. Please try again."


GENERATION_FAILED_MESSAGE = "Generation failed."


def sse(event: str, payload: dict[str, Any]) -> str:
    """Format one SSE frame (same helper shape as the subtitle export stream)."""
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


# SSE comment lines are ignored by every client but keep the connection warm.
def _context_warning_frames(meta: dict[str, Any]) -> list[str]:
    """The ``warning`` frames context preparation can raise: a speaker or a plan."""
    frames: list[str] = []
    # A speaker mention that matched more than one
    # roster entry resolves to NO filter (never a guess) — surface
    # the candidates so the user can disambiguate, exactly like
    # `context_dropped`/language warn elsewhere in this turn.
    ambiguous_speakers = (meta.get("speaker_resolution") or {}).get("ambiguous") or []
    if ambiguous_speakers:
        frames.append(
            sse(
                "warning",
                {"code": "ambiguous_speaker", "candidates": list(ambiguous_speakers)},
            )
        )

    # #403: a plan that failed to parse/execute falls back to
    # the rules-only route rather than failing the turn — surfaced
    # so a user (or `ChatMessageMeta`) can tell "no plan was
    # attempted" from "one was attempted and dropped".
    if (meta.get("plan") or {}).get("failed"):
        frames.append(sse("warning", {"code": "plan_failed"}))
    return frames


def _grounding_frames(
    turn: ChatTurn,
    *,
    settings: ChatSettings,
    masked: list[MaskedChunk],
    excerpt_ids: list[int],
    overview,
    overview_block: str,
    prompt_diagnostics: dict[str, Any],
    llm,
    answer_tokens: int,
    assistant_message_uuid: str,
) -> list[str]:
    """The ``sources`` frame, then any warning that the answer is weakly grounded."""
    frames: list[str] = []
    turn.offered_citations = citations_mod.build_offered_citations(masked, excerpt_ids)
    # #532 arm (a): the overview's own citation ids join the offer —
    # but only when the block actually SURVIVED into the prompt.
    # Offering ids for a budget-dropped block is exactly the
    # sourced-but-not-grounded failure issue #384 closed.
    if (
        settings.overview_citable
        and overview is not None
        and overview.citation_payloads
        and overview_block
        and "overview" not in (prompt_diagnostics.get("evidence_blocks_dropped") or ())
    ):
        turn.offered_citations = turn.offered_citations + list(overview.citation_payloads)
    frames.append(sse("sources", {"citations": turn.offered_citations}))

    if not masked:
        # Nothing reached the prompt at all. The model will answer "I
        # don't have enough information", which is indistinguishable
        # from a grounded negative — and, worse, from a working search
        # that simply found nothing. Retrieval degrades to an empty
        # list on ANY failure (issue #438: an OpenSearch 503 during a
        # reindex produced a confident "I don't know" over a corpus
        # full of matching material), so the counters are the only
        # evidence the user can be shown. `retrieved` separates the
        # remaining two: 0 means the search returned nothing,
        # non-zero means masking failed closed on every chunk.
        #
        # `retrieval_failed` is a THIRD, more specific fact
        # `_prepare_context` now threads out of `retrieve_chunks`'s
        # `diagnostics` param: the search backend itself raised or was
        # unreachable, rather than genuinely returning zero hits. That
        # is worth its own code — "your library has nothing about
        # this" and "search was down" call for different next steps —
        # so the two are mutually exclusive branches here, exactly
        # like `context_dropped`/`no_context` below.
        retrieval_failed = bool(turn.metadata.get("retrieval_failed"))
        code = "retrieval_failed" if retrieval_failed else "no_context"
        turn.metadata[code] = True
        logger.warning(
            "Chat turn %s answered with NO excerpts (%s): retrieved=%s, "
            "files_searched=%s — the reply is ungrounded",
            assistant_message_uuid,
            code,
            turn.metadata.get("retrieved", 0),
            turn.metadata.get("files_searched", 0),
        )
        frames.append(
            sse(
                "warning",
                {
                    "code": code,
                    "retrieved": turn.metadata.get("retrieved", 0),
                    "files_searched": turn.metadata.get("files_searched", 0),
                },
            )
        )
    elif not excerpt_ids:
        # Retrieval found material and the budget left no room for any
        # of it. Say so rather than answering ungrounded behind a
        # normal-looking reply.
        turn.metadata["context_dropped"] = True
        logger.warning(
            "Chat turn %s dropped all %d retrieved excerpts: no room in "
            "the %d-token context window after %d reserved answer tokens",
            assistant_message_uuid,
            len(masked),
            llm.user_context_window,
            answer_tokens,
        )
        frames.append(
            sse(
                "warning",
                {"code": "context_dropped", "retrieved": len(masked)},
            )
        )

    # Answering anyway is deliberate: a library is usually mixed, and
    # refusing every question because one recording is Spanish would
    # be worse than useless. The turn says what it could not serve
    # well and answers from the rest.
    language_warning = language_warning_payload(turn.metadata)
    if language_warning is not None:
        logger.info(
            "Chat turn %s spans unsupported RAG languages %s (%d files, %d of unknown language)",
            assistant_message_uuid,
            language_warning["languages"],
            language_warning["files"],
            language_warning["unknown_files"],
        )
        frames.append(sse("warning", language_warning))
    return frames


async def _text_frame(
    event, turn: ChatTurn, answer_redactor: OutputRedactor, reasoning_redactor: OutputRedactor
) -> str:
    """The ``delta``/``reasoning`` frame for one text event; ``""`` while it is held back."""
    if event.type == "delta":
        safe = await _redact_delta(answer_redactor, event.text)
        if safe:
            turn.answer_parts.append(safe)
            return sse("delta", {"text": safe})
        return ""
    # Reasoning is rendered too (a collapsed block, not a hidden
    # one), so it is a display surface and gets its own buffer.
    safe = await _redact_delta(reasoning_redactor, event.text)
    if safe:
        turn.reasoning_parts.append(safe)
        return sse("reasoning", {"text": safe})
    return ""


def _provider_error_frame(turn: ChatTurn, event, conversation_uuid: str) -> str:
    """Record a provider ``error`` event on the turn and return its ``error`` frame."""
    # Provider prose (quota text, request IDs, SDK retry counts) goes
    # to the log only; the frame, the persisted row and the API all
    # carry a fixed sentence plus a code the client translates
    # (issue #1049, same hygiene as #959).
    logger.warning(
        "LLM provider error for conversation %s (transient=%s): %s",
        conversation_uuid,
        event.transient,
        event.message,
    )
    if event.transient:
        turn.error = PROVIDER_UNAVAILABLE_MESSAGE
        turn.error_code = "provider_unavailable"
        turn.metadata["error_code"] = turn.error_code
        return sse("error", {"code": "provider_unavailable", "message": turn.error})
    turn.error = PROVIDER_ERROR_MESSAGE
    turn.error_code = "provider_error"
    turn.metadata["error_code"] = turn.error_code
    return sse("error", {"code": "provider_error", "message": turn.error})


def _closing_frames(turn: ChatTurn) -> list[str]:
    """``usage`` (only when the turn did not fail) and ``done``."""
    frames: list[str] = []
    # Only reachable on a normal (non-cancelled) exit — there is no client
    # left to receive these frames otherwise.
    if not turn.error:
        frames.append(
            sse(
                "usage",
                {
                    "prompt_tokens": turn.prompt_tokens or 0,
                    "completion_tokens": turn.completion_tokens or 0,
                    "total_tokens": turn.total_tokens,
                    "estimated": turn.tokens_estimated,
                },
            )
        )

    done_payload: dict[str, Any] = {"finish_reason": turn.finish_reason or "stop"}
    if turn.title:
        done_payload["title"] = turn.title
    frames.append(sse("done", done_payload))
    return frames
