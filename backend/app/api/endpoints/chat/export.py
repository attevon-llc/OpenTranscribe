"""Export a conversation (issue #52).

Chat answers frequently end up in a meeting note, a ticket or an email, so
getting one out of the app without copy-pasting message by message is table
stakes — both ChatGPT and Open WebUI offer it.

Markdown is the default because that is what the answers already are; JSON is
offered for anyone piping conversations into their own tooling. Citations are
rendered as a source list per answer so the export stays verifiable away from
the app, with deep links back to the exact moment in each recording.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.endpoints.chat.common import get_owned_conversation
from app.db.base import get_db
from app.models.chat import ROLE_USER
from app.models.chat import STATUS_SUPERSEDED
from app.models.chat import ChatMessage

logger = logging.getLogger(__name__)

router = APIRouter()


def _clock(seconds: float | None) -> str:
    total = max(0, int(seconds or 0))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _safe_filename(title: str | None, extension: str) -> str:
    """Build a download filename that is safe in a Content-Disposition header."""
    base = (title or "conversation").strip() or "conversation"
    cleaned = "".join(ch if ch.isalnum() or ch in " -_" else "-" for ch in base)[:60]
    cleaned = cleaned.strip() or "conversation"
    return f"{cleaned}.{extension}"


def _render_citation(citation: dict) -> list[str]:
    """One citation's Markdown lines, kind-aware (issue #464 amendment b).

    Before this, every citation rendered identically — a speaker quote at a
    timestamp — which is wrong for anything that is not a transcript chunk:

    * ``summary``: LLM-generated prose ABOUT the recording, not a quote from
      it. No timestamp (there is no single moment it corresponds to), no
      speaker, and a deep link to the file's summary view rather than a
      player position — the same distinction ``ChatSources.svelte`` draws for
      the live-stream rendering of the same citation shape.
    * ``document`` (a later lane's kind, #362/#403 Stage 6 — handled here so
      that lane needs no follow-up edit to this file): a chunk index and page
      under ``/documents/``, never a fabricated ``t=0`` — a document has no
      timeline, and an audio-style timestamp link would look like it works
      and land nowhere meaningful.
    * everything else (``chunk``, ``digest``, and an absent ``kind`` for
      messages persisted before the field existed): the original rendering,
      unchanged.
    """
    kind = citation.get("kind") or "chunk"
    title = citation.get("title") or "Untitled recording"
    cid = citation.get("id")
    snippet = (citation.get("snippet") or "").strip()
    file_uuid = citation.get("file_uuid")

    if kind == "summary":
        section = citation.get("digest_section")
        link = f"/files/{file_uuid}?view=summary"
        if section is not None:
            link += f"&section={section}"
        lines = [f"- `[{cid}]` **{title}** — AI-generated summary", f"  {link}"]
        if snippet:
            # Italicized, never blockquoted: a blockquote reads as "these were
            # the words", which is exactly what a summary citation is not.
            lines.append(f"  *{snippet}*")
        return lines

    if kind == "document":
        chunk_index = citation.get("chunk_index") or 0
        link = f"/documents/{file_uuid}?chunk={chunk_index}"
        lines = [f"- `[{cid}]` **{title}** — document excerpt", f"  {link}"]
        if snippet:
            lines.append(f"  > {snippet}")
        return lines

    speaker = citation.get("speaker") or "Unknown speaker"
    stamp = _clock(citation.get("start_time"))
    link = f"/files/{file_uuid}?t={int(citation.get('start_time') or 0)}"
    lines = [f"- `[{cid}]` **{title}** — {speaker} at {stamp}", f"  {link}"]
    if snippet:
        lines.append(f"  > {snippet}")
    return lines


def _render_markdown(conversation, messages: list[ChatMessage]) -> str:
    lines: list[str] = [f"# {conversation.title or 'Chat'}", ""]

    created = conversation.created_at
    if isinstance(created, datetime):
        lines.append(f"*Exported from OpenTranscribe — started {created:%Y-%m-%d %H:%M}*")
        lines.append("")

    scope = conversation.scope
    scope_bits = []
    if scope.get("file_uuids"):
        scope_bits.append(f"{len(scope['file_uuids'])} recording(s)")
    if scope.get("collection_uuids"):
        scope_bits.append(f"{len(scope['collection_uuids'])} collection(s)")
    if scope.get("tag_names"):
        scope_bits.append(f"tags: {', '.join(scope['tag_names'])}")
    if scope.get("speakers"):
        scope_bits.append(f"speakers: {', '.join(scope['speakers'])}")
    if scope_bits:
        lines.extend([f"**Context:** {' · '.join(scope_bits)}", ""])

    for message in messages:
        if message.role == ROLE_USER:
            lines.extend(["---", "", f"## {message.content}", ""])
            continue

        lines.extend([message.content or "", ""])

        citations = message.citations or []
        if citations:
            lines.append("**Sources**")
            lines.append("")
            for citation in citations:
                lines.extend(_render_citation(citation))
            lines.append("")

        if message.model:
            provider = f"{message.provider}/" if message.provider else ""
            lines.extend([f"<sub>{provider}{message.model}</sub>", ""])

    return "\n".join(lines).rstrip() + "\n"


def _render_json(conversation, messages: list[ChatMessage]) -> str:
    payload = {
        "uuid": str(conversation.uuid),
        "title": conversation.title,
        "created_at": conversation.created_at.isoformat() if conversation.created_at else None,
        "scope": conversation.scope,
        "messages": [
            {
                "uuid": str(m.uuid),
                "role": m.role,
                "content": m.content,
                "reasoning_content": m.reasoning_content,
                "citations": m.citations or [],
                "provider": m.provider,
                "model": m.model,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in messages
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


@router.get("/conversations/{conversation_uuid}/export")
def export_conversation(
    conversation_uuid: str,
    # Named "format" on the wire (what callers expect) but bound to a
    # non-shadowing local.
    export_format: str = Query("markdown", alias="format", pattern="^(markdown|json)$"),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> Response:
    """Download a conversation as Markdown or JSON.

    Superseded turns (edited questions and their replaced answers) are omitted:
    an export should read as the conversation the user actually had.
    """
    conversation = get_owned_conversation(db, ctx, conversation_uuid)

    messages = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.conversation_id == conversation.id,
            ChatMessage.status != STATUS_SUPERSEDED,
        )
        .order_by(ChatMessage.id.asc())
        .all()
    )

    if export_format == "json":
        body = _render_json(conversation, messages)
        media_type = "application/json"
        filename = _safe_filename(conversation.title, "json")
    else:
        body = _render_markdown(conversation, messages)
        media_type = "text/markdown"
        filename = _safe_filename(conversation.title, "md")

    logger.info(
        "Chat export: conversation %s (%s, %d messages)",
        conversation_uuid,
        export_format,
        len(messages),
    )
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
