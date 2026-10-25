"""``export_locked`` and the reader's policy must govern every DOCX / PDF export (issue #1278).

The synchronous formats are pinned by ``test_transcript_export_redaction_gate.py``. A document
is rendered on a worker, so this drives the whole path (the route, then the task body) and reads
the text a user would see in the file. Every "profanity absent" assertion is paired with
"clean text present" in the SAME document, and the unlocked cases show the profanity is really
there to be masked: a text extraction that returns nothing must fail, not pass.
"""

from __future__ import annotations

import pytest
from fixtures.document_export import document_text
from fixtures.redaction_export_world import CLEAN_TEXT
from fixtures.redaction_export_world import PROFANITY
from fixtures.redaction_export_world import enable_user_redaction
from fixtures.redaction_export_world import make_redactable_file
from fixtures.redaction_export_world import set_export_locked

pytestmark = pytest.mark.xdist_group("redaction_system_settings")

DOCUMENT_FORMATS = ("docx", "pdf")


def _text(fmt: str, data: bytes) -> str:
    text: str = document_text(fmt, data)
    assert text.strip(), f"no text could be read back from the {fmt}: an empty read proves nothing"
    return text


@pytest.mark.parametrize("fmt", DOCUMENT_FORMATS)
def test_export_locked_masks_the_document(
    client,
    super_admin_token_headers,
    user_token_headers,
    normal_user,
    db_session,
    document_export,
    fmt,
):
    enable_user_redaction(client, user_token_headers)
    set_export_locked(client, super_admin_token_headers, True)
    media_file = make_redactable_file(db_session, normal_user)

    text = _text(fmt, document_export.export(client, user_token_headers, media_file, format=fmt))

    assert PROFANITY not in text, f"{fmt} leaked unmasked content under export_locked"
    assert CLEAN_TEXT in text


@pytest.mark.parametrize("fmt", DOCUMENT_FORMATS)
def test_export_locked_cannot_be_bypassed_by_reveal_original(
    client,
    super_admin_token_headers,
    user_token_headers,
    normal_user,
    db_session,
    document_export,
    fmt,
):
    enable_user_redaction(client, user_token_headers)
    set_export_locked(client, super_admin_token_headers, True)
    media_file = make_redactable_file(db_session, normal_user)

    data = document_export.export(
        client, user_token_headers, media_file, format=fmt, reveal_original=True
    )
    text = _text(fmt, data)

    assert PROFANITY not in text, f"{fmt} bypassed export_locked via reveal_original"
    assert CLEAN_TEXT in text


@pytest.mark.parametrize("fmt", DOCUMENT_FORMATS)
def test_unlocked_and_no_policy_exports_the_original(
    client,
    super_admin_token_headers,
    user_token_headers,
    normal_user,
    db_session,
    document_export,
    fmt,
):
    set_export_locked(client, super_admin_token_headers, False)
    media_file = make_redactable_file(db_session, normal_user)

    text = _text(fmt, document_export.export(client, user_token_headers, media_file, format=fmt))

    assert PROFANITY in text, f"{fmt} masked content with no policy enforcing it"
    assert CLEAN_TEXT in text


@pytest.mark.parametrize("fmt", DOCUMENT_FORMATS)
def test_unlocked_with_the_users_own_policy_masks_by_default(
    client,
    super_admin_token_headers,
    user_token_headers,
    normal_user,
    db_session,
    document_export,
    fmt,
):
    enable_user_redaction(client, user_token_headers)
    set_export_locked(client, super_admin_token_headers, False)
    media_file = make_redactable_file(db_session, normal_user)

    text = _text(fmt, document_export.export(client, user_token_headers, media_file, format=fmt))

    assert PROFANITY not in text
    assert CLEAN_TEXT in text


@pytest.mark.parametrize("fmt", DOCUMENT_FORMATS)
def test_unlocked_owner_reveal_shows_the_original(
    client,
    super_admin_token_headers,
    user_token_headers,
    normal_user,
    db_session,
    document_export,
    fmt,
):
    enable_user_redaction(client, user_token_headers)
    set_export_locked(client, super_admin_token_headers, False)
    media_file = make_redactable_file(db_session, normal_user)

    data = document_export.export(
        client, user_token_headers, media_file, format=fmt, reveal_original=True
    )

    assert PROFANITY in _text(fmt, data)
