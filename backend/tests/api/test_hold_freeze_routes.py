"""A held file's content is frozen: every content write route refuses it (plan §4.1, W1-W45).

Each case sends one write to a file under a legal hold and expects ``409 FILE_UNDER_LEGAL_HOLD``
with the fixed freeze text, the row unchanged, and exactly one ``content.hold.refused`` audit
event whose ``action`` is the case's pinned action. The control sends the same request to the
same world with the file NOT held and expects it to get past the guard (for the plain writes,
to succeed): a refusal that fired for any other reason would pass the first half alone.

Cross-file operations (a profile rename, a cluster promote / unassign / batch verify) do not
refuse: they leave the held file's speaker as it was and report ``held_skipped`` (plan §3.5).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any
from typing import cast
from unittest.mock import patch

import pytest
from fastapi import status
from starlette.requests import Request

from app.auth.audit import AuditEventType
from app.auth.audit import audit_logger
from app.models.collection import Collection
from app.models.collection import CollectionMember
from app.models.media import Comment
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.models.speaker import SpeakerCluster
from app.models.speaker import SpeakerClusterMember
from app.models.speaker import SpeakerProfile
from app.models.tag import FileTag
from app.models.tag import Tag
from app.models.topic import TopicSuggestion
from app.services.hold_guard import FREEZE_REFUSAL
from app.services.hold_guard import LEGAL_HOLD_ERROR_CODE
from app.services.hold_guard import REPROCESS_REFUSAL
from app.services.tag_service import normalize_tag_name


@dataclass
class World:
    file: MediaFile
    seg: TranscriptSegment
    sp1: Speaker
    sp2: Speaker
    comment: Comment
    tag: Tag
    tag2: Tag
    col: Collection
    col2: Collection
    profile: SpeakerProfile
    cluster: SpeakerCluster


def _world(db, owner, *, held: bool) -> World:
    file_uuid = str(uuid.uuid4())
    mf = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename="held.wav",
        title="Original title",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status="completed",
        summary_status="failed",
        legal_hold=held,
    )
    db.add(mf)
    db.flush()
    profile = SpeakerProfile(
        uuid=uuid.uuid4(), user_id=owner.id, name=f"Profile {uuid.uuid4().hex[:8]}"
    )
    db.add(profile)
    db.flush()
    sp1 = Speaker(
        user_id=owner.id,
        media_file_id=mf.id,
        name="SPEAKER_00",
        display_name="Held Name",
        profile_id=profile.id,
    )
    sp2 = Speaker(user_id=owner.id, media_file_id=mf.id, name="SPEAKER_01")
    db.add_all([sp1, sp2])
    db.flush()
    seg = TranscriptSegment(
        media_file_id=mf.id, speaker_id=sp1.id, start_time=0.0, end_time=1.0, text="kept"
    )
    comment = Comment(media_file_id=mf.id, user_id=owner.id, text="original comment")
    suffix = uuid.uuid4().hex[:8]
    tag = Tag(name=f"held-tag-{suffix}", user_id=owner.id, source="manual")
    tag2 = Tag(name=f"other-tag-{suffix}", user_id=owner.id, source="manual")
    col = Collection(name=f"col-{suffix}", user_id=owner.id)
    col2 = Collection(name=f"col2-{suffix}", user_id=owner.id)
    cluster = SpeakerCluster(user_id=owner.id, member_count=1)
    db.add_all([seg, comment, tag, tag2, col, col2, cluster])
    db.flush()
    db.add_all(
        [
            FileTag(media_file_id=mf.id, tag_id=tag.id, source="manual"),
            CollectionMember(collection_id=col.id, media_file_id=mf.id),
            SpeakerClusterMember(cluster_id=cluster.id, speaker_id=sp1.id, confidence=0.9),
            TopicSuggestion(media_file_id=mf.id, user_id=owner.id, status="pending"),
        ]
    )
    sp1.cluster_id = cluster.id
    db.commit()
    for row in (mf, seg, sp1, sp2, comment, tag, tag2, col, col2, profile, cluster):
        db.refresh(row)
    return World(mf, seg, sp1, sp2, comment, tag, tag2, col, col2, profile, cluster)


@pytest.fixture()
def events(monkeypatch) -> list[dict]:
    written: list[dict] = []
    monkeypatch.setattr(audit_logger, "log", lambda **kw: written.append(kw))
    return written


def _refusals(events: list[dict], action: str) -> list[dict]:
    return [
        e
        for e in events
        if e["event_type"] is AuditEventType.CONTENT_HOLD_REFUSED
        and e["details"]["action"] == action
    ]


def _is_hold_refusal(response) -> bool:
    if response.status_code != status.HTTP_409_CONFLICT:
        return False
    detail = response.json().get("detail")
    return isinstance(detail, dict) and detail.get("error") == LEGAL_HOLD_ERROR_CODE


@dataclass(frozen=True)
class Case:
    action: str
    method: str
    #: Builds the URL from the world.
    url: Any
    body: Any = None
    #: The control (an unheld file) must succeed, not only get past the guard.
    succeeds: bool = False
    message: str = FREEZE_REFUSAL


def _f(w: World) -> str:
    return str(w.file.uuid)


CASES: dict[str, Case] = {
    # W1
    "segment_edit": Case(
        "segment_edit",
        "PUT",
        lambda w: f"/api/files/{_f(w)}/transcript/segments/{w.seg.uuid}",
        lambda w: {"text": "changed"},
        succeeds=True,
    ),
    # W2
    "segment_speaker": Case(
        "segment_speaker",
        "PUT",
        lambda w: f"/api/transcripts/segments/{w.seg.uuid}/speaker",
        lambda w: {"speaker_uuid": str(w.sp2.uuid)},
    ),
    # W3
    "speaker_merge": Case(
        "speaker_merge", "POST", lambda w: f"/api/speakers/{w.sp2.uuid}/merge/{w.sp1.uuid}"
    ),
    # W4
    "speaker_update": Case(
        "speaker_update",
        "PUT",
        lambda w: f"/api/speakers/{w.sp2.uuid}",
        lambda w: {"display_name": "Changed Name"},
    ),
    # W5
    "speaker_create": Case(
        "speaker_create",
        "POST",
        lambda w: f"/api/speakers?media_file_uuid={_f(w)}",
        lambda w: {"name": "SPEAKER_09"},
        succeeds=True,
    ),
    # W6
    "speaker_verify": Case(
        "speaker_verify", "POST", lambda w: f"/api/speakers/{w.sp2.uuid}/verify?action=reject"
    ),
    # W7
    "speaker_gender": Case(
        "speaker_gender",
        "POST",
        lambda w: f"/api/speakers/{w.sp2.uuid}/confirm-gender?gender=male",
        succeeds=True,
    ),
    # W8
    "speaker_link": Case(
        "speaker_link",
        "POST",
        lambda w: (
            f"/api/speaker-profiles/speakers/{w.sp2.uuid}/assign-profile"
            f"?profile_uuid={w.profile.uuid}"
        ),
    ),
    # W9
    "file_update": Case(
        "file_update",
        "PUT",
        lambda w: f"/api/files/{_f(w)}",
        lambda w: {"title": "Changed"},
        succeeds=True,
    ),
    # W10-W12
    "summary_generate": Case("summary_generate", "POST", lambda w: f"/api/files/{_f(w)}/summarize"),
    "identify_speakers": Case(
        "identify_speakers", "POST", lambda w: f"/api/files/{_f(w)}/identify-speakers"
    ),
    "summary_retry": Case(
        "summary_generate", "POST", lambda w: f"/api/files/{_f(w)}/retry-summary"
    ),
    # W13-W17
    "topic_extract": Case("topic_extract", "POST", lambda w: f"/api/files/{_f(w)}/extract"),
    "topic_apply": Case(
        "topic_apply",
        "POST",
        lambda w: f"/api/files/{_f(w)}/apply",
        lambda w: {"accepted_tags": ["x"], "accepted_collections": []},
    ),
    "topic_dismiss": Case(
        "topic_dismiss", "DELETE", lambda w: f"/api/files/{_f(w)}/suggestions", succeeds=True
    ),
    "auto_label": Case("auto_label", "POST", lambda w: f"/api/files/{_f(w)}/auto-label"),
    "batch_extract": Case(
        "topic_extract",
        "POST",
        lambda w: "/api/files/batch-extract",
        lambda w: {"file_uuids": [_f(w)]},
    ),
    # W19-W23
    "tag_add": Case(
        "tag_add",
        "POST",
        lambda w: f"/api/tags/files/{_f(w)}/tags",
        lambda w: {"name": "added-tag"},
        succeeds=True,
    ),
    "tag_remove": Case(
        "tag_remove",
        "DELETE",
        lambda w: f"/api/tags/files/{_f(w)}/tags/{w.tag.name}",
        succeeds=True,
    ),
    "tag_merge": Case(
        "tag_merge",
        "POST",
        lambda w: f"/api/tags/{w.tag2.uuid}/merge",
        lambda w: {"source_uuids": [str(w.tag.uuid)]},
        succeeds=True,
    ),
    "tag_rename_merge": Case(
        "tag_merge",
        "PATCH",
        lambda w: f"/api/tags/{w.tag.uuid}",
        lambda w: {"name": w.tag2.name, "confirm_merge": True},
        succeeds=True,
    ),
    "tag_delete": Case(
        "tag_delete", "DELETE", lambda w: f"/api/tags?tag_uuids={w.tag.uuid}", succeeds=True
    ),
    # W24-W26
    "collection_add": Case(
        "collection_add",
        "POST",
        lambda w: f"/api/collections/{w.col2.uuid}/media",
        lambda w: {"media_file_ids": [_f(w)]},
        succeeds=True,
    ),
    "remove_from_collection": Case(
        "remove_from_collection",
        "DELETE",
        lambda w: f"/api/collections/{w.col.uuid}/media",
        lambda w: {"media_file_ids": [_f(w)]},
        succeeds=True,
    ),
    "delete_collection": Case(
        "delete_collection", "DELETE", lambda w: f"/api/collections/{w.col.uuid}", succeeds=True
    ),
    # W27, W28
    "comment_edit": Case(
        "comment_edit",
        "PUT",
        lambda w: f"/api/comments/{w.comment.uuid}",
        lambda w: {"text": "edited"},
        succeeds=True,
    ),
    "comment_delete": Case(
        "comment_delete", "DELETE", lambda w: f"/api/comments/{w.comment.uuid}", succeeds=True
    ),
    # W33-W35
    "recover": Case(
        "recover", "POST", lambda w: f"/api/files/{_f(w)}/recover", message=REPROCESS_REFUSAL
    ),
    "retry_tasks": Case(
        "retry", "POST", lambda w: f"/api/tasks/retry/{_f(w)}", message=REPROCESS_REFUSAL
    ),
    "retry_my_files": Case(
        "retry", "POST", lambda w: f"/api/my-files/{_f(w)}/retry", message=REPROCESS_REFUSAL
    ),
    # W37
    "profile_delete": Case(
        "profile_delete",
        "DELETE",
        lambda w: f"/api/speaker-profiles/profiles/{w.profile.uuid}",
    ),
}


def _send(client, headers, case: Case, w: World):
    body = case.body(w) if case.body else None
    return client.request(case.method, case.url(w), json=body, headers=headers)


def _snapshot(db, w: World) -> dict[str, Any]:
    """The frozen facts of the world's file, read fresh from the database."""
    db.expire_all()
    file = db.get(MediaFile, w.file.id)
    return {
        "title": file.title,
        "segment": db.get(TranscriptSegment, w.seg.id).text,
        "segment_speaker": db.get(TranscriptSegment, w.seg.id).speaker_id,
        "speakers": sorted(
            (s.name, s.display_name, s.profile_id, s.verified, s.predicted_gender)
            for s in db.query(Speaker).filter(Speaker.media_file_id == w.file.id)
        ),
        "comments": sorted(c.text for c in db.query(Comment).filter_by(media_file_id=w.file.id)),
        "tags": sorted(
            t for (t,) in db.query(FileTag.tag_id).filter(FileTag.media_file_id == w.file.id)
        ),
        "collections": sorted(
            c
            for (c,) in db.query(CollectionMember.collection_id).filter(
                CollectionMember.media_file_id == w.file.id
            )
        ),
        "profile": db.get(SpeakerProfile, w.profile.id) is not None,
        "summary_status": file.summary_status,
    }


