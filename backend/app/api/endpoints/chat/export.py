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
                speaker = citation.get("speaker") or "Unknown speaker"
                title = citation.get("title") or "Untitled recording"
                stamp = _clock(citation.get("start_time"))
                link = (
                    f"/files/{citation.get('file_uuid')}?t={int(citation.get('start_time') or 0)}"
                )
                lines.append(f"- `[{citation.get('id')}]` **{title}** — {speaker} at {stamp}")
                lines.append(f"  {link}")
                snippet = (citation.get("snippet") or "").strip()
                if snippet:
                    lines.append(f"  > {snippet}")
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
