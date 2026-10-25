"""Every download surface must survive a non-ASCII file name (issue #1283).

Starlette encodes response headers as latin-1, so a raw ``attachment; filename="<name>"``
built from an Arabic, Chinese or accented name raised ``UnicodeEncodeError`` while the
response was being sent: the export of that file was an HTTP 500 on the transcript,
subtitle, summary and chat export routes. The fix is one RFC 6266 helper
(``app.utils.content_disposition.attachment_header``); these tests drive the real routes
with the names that broke and check what the client actually receives: a 200, a latin-1-safe
``filename=`` fallback, and a ``filename*=UTF-8''`` that decodes back to the exact name.
"""

from __future__ import annotations

import re
import uuid as uuid_pkg
from urllib.parse import unquote

import pytest
from fastapi import status

from app.models.chat import ROLE_ASSISTANT
from app.models.chat import ROLE_USER
from app.models.chat import ChatConversation
from app.models.chat import ChatMessage
from app.models.media import MediaFile
from app.models.media import TranscriptSegment

#: Names that each raised UnicodeEncodeError before the fix.
NON_ASCII_STEMS = {
    "arabic": "اجتماع الميزانية",
    "cjk": "会议记录 第三季度",
    "accented": "Réunion café été",
}

_EXT_RE = re.compile(r"filename\*=UTF-8''(?P<value>[^;]+)$")


def _make_file(db_session, owner, stem: str) -> MediaFile:
    file_uuid = str(uuid_pkg.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        filename=f"{stem}.wav",
        title=stem,
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status="completed",
        is_public=False,
        user_id=owner.id,
        redaction_status="done",
        summary_data={"bluf": "The budget was approved.", "brief_summary": "Short."},
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    db_session.add(
        TranscriptSegment(
            media_file_id=media_file.id, start_time=0.0, end_time=2.0, text="Hello there."
        )
    )
    db_session.commit()
    return media_file


def _decoded_filename(response) -> str:
    """The name a browser would save under, read from ``filename*`` (with the fallback checked)."""
    assert response.status_code == status.HTTP_200_OK, response.text
    header = response.headers["content-disposition"]
    assert header.startswith("attachment; "), header
    match = _EXT_RE.search(header)
    assert match is not None, f"no filename* parameter in {header!r}"
    fallback = re.search(r'filename="(?P<value>[^"]*)"', header)
    assert fallback is not None and fallback.group("value"), header
    assert fallback.group("value").isascii()
    return unquote(match.group("value"))


@pytest.mark.parametrize("fmt", ["txt", "json", "csv", "srt", "vtt", "md"])
@pytest.mark.parametrize("stem", NON_ASCII_STEMS.values(), ids=NON_ASCII_STEMS.keys())
def test_transcript_export_with_a_non_ascii_name(
    client, user_token_headers, normal_user, db_session, stem, fmt
):
    media_file = _make_file(db_session, normal_user, stem)
    response = client.get(
        f"/api/files/{media_file.uuid}/export", headers=user_token_headers, params={"format": fmt}
    )
    assert _decoded_filename(response) == f"{stem}.{fmt}"


@pytest.mark.parametrize(
    ("subtitle_format", "extension"), [("srt", "srt"), ("webvtt", "webvtt"), ("txt", "txt")]
)
@pytest.mark.parametrize("stem", NON_ASCII_STEMS.values(), ids=NON_ASCII_STEMS.keys())
def test_subtitle_export_with_a_non_ascii_name(
    client, user_token_headers, normal_user, db_session, stem, subtitle_format, extension
):
    media_file = _make_file(db_session, normal_user, stem)
    response = client.get(
        f"/api/files/{media_file.uuid}/subtitles",
        headers=user_token_headers,
        params={"subtitle_format": subtitle_format},
    )
    assert _decoded_filename(response) == f"{stem}.{extension}"


@pytest.mark.parametrize("stem", NON_ASCII_STEMS.values(), ids=NON_ASCII_STEMS.keys())
def test_summary_export_with_a_non_ascii_name(
    client, user_token_headers, normal_user, db_session, stem
):
    media_file = _make_file(db_session, normal_user, stem)
    response = client.get(
        f"/api/files/{media_file.uuid}/summary/export",
        headers=user_token_headers,
        params={"format": "md"},
    )
    assert _decoded_filename(response) == f"{stem}-summary.md"


@pytest.mark.parametrize("fmt", ["markdown", "json"])
@pytest.mark.parametrize("stem", NON_ASCII_STEMS.values(), ids=NON_ASCII_STEMS.keys())
def test_chat_export_with_a_non_ascii_title(
    client, user_token_headers, normal_user, db_session, stem, fmt
):
    """The chat export builds its own name from the conversation title; letters survive
    its sanitiser, so an Arabic or Chinese title hit the same latin-1 failure."""
    conversation = ChatConversation(
        uuid=uuid_pkg.uuid4(), user_id=normal_user.id, title=stem, context={}
    )
    db_session.add(conversation)
    db_session.commit()
    db_session.refresh(conversation)
    for role, content in ((ROLE_USER, "question"), (ROLE_ASSISTANT, "answer")):
        db_session.add(
            ChatMessage(
                uuid=uuid_pkg.uuid4(),
                conversation_id=conversation.id,
                role=role,
                content=content,
            )
        )
    db_session.commit()

    response = client.get(
        f"/api/chat/conversations/{conversation.uuid}/export",
        headers=user_token_headers,
        params={"format": fmt},
    )
    extension = "md" if fmt == "markdown" else "json"
    assert _decoded_filename(response) == f"{stem}.{extension}"


def test_an_ascii_name_keeps_a_plain_filename_parameter(
    client, user_token_headers, normal_user, db_session
):
    media_file = _make_file(db_session, normal_user, "plain name")
    response = client.get(
        f"/api/files/{media_file.uuid}/export", headers=user_token_headers, params={"format": "txt"}
    )
    assert response.status_code == status.HTTP_200_OK
    assert 'filename="plain name.txt"' in response.headers["content-disposition"]


def test_a_name_cannot_inject_header_lines_or_break_out_of_the_quotes(
    client, user_token_headers, normal_user, db_session
):
    hostile = 'evil";\r\nX-Injected: 1; x="'
    media_file = _make_file(db_session, normal_user, hostile)
    response = client.get(
        f"/api/files/{media_file.uuid}/export", headers=user_token_headers, params={"format": "txt"}
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    assert "x-injected" not in response.headers
    header = response.headers["content-disposition"]
    assert "\r" not in header and "\n" not in header
    fallback = re.search(r'filename="(?P<value>[^"]*)"', header)
    assert fallback is not None
    # The quote inside the name must not have terminated the quoted string early.
    assert header.count('"') == 2
