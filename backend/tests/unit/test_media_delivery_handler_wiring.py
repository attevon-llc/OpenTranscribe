"""The download handlers hand the caller's tenant and redaction variant to every helper.

``test_media_delivery_gate_branches.py`` pins the helpers; this pins the handlers' wiring:
the tenant the variant is resolved in, the variant the readiness check is asked about, and
the never-collapsed download audit event. The helpers are replaced at the module whose
handler reads them (``files.media_delivery``), so no database or storage is needed.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import Request
from sqlalchemy.orm import Session

from app.api.endpoints.files import media_delivery
from app.models.media import MediaFile
from app.models.user import User
from app.services.request_context import RequestContext

_ORG = 31


@pytest.fixture
def wiring(monkeypatch) -> dict[str, Any]:
    seen: dict[str, Any] = {"variant": [], "ready": [], "audit": []}
    media_file = MediaFile(id=4242, uuid="00000000-0000-0000-0000-000000004242")

    def _variant(db, db_file, user_id, mode, organization_id=None):
        seen["variant"].append(organization_id)
        return "fp-reader"

    def _ready(db_file, mode, redaction_fingerprint=""):
        seen["ready"].append(redaction_fingerprint)
        return {"url": "u", "filename": "f"}

    monkeypatch.setattr(media_delivery, "get_media_file_by_uuid", lambda *a, **k: media_file)
    monkeypatch.setattr(media_delivery, "_download_redaction_variant", _variant)
    monkeypatch.setattr(media_delivery, "_resolve_ready_download", _ready)
    monkeypatch.setattr(
        media_delivery, "record_file_event", lambda *a, **k: seen["audit"].append(k)
    )
    return seen


def _args() -> tuple[Session, User, RequestContext, Request]:
    user = MagicMock(spec=User)
    user.id = 7
    ctx = MagicMock(spec=RequestContext)
    ctx.org_id = _ORG
    return MagicMock(spec=Session), user, ctx, MagicMock(spec=Request)


def test_prepare_resolves_the_variant_in_the_callers_tenant_and_asks_about_it(wiring):
    db, user, ctx, request = _args()
    result = media_delivery.prepare_download(
        "f", request, mode="video_subtitles", db=db, current_user=user, ctx=ctx
    )
    assert result == {"status": "ready", "url": "u", "filename": "f"}
    assert wiring["variant"] == [_ORG]
    assert wiring["ready"] == ["fp-reader"]
    assert [a["dedup"] for a in wiring["audit"]] == [False]


def test_the_stream_resolves_the_variant_in_the_callers_tenant_and_asks_about_it(wiring):
    db, user, ctx, request = _args()
    response = media_delivery.download_stream(
        "f", request, mode="video_subtitles", db=db, current_user=user, ctx=ctx
    )

    async def _first_frame() -> str:
        async for frame in response.body_iterator:
            return str(frame)
        return ""

    frame = asyncio.run(_first_frame())
    assert frame.startswith("event: ready")
    assert wiring["variant"] == [_ORG]
    assert wiring["ready"] == ["fp-reader"]
    assert [a["dedup"] for a in wiring["audit"]] == [False]
