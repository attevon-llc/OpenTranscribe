"""``GET /speaker-profiles/speakers/{uuid}/suggestions`` runs in the request's tenant (#1027 A).

The endpoint used to take ``get_current_active_user`` and never learn the request's
organization, so every suggestion ran in personal scope. That failed closed for the kNN,
but the profile set it matched against came from an un-gated SQL query, so an org
request's LLM suggestion could resolve to one of the caller's profiles from ANOTHER
tenant. It now takes ``get_current_context`` and threads ``ctx.org_id`` into both.

Two orgs plus personal scope, all owned by one user, so ownership alone cannot tell the
profiles apart: only the tenant gate can. Each "sees nothing from another tenant" case
seeds ONLY the other tenants' profiles, so a missing gate returns one of them instead of
the create-new suggestion — deterministically, not by ``.first()`` ordering luck.
"""

from __future__ import annotations

import uuid as uuid_pkg
from typing import Any

import pytest
from fastapi import status

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerProfile
from app.models.organization import Organization

SUGGESTED = "Quorra Vexlin"


def _org(db_session, label: str) -> Organization:
    org = Organization(name=label, slug=f"{label}-{uuid_pkg.uuid4().hex[:8]}")
    db_session.add(org)
    db_session.commit()
    return org


def _profile(db_session, owner, org_id: int | None, tag: str) -> SpeakerProfile:
    profile = SpeakerProfile(
        uuid=uuid_pkg.uuid4(),
        user_id=owner.id,
        name=f"{SUGGESTED} ({tag})",
        organization_id=org_id,
    )
    db_session.add(profile)
    db_session.commit()
    db_session.refresh(profile)
    return profile


def _llm_suggested_speaker(db_session, owner, org_id: int | None) -> Speaker:
    file_uuid = uuid_pkg.uuid4()
    media_file = MediaFile(
        uuid=file_uuid,
        filename="suggest_probe.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status="completed",
        user_id=owner.id,
        organization_id=org_id,
    )
    db_session.add(media_file)
    db_session.commit()
    speaker = Speaker(
        uuid=uuid_pkg.uuid4(),
        user_id=owner.id,
        media_file_id=media_file.id,
        name="SPEAKER_00",
        suggested_name=SUGGESTED,
        confidence=0.9,
        suggestion_source="llm_analysis",
    )
    db_session.add(speaker)
    db_session.commit()
    db_session.refresh(speaker)
    return speaker


def _llm_suggestion(client, headers, speaker: Speaker) -> dict[str, Any]:
    response = client.get(
        f"/api/speaker-profiles/speakers/{speaker.uuid}/suggestions", headers=headers
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    llm: list[dict[str, Any]] = [s for s in response.json() if s["source"] == "llm_analysis"]
    assert len(llm) == 1, response.json()
    return llm[0]


@pytest.fixture
def two_orgs(db_session):
    return _org(db_session, "tenant-a"), _org(db_session, "tenant-b")


@pytest.mark.parametrize("scope", ["org_a", "org_b", "personal"])
def test_each_scope_is_suggested_its_own_profile(
    scope, client, user_token_headers, normal_user, db_session, org_context, two_orgs
):
    org_a, org_b = two_orgs
    active = {"org_a": org_a.id, "org_b": org_b.id, "personal": None}[scope]
    profiles = {
        "org_a": _profile(db_session, normal_user, org_a.id, "A"),
        "org_b": _profile(db_session, normal_user, org_b.id, "B"),
        "personal": _profile(db_session, normal_user, None, "personal"),
    }
    if active is not None:
        org_context(org_id=active, org_role="org:member", only_for=normal_user.id)
    speaker = _llm_suggested_speaker(db_session, normal_user, active)

    suggestion = _llm_suggestion(client, user_token_headers, speaker)

    assert suggestion["profile_id"] == str(profiles[scope].uuid)


@pytest.mark.parametrize("scope", ["org_a", "org_b", "personal"])
def test_no_scope_is_suggested_another_tenants_profile(
    scope, client, user_token_headers, normal_user, db_session, org_context, two_orgs
):
    """Only OTHER tenants' matching profiles exist: the answer must be "create new"."""
    org_a, org_b = two_orgs
    scopes = {"org_a": org_a.id, "org_b": org_b.id, "personal": None}
    for other, org_id in scopes.items():
        if other != scope:
            _profile(db_session, normal_user, org_id, other)
    active = scopes[scope]
    if active is not None:
        org_context(org_id=active, org_role="org:member", only_for=normal_user.id)
    speaker = _llm_suggested_speaker(db_session, normal_user, active)

    suggestion = _llm_suggestion(client, user_token_headers, speaker)

    assert suggestion["profile_id"] is None
    assert suggestion["create_new"] is True


def test_the_voiceprint_knn_runs_in_the_request_tenant(
    client, user_token_headers, normal_user, db_session, org_context, two_orgs, monkeypatch
):
    """The embedding leg gets the same scope: the org id for its OpenSearch gate, and a
    candidate set that already holds only that org's profiles."""
    from app.services import opensearch_service
    from app.services.profile_embedding_service import ProfileEmbeddingService

    org_a, org_b = two_orgs
    profile_a = _profile(db_session, normal_user, org_a.id, "A")
    _profile(db_session, normal_user, org_b.id, "B")
    _profile(db_session, normal_user, None, "personal")
    org_context(org_id=org_a.id, org_role="org:member", only_for=normal_user.id)
    speaker = _llm_suggested_speaker(db_session, normal_user, org_a.id)

    knn_calls: list[dict] = []

    def _record(embedding, user_id, threshold=0.7, accessible_profile_ids=None, **kwargs):
        knn_calls.append({"profile_ids": accessible_profile_ids, **kwargs})
        return []

    monkeypatch.setattr(opensearch_service, "get_speaker_embedding", lambda _uuid: [0.1] * 8)
    monkeypatch.setattr(
        ProfileEmbeddingService, "calculate_profile_similarity", staticmethod(_record)
    )

    _llm_suggestion(client, user_token_headers, speaker)

    assert knn_calls == [{"profile_ids": {profile_a.id}, "organization_id": org_a.id}]
