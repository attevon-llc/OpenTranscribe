"""``export_locked`` must be enforced for EVERY transcript export format, not just subtitles.

Issue #673: the admin "mandate censored exports for all users" floor was enforced in exactly
one backend path (``files/subtitles.py``). Every other export format — txt/json/csv/srt/vtt —
was serialized **client-side** in the browser from data already in memory, so it never
consulted the setting at all: an admin who enabled the floor got the guarantee for subtitle
downloads and nothing else.

The fix moves txt/json/csv/srt/vtt serialization to a new backend endpoint,
``GET /api/files/{uuid}/export``, which resolves the same ``EffectiveRedactionConfig`` the
subtitle export does and applies the identical fail-closed rule: ``export_locked`` always wins
over a caller's ``redact=false``, regardless of format.

These tests enumerate every format so a new format added later without wiring the gate fails
here, rather than reproducing the exact bug shape this issue found (issue #673's own "Suggested
fix" #2).
"""

from __future__ import annotations

import pytest
from fastapi import status
from fixtures.redaction_export_world import CLEAN_TEXT
from fixtures.redaction_export_world import PROFANITY
from fixtures.redaction_export_world import enable_user_redaction
from fixtures.redaction_export_world import make_redactable_file
from fixtures.redaction_export_world import set_export_locked

ALL_FORMATS = ("txt", "json", "csv", "srt", "vtt", "md")


def _export(client, headers, media_file, fmt: str, *, redact: bool | None = None):
    params = {"format": fmt}
    if redact is not None:
        params["redact"] = "false" if redact is False else "true"
    return client.get(f"/api/files/{media_file.uuid}/export", headers=headers, params=params)


# ---------------------------------------------------------------------------
# export_locked ON: every format must mask, and a reveal request cannot bypass it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ALL_FORMATS)
def test_export_locked_masks_every_format(
    client, super_admin_token_headers, user_token_headers, normal_user, db_session, fmt
):
    enable_user_redaction(client, user_token_headers)
    set_export_locked(client, super_admin_token_headers, True)
    media_file = make_redactable_file(db_session, normal_user)

    response = _export(client, user_token_headers, media_file, fmt)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert PROFANITY not in response.text, (
        f"{fmt} export leaked unmasked content under export_locked"
    )
    # Whitespace-normalized: SRT/VTT cues wrap a sentence onto two lines.
    assert CLEAN_TEXT in " ".join(response.text.split())


@pytest.mark.parametrize("fmt", ALL_FORMATS)
def test_export_locked_cannot_be_bypassed_by_redact_false(
    client, super_admin_token_headers, user_token_headers, normal_user, db_session, fmt
):
    """The owner's own ``?redact=false`` reveal request must not defeat the admin floor."""
    enable_user_redaction(client, user_token_headers)
    set_export_locked(client, super_admin_token_headers, True)
    media_file = make_redactable_file(db_session, normal_user)

    response = _export(client, user_token_headers, media_file, fmt, redact=False)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert PROFANITY not in response.text, f"{fmt} export bypassed export_locked via redact=false"


# ---------------------------------------------------------------------------
# export_locked OFF: an owner who disabled redaction gets the raw content back
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ALL_FORMATS)
def test_export_locked_off_and_redaction_disabled_exports_unmasked(
    client, super_admin_token_headers, user_token_headers, normal_user, db_session, fmt
):
    set_export_locked(client, super_admin_token_headers, False)
    media_file = make_redactable_file(db_session, normal_user)

    response = _export(client, user_token_headers, media_file, fmt)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert PROFANITY in response.text, f"{fmt} export masked content with no policy enforcing it"


@pytest.mark.parametrize("fmt", ALL_FORMATS)
def test_export_locked_off_but_user_enabled_masks_by_default(
    client, super_admin_token_headers, user_token_headers, normal_user, db_session, fmt
):
    """A user's own redaction preference still applies; export_locked being off just
    means it is not admin-mandated."""
    enable_user_redaction(client, user_token_headers)
    set_export_locked(client, super_admin_token_headers, False)
    media_file = make_redactable_file(db_session, normal_user)

    response = _export(client, user_token_headers, media_file, fmt)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert PROFANITY not in response.text


@pytest.mark.parametrize("fmt", ALL_FORMATS)
def test_export_locked_off_owner_reveal_shows_original(
    client, super_admin_token_headers, user_token_headers, normal_user, db_session, fmt
):
    """Without the admin floor, the owner's own ``?redact=false`` reveals their content."""
    enable_user_redaction(client, user_token_headers)
    set_export_locked(client, super_admin_token_headers, False)
    media_file = make_redactable_file(db_session, normal_user)

    response = _export(client, user_token_headers, media_file, fmt, redact=False)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert PROFANITY in response.text


# ---------------------------------------------------------------------------
# Misc endpoint behaviour
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["exe", "markdown", "docx", "pdf"])
def test_unsupported_format_is_400(client, user_token_headers, normal_user, db_session, fmt):
    """An unknown token is refused, never served as some other format.

    ``markdown`` is deliberately refused: the token is ``md`` (token == extension).
    ``docx``/``pdf`` are not synchronous formats and do not exist yet on this route.
    """
    media_file = make_redactable_file(db_session, normal_user)
    response = client.get(
        f"/api/files/{media_file.uuid}/export",
        headers=user_token_headers,
        params={"format": fmt},
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST


def test_md_is_served_as_markdown_not_plain_text(
    client, user_token_headers, normal_user, db_session
):
    media_file = make_redactable_file(db_session, normal_user)
    response = client.get(
        f"/api/files/{media_file.uuid}/export",
        headers=user_token_headers,
        params={"format": "md"},
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert response.text.startswith("---\ntitle: ")
    assert "filename*=UTF-8''export673.md" in response.headers["content-disposition"]


def test_export_requires_auth(client, normal_user, db_session):
    media_file = make_redactable_file(db_session, normal_user)
    response = client.get(f"/api/files/{media_file.uuid}/export", params={"format": "txt"})
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