@pytest.mark.parametrize("name", sorted(CASES))
def test_a_write_to_a_held_file_is_refused(
    name, client, user_token_headers, normal_user, db_session, events
):
    case = CASES[name]
    w = _world(db_session, normal_user, held=True)
    before = _snapshot(db_session, w)

    response = _send(client, user_token_headers, case, w)

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    detail = response.json()["detail"]
    assert detail["error"] == LEGAL_HOLD_ERROR_CODE
    assert detail["held_by"] == ["platform"]
    assert detail["message"] == case.message
    assert "holds" not in detail  # an editor is never given the hold uuids
    assert _snapshot(db_session, w) == before
    refused = _refusals(events, case.action)
    assert len(refused) == 1, events
    assert refused[0]["resource_uuid"] == _f(w)
    assert refused[0]["details"] == {"action": case.action, "held_by": ["platform"]}


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_same_write_to_an_unheld_file_is_not_a_hold_refusal(
    name, client, user_token_headers, normal_user, db_session, events
):
    case = CASES[name]
    w = _world(db_session, normal_user, held=False)

    response = _send(client, user_token_headers, case, w)

    assert not _is_hold_refusal(response), response.text
    assert not _refusals(events, case.action)
    if case.succeeds:
        assert response.status_code < 400, response.text


