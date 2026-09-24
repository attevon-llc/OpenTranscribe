"""Regression coverage for issue #988: real read surfaces must actually thread
``organization_id`` into ``resolve_effective_config``, not just accept the parameter.

Issue #982/#987 built and unit-tested the org-aware redaction-floor resolver seam
(``set_redaction_floor_resolver`` / ``resolve_redaction_floor``) and wired it into
``resolve_effective_config``'s optional ``organization_id`` parameter. But every
production call site kept calling it with the two-argument form, so a registered
resolver was never actually consulted from a real request — #987's own unit tests
(``tests/redaction/test_config_resolution.py::TestTenantRedactionFloor``) only prove
the resolver function works in isolation; nothing proved a real HTTP read carried an
org id into it at all.

This file drives ``GET /api/files/{uuid}/subtitles`` (``files/subtitles.py``'s
``_resolve_subtitle_redaction``) end to end over HTTP, through the real
``get_current_context`` dependency chain (``tests/api/conftest.py``'s ``org_context``
fixture fakes only the identity-provider step, exactly as the org-admin HTTP suites
do), with a per-org redaction floor registered that forces a category the requesting
user's own preferences leave off. Before the fix, the floor is silently never
consulted (``ctx.org_id`` never reaches ``resolve_effective_config``) and the export
ships the sensitive word raw; after the fix it is masked.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from fastapi import status

from app.core import constants as C  # noqa: N812
from app.core.tenant_limits import RedactionFloor
from app.core.tenant_limits import reset_resolvers
from app.core.tenant_limits import set_redaction_floor_resolver
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.organization import Organization
from app.models.organization import OrganizationMembership

FORCED_WORD = "acquisition"
CLEAN_TEXT = "the quarterly numbers look fine"
SENSITIVE_TEXT = f"we should discuss the {FORCED_WORD} before the board meets"


@pytest.fixture(autouse=True)
def _reset_tenant_resolvers():
    """The floor resolver is process-global; leaking one poisons later tests."""
    reset_resolvers()
    yield
    reset_resolvers()


def _set_prefs(db_session, user, **prefs: str) -> None:
    from app import models

    for key, value in prefs.items():
        db_session.add(models.UserSetting(user_id=user.id, setting_key=key, setting_value=value))
    db_session.flush()


def _org(db_session, user, *, role: str = "org:member") -> Organization:
    """A real org row + membership — the file's ``organization_id`` FK needs the
    former, and ``org_context`` fakes only ``resolve_org_context``'s identity-
    provider step, not the membership-mirror lookup ``scope_to_context`` relies
    on elsewhere in this same request path.
    """
    org = Organization(name="acme", slug=f"acme-{uuid_pkg.uuid4().hex[:8]}")
    db_session.add(org)
    db_session.flush()
    db_session.add(OrganizationMembership(organization_id=org.id, user_id=user.id, role=role))
    db_session.commit()
    db_session.refresh(org)
    return org


def _make_org_file(db_session, owner, org: Organization) -> MediaFile:
    """A completed, already-scanned file stamped into ``org``'s tenant scope."""
    file_uuid = str(uuid_pkg.uuid4())
    start = SENSITIVE_TEXT.find(FORCED_WORD)
    media_file = MediaFile(
        uuid=file_uuid,
        filename="org_floor.wav",
        title="org_floor",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status="completed",
        is_public=False,
        user_id=owner.id,
        organization_id=org.id,
        redaction_status=C.REDACTION_STATUS_DONE,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)

    db_session.add(
        TranscriptSegment(
            media_file_id=media_file.id, start_time=0.0, end_time=1.5, text=CLEAN_TEXT
        )
    )
    db_session.add(
        TranscriptSegment(
            media_file_id=media_file.id,
            start_time=1.5,
            end_time=3.0,
            text=SENSITIVE_TEXT,
            redactions=[
                {
                    "char_start": start,
                    "char_end": start + len(FORCED_WORD),
                    "category": "custom",
                    "entity_type": "CUSTOM",
                    "detector": "wordlist",
                    "confidence": 1.0,
                }
            ],
        )
    )
    db_session.commit()


def test_org_redaction_floor_masks_a_word_the_users_own_prefs_leave_off(
    client, user_token_headers, normal_user, db_session, org_context
):
    """The whole point of #982/#987/#988 together: a tenant floor a user never
    opted into still masks their export, once the request actually carries an
    org id into ``resolve_effective_config``.

    ``normal_user`` explicitly disables redaction. With no org context this
    export must ship ``FORCED_WORD`` raw (pinned by the sibling assertion
    below) — the org floor must be the ONLY thing that changes the outcome.
    """
    org = _org(db_session, normal_user)
    _set_prefs(db_session, normal_user, redaction_enabled="false")
    _make_org_file(db_session, normal_user, org)
    media_file = db_session.query(MediaFile).filter(MediaFile.user_id == normal_user.id).one()

    def _resolver(db, organization_id):
        if organization_id == org.id:
            return RedactionFloor(forced_categories=frozenset({"custom"}))
        return None

    set_redaction_floor_resolver(_resolver)
    org_context(org_id=org.id, org_role="org:member", only_for=normal_user.id)

    response = client.get(
        f"/api/files/{media_file.uuid}/subtitles",
        headers=user_token_headers,
        params={"subtitle_format": "srt"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert CLEAN_TEXT in response.text
    assert FORCED_WORD not in response.text, (
        "the org's redaction floor was registered but never reached "
        "resolve_effective_config — organization_id was not threaded through "
        "the subtitle export's call site"
    )


def test_without_org_context_the_same_users_export_is_unmasked(
    client, user_token_headers, normal_user, db_session
):
    """Control: proves the fixture setup above is real — absent org context (and
    the floor it carries), this user's own disabled preference ships the word raw.

    Personal scope (no org at all), so no org-membership FK is needed here.
    """
    org = _org(db_session, normal_user)  # membership only; org_context is never installed
    _set_prefs(db_session, normal_user, redaction_enabled="false")
    _make_org_file(db_session, normal_user, org)
    media_file = db_session.query(MediaFile).filter(MediaFile.user_id == normal_user.id).one()
    # No org context installed and no resolver registered — with `ctx.org_id` None
    # (personal scope) the file must be org-less to stay visible under that scope.
    media_file.organization_id = None
    db_session.commit()

    response = client.get(
        f"/api/files/{media_file.uuid}/subtitles",
        headers=user_token_headers,
        params={"subtitle_format": "srt"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert FORCED_WORD in response.text
