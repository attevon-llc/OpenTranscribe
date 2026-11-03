"""``speaker_profile.last_interaction_at``: what moves it and what must not (v450)."""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime

import pytest
from sqlalchemy import text

from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerProfile
from app.services.speaker_profile_activity import touch_profile_interaction

_OLD = datetime(2020, 1, 1, tzinfo=UTC)


def _profile(db, user) -> SpeakerProfile:
    p = SpeakerProfile(user_id=user.id, name=f"act-{uuid_pkg.uuid4().hex[:8]}")
    db.add(p)
    db.flush()
    db.execute(
        text("UPDATE speaker_profile SET last_interaction_at = :t WHERE id = :i"),
        {"t": _OLD, "i": p.id},
    )
    return p


def _speaker(db, user) -> Speaker:
    f = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename="act.wav",
        storage_path="",
        content_type="audio/wav",
        file_size=1,
        status=FileStatus.COMPLETED,
    )
    db.add(f)
    db.flush()
    s = Speaker(media_file_id=f.id, user_id=user.id, name="SPEAKER_00")
    db.add(s)
    db.flush()
    return s


def _stamp(db, profile) -> datetime:
    stamp: datetime = db.execute(
        text("SELECT last_interaction_at FROM speaker_profile WHERE id = :i"), {"i": profile.id}
    ).scalar()
    return stamp


def test_a_new_profile_starts_its_clock(db_session, normal_user):
    p = SpeakerProfile(user_id=normal_user.id, name=f"new-{uuid_pkg.uuid4().hex[:8]}")
    db_session.add(p)
    db_session.flush()
    stamp = _stamp(db_session, p)
    now = db_session.execute(text("SELECT now()")).scalar_one()
    assert stamp == now  # the transaction's now(): the creation statement set it


def test_touch_sets_now_and_ignores_none(db_session, normal_user):
    p = _profile(db_session, normal_user)
    touch_profile_interaction(db_session, [p.id, None], "human_edit")
    assert _stamp(db_session, p) > _OLD


def test_changing_or_removing_an_avatar_moves_the_clock(db_session, normal_user):
    """The avatar routes stamp through ``swap_avatar_path`` (see test_profile_interaction_sites)."""
    from app.services.speaker_avatar_service import swap_avatar_path

    p = _profile(db_session, normal_user)
    swap_avatar_path(db_session, int(p.id), "avatars/new.png")
    after_upload = _stamp(db_session, p)
    assert after_upload > _OLD

    db_session.execute(
        text("UPDATE speaker_profile SET last_interaction_at = :t WHERE id = :i"),
        {"t": _OLD, "i": p.id},
    )
    previous = swap_avatar_path(db_session, int(p.id), None)
    assert previous == "avatars/new.png"
    assert _stamp(db_session, p) > _OLD


def test_an_unknown_kind_is_refused(db_session):
    with pytest.raises(ValueError):
        touch_profile_interaction(db_session, 1, "viewed")


def test_a_human_assign_moves_the_clock(db_session, normal_user):
    from app.services.speaker_matching_service import SpeakerMatchingService

    p = _profile(db_session, normal_user)
    s = _speaker(db_session, normal_user)
    SpeakerMatchingService(db_session, None).assign_speaker_to_profile(
        int(s.id), int(p.id), user_id=int(normal_user.id)
    )
    assert _stamp(db_session, p) > _OLD


def test_listing_profiles_does_not_move_the_clock(
    client, db_session, normal_user, user_token_headers
):
    p = _profile(db_session, normal_user)
    db_session.commit()
    r = client.get("/api/speaker-profiles/profiles", headers=user_token_headers)
    assert r.status_code == 200, r.text
    assert any(row.get("uuid") == str(p.uuid) or row.get("id") == str(p.uuid) for row in r.json())
    db_session.expire_all()
    assert _stamp(db_session, p) == _OLD


def test_an_embedding_recompute_does_not_move_the_clock(db_session, normal_user):
    from app.services.profile_embedding_service import ProfileEmbeddingService

    p = _profile(db_session, normal_user)
    ProfileEmbeddingService.update_profile_embedding(db_session, int(p.id))
    db_session.expire_all()
    row = db_session.get(SpeakerProfile, p.id)
    assert row is not None and row.last_embedding_update is not None  # the recompute ran
    assert _stamp(db_session, p) == _OLD