# ---------------------------------------------------------------------------
# The bulk rail (W29-W32, W50): the held file's row carries the code; the batch carries on.
# ---------------------------------------------------------------------------

_BULK = {
    "summarize": ("summary_generate", FREEZE_REFUSAL),
    "identify_speakers": ("identify_speakers", FREEZE_REFUSAL),
    "redact": ("redaction_rescan", FREEZE_REFUSAL),
    "add_tag": ("tag_add", FREEZE_REFUSAL),
    "remove_tag": ("tag_remove", FREEZE_REFUSAL),
    "recover": ("recover", REPROCESS_REFUSAL),
    "retry": ("retry", REPROCESS_REFUSAL),
    "reprocess": ("reprocess", REPROCESS_REFUSAL),
}


@pytest.mark.parametrize("verb", sorted(_BULK))
def test_a_bulk_verb_refuses_the_held_file_only(
    verb, client, user_token_headers, normal_user, db_session, events
):
    action, message = _BULK[verb]
    held = _world(db_session, normal_user, held=True)
    free = _world(db_session, normal_user, held=False)
    before = _snapshot(db_session, held)
    # Redaction off is a batch-wide refusal for any file that reaches the scan; a held file
    # never does, so it is sent alone.
    uuids = [_f(held)] if verb == "redact" else [_f(held), _f(free)]
    body: dict[str, Any] = {"file_uuids": uuids, "action": verb}
    if verb in ("add_tag", "remove_tag"):
        body["tag_name"] = held.tag.name

    response = client.post(
        "/api/files/management/bulk-action", json=body, headers=user_token_headers
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    rows = {r["file_uuid"]: r for r in response.json()}
    assert rows[_f(held)]["success"] is False
    assert rows[_f(held)]["error"] == LEGAL_HOLD_ERROR_CODE
    assert rows[_f(held)]["message"] == message
    if _f(free) in rows:
        assert rows[_f(free)]["error"] != LEGAL_HOLD_ERROR_CODE
    assert _snapshot(db_session, held) == before
    refused = _refusals(events, action)
    assert [e["resource_uuid"] for e in refused] == [_f(held)]


# ---------------------------------------------------------------------------
# Cross-file operations (plan §3.5): the held row is left as it was and counted.
# ---------------------------------------------------------------------------


def test_a_profile_rename_leaves_a_held_files_speaker_named_as_it_was(
    client, user_token_headers, normal_user, db_session
):
    held = _world(db_session, normal_user, held=True)
    free = _world(db_session, normal_user, held=False)
    free.sp1.profile_id = held.profile.id
    db_session.commit()

    response = client.put(
        f"/api/speaker-profiles/profiles/{held.profile.uuid}?name=Renamed%20Person",
        headers=user_token_headers,
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["held_skipped"] == 1
    db_session.expire_all()
    assert db_session.get(Speaker, held.sp1.id).display_name == "Held Name"
    assert db_session.get(Speaker, free.sp1.id).display_name == "Renamed Person"


def test_a_profile_gender_confirm_leaves_a_held_files_speaker_as_it_was(
    client, user_token_headers, normal_user, db_session
):
    held = _world(db_session, normal_user, held=True)
    free = _world(db_session, normal_user, held=False)
    free.sp1.profile_id = held.profile.id
    db_session.commit()

    response = client.post(
        f"/api/speaker-profiles/profiles/{held.profile.uuid}/confirm-gender?gender=female",
        headers=user_token_headers,
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["updated_count"] == 1
    db_session.expire_all()
    assert db_session.get(Speaker, held.sp1.id).predicted_gender is None
    assert db_session.get(Speaker, free.sp1.id).predicted_gender == "female"


def test_a_cluster_batch_verify_skips_a_held_files_speaker(
    client, user_token_headers, normal_user, db_session
):
    held = _world(db_session, normal_user, held=True)
    free = _world(db_session, normal_user, held=False)

    response = client.post(
        "/api/speaker-clusters/batch-verify",
        json={
            "speaker_uuids": [str(held.sp2.uuid), str(free.sp2.uuid)],
            "action": "name",
            "display_name": "Batch Name",
        },
        headers=user_token_headers,
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["held_skipped"] == 1
    assert body["updated_count"] == 1
    db_session.expire_all()
    assert db_session.get(Speaker, held.sp2.id).display_name is None
    assert db_session.get(Speaker, free.sp2.id).display_name == "Batch Name"


def test_a_cluster_unassign_leaves_a_held_files_speaker_in_place(
    client, user_token_headers, normal_user, db_session
):
    held = _world(db_session, normal_user, held=True)

    response = client.post(
        f"/api/speaker-clusters/{held.cluster.uuid}/unassign",
        json={"speaker_uuids": [str(held.sp1.uuid)]},
        headers=user_token_headers,
    )

    assert response.status_code >= 400, response.text  # nothing left to unassign
    db_session.expire_all()
    speaker = db_session.get(Speaker, held.sp1.id)
    assert (speaker.cluster_id, speaker.profile_id, speaker.display_name) == (
        held.cluster.id,
        held.profile.id,
        "Held Name",
    )


def test_a_cluster_promote_does_not_relink_a_held_files_speaker(
    client, user_token_headers, normal_user, db_session
):
    held = _world(db_session, normal_user, held=True)
    held.sp1.profile_id = None
    db_session.commit()

    # The derived-copy rebuild needs OpenSearch; what is asserted is the Postgres side.
    with patch("app.services.clustering.cluster_identity.raise_if_incomplete"):
        response = client.post(
            f"/api/speaker-clusters/{held.cluster.uuid}/promote",
            json={"name": "Promoted Person"},
            headers=user_token_headers,
        )

    assert response.status_code == status.HTTP_200_OK, response.text
    db_session.expire_all()
    speaker = db_session.get(Speaker, held.sp1.id)
    assert (speaker.profile_id, speaker.display_name) == (None, "Held Name")


def test_promoting_a_tag_that_would_fold_a_held_files_tag_is_refused(
    client, admin_token_headers, admin_user, db_session, events
):
    """Publishing a tag that a shared tag of the same name already exists for folds it into
    that one, re-pointing every file that carried it (W23)."""
    held = _world(db_session, admin_user, held=True)
    shared = Tag(
        name=held.tag.name,
        normalized_name=normalize_tag_name(held.tag.name),
        user_id=None,
        organization_id=None,
        source="manual",
    )
    db_session.add(shared)
    db_session.commit()
    before = _snapshot(db_session, held)

    response = client.post(
        "/api/tags/promote",
        json={"tag_uuids": [str(held.tag.uuid)]},
        headers=admin_token_headers,
    )

    assert _is_hold_refusal(response), response.text
    assert _snapshot(db_session, held) == before
    db_session.expire_all()
    assert db_session.get(Tag, held.tag.id) is not None, "the held file's tag was not folded"
    refused = _refusals(events, "tag_merge")
    assert [e["resource_uuid"] for e in refused] == [_f(held)]
    assert refused[0]["details"] == {"action": "tag_merge", "held_by": ["platform"]}


def test_a_hold_refusal_no_route_caught_is_still_a_409_with_the_freeze_text():
    """The backstop handler for a ``HoldRefusedError`` that escapes a service (no audit)."""
    import asyncio
    import json

    from app.api.hold_refusal import hold_refused_handler
    from app.main import app
    from app.services.hold_guard import HoldRefusedError
    from app.services.hold_guard import HoldState

    assert app.exception_handlers[HoldRefusedError] is hold_refused_handler
    exc = HoldRefusedError({7: HoldState(platform_flag=False, org_hold_uuids=("h-1",))})

    # The handler never reads the request, so only the exception matters here.
    response = asyncio.run(hold_refused_handler(cast("Request", None), exc))

    assert response.status_code == status.HTTP_409_CONFLICT
    assert json.loads(bytes(response.body)) == {
        "detail": {
            "error": LEGAL_HOLD_ERROR_CODE,
            "message": FREEZE_REFUSAL,
            "held_by": ["organization"],
        }
    }
