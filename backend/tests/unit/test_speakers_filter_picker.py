"""W2.2: ``GET /speakers?for_filter=true`` — the type-to-search picker roster.

Covers the fix to ``_get_unique_speakers_for_filter`` (quarantine exclusion,
which it previously did not apply) and the new server-side ``q``/``limit``/
``profile_id``/``is_profile`` parameters. Rows are created directly against
the DB session the test client shares (`conftest.py`'s ``get_db`` override),
so none of this needs S3/MinIO — unlike ``tests/api/endpoints/test_speakers.py``,
which uploads real media files and is skipped without it.

Permission matrix row T4 lives here: LEAK (the picker excludes both unshared
AND quarantined speakers) and SHARED (the picker lists the owner's speakers
for a genuinely shared file) — asserted against real share rows.
"""

from __future__ import annotations

import datetime as dt
import uuid as uuid_pkg

import pytest

pytestmark = pytest.mark.unit


def _make_file(db, user, *, title="Recording", quarantined=False):
    from app.models.media import MediaFile

    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename=f"{title}.mp4",
        title=title,
        storage_path=f"media/test/{uuid_pkg.uuid4()}.mp4",
        content_type="video/mp4",
        file_size=1000,
        status="completed",
        upload_time=dt.datetime.now(dt.UTC),
        is_quarantined=quarantined,
    )
    db.add(media)
    db.commit()
    db.refresh(media)
    return media


def _share_with(db, owner, recipient, media_file, *, permission="viewer") -> None:
    from app.models.media import Collection
    from app.models.media import CollectionMember
    from app.models.sharing import CollectionShare

    collection = Collection(
        user_id=owner.id, name=f"share-{uuid_pkg.uuid4().hex[:8]}", description="w2.2 test"
    )
    db.add(collection)
    db.commit()
    db.add(CollectionMember(collection_id=collection.id, media_file_id=media_file.id))
    db.add(
        CollectionShare(
            collection_id=collection.id,
            shared_by_id=owner.id,
            target_type="user",
            target_user_id=recipient.id,
            permission=permission,
        )
    )
    db.commit()


def _add_speaker(db, media_file, owner, *, display_name, name="SPEAKER_00", profile_id=None):
    from app.models.media import Speaker

    speaker = Speaker(
        uuid=uuid_pkg.uuid4(),
        user_id=owner.id,
        media_file_id=media_file.id,
        name=name,
        display_name=display_name,
        profile_id=profile_id,
        verified=True,
    )
    db.add(speaker)
    db.commit()
    db.refresh(speaker)
    return speaker


def _names(response_json) -> set[str]:
    return {row["display_name"] for row in response_json}


def test_t4_leak_the_picker_excludes_an_unshared_speaker(
    client, db_session, normal_user, other_user, user_token_headers
):
    unshared = _make_file(db_session, other_user, title="Not shared")
    _add_speaker(db_session, unshared, other_user, display_name="Priya Patel")

    response = client.get("/api/speakers", params={"for_filter": True}, headers=user_token_headers)
    assert response.status_code == 200
    assert "Priya Patel" not in _names(response.json())


def test_t4_leak_the_picker_excludes_a_quarantined_speaker(
    client, db_session, normal_user, user_token_headers
):
    quarantined = _make_file(db_session, normal_user, title="Quarantined", quarantined=True)
    _add_speaker(db_session, quarantined, normal_user, display_name="Quinn Zhao")

    response = client.get("/api/speakers", params={"for_filter": True}, headers=user_token_headers)
    assert response.status_code == 200
    assert "Quinn Zhao" not in _names(response.json())


def test_t4_shared_the_picker_lists_the_owners_speakers_for_a_shared_file(
    client, db_session, normal_user, other_user, user_token_headers
):
    shared = _make_file(db_session, other_user, title="Shared with me")
    _add_speaker(db_session, shared, other_user, display_name="Priya Patel")
    _share_with(db_session, other_user, normal_user, shared)

    response = client.get("/api/speakers", params={"for_filter": True}, headers=user_token_headers)
    assert response.status_code == 200
    row = next(r for r in response.json() if r["display_name"] == "Priya Patel")
    assert row["media_count"] == 1


def test_q_narrows_to_a_substring_case_insensitively(
    client, db_session, normal_user, user_token_headers
):
    media = _make_file(db_session, normal_user)
    _add_speaker(db_session, media, normal_user, display_name="Priya Patel", name="SPEAKER_00")
    _add_speaker(db_session, media, normal_user, display_name="Quinn Zhao", name="SPEAKER_01")

    response = client.get(
        "/api/speakers", params={"for_filter": True, "q": "priy"}, headers=user_token_headers
    )
    assert response.status_code == 200
    assert _names(response.json()) == {"Priya Patel"}


def test_limit_bounds_the_page_size(client, db_session, normal_user, user_token_headers):
    media = _make_file(db_session, normal_user)
    for i in range(5):
        _add_speaker(
            db_session, media, normal_user, display_name=f"Speaker {i}", name=f"SPEAKER_{i:02d}"
        )

    response = client.get(
        "/api/speakers", params={"for_filter": True, "limit": 2}, headers=user_token_headers
    )
    assert response.status_code == 200
    assert len(response.json()) == 2


def test_is_profile_true_restricts_to_profile_linked_speakers(
    client, db_session, normal_user, user_token_headers
):
    from app.models.media import SpeakerProfile

    profile = SpeakerProfile(uuid=uuid_pkg.uuid4(), user_id=normal_user.id, name="Priya")
    db_session.add(profile)
    db_session.commit()
    db_session.refresh(profile)

    media = _make_file(db_session, normal_user)
    _add_speaker(
        db_session,
        media,
        normal_user,
        display_name="Priya Patel",
        name="SPEAKER_00",
        profile_id=profile.id,
    )
    _add_speaker(db_session, media, normal_user, display_name="Quinn Zhao", name="SPEAKER_01")

    response = client.get(
        "/api/speakers",
        params={"for_filter": True, "is_profile": True},
        headers=user_token_headers,
    )
    assert response.status_code == 200
    assert _names(response.json()) == {"Priya Patel"}


def test_is_profile_false_restricts_to_unlinked_speakers(
    client, db_session, normal_user, user_token_headers
):
    from app.models.media import SpeakerProfile

    profile = SpeakerProfile(uuid=uuid_pkg.uuid4(), user_id=normal_user.id, name="Priya")
    db_session.add(profile)
    db_session.commit()
    db_session.refresh(profile)

    media = _make_file(db_session, normal_user)
    _add_speaker(
        db_session,
        media,
        normal_user,
        display_name="Priya Patel",
        name="SPEAKER_00",
        profile_id=profile.id,
    )
    _add_speaker(db_session, media, normal_user, display_name="Quinn Zhao", name="SPEAKER_01")

    response = client.get(
        "/api/speakers",
        params={"for_filter": True, "is_profile": False},
        headers=user_token_headers,
    )
    assert response.status_code == 200
    assert _names(response.json()) == {"Quinn Zhao"}


def test_profile_id_filters_to_a_specific_profile(
    client, db_session, normal_user, user_token_headers
):
    from app.models.media import SpeakerProfile

    wanted = SpeakerProfile(uuid=uuid_pkg.uuid4(), user_id=normal_user.id, name="Priya")
    other = SpeakerProfile(uuid=uuid_pkg.uuid4(), user_id=normal_user.id, name="Quinn")
    db_session.add_all([wanted, other])
    db_session.commit()
    db_session.refresh(wanted)
    db_session.refresh(other)

    media = _make_file(db_session, normal_user)
    _add_speaker(
        db_session,
        media,
        normal_user,
        display_name="Priya Patel",
        name="SPEAKER_00",
        profile_id=wanted.id,
    )
    _add_speaker(
        db_session,
        media,
        normal_user,
        display_name="Quinn Zhao",
        name="SPEAKER_01",
        profile_id=other.id,
    )

    response = client.get(
        "/api/speakers",
        params={"for_filter": True, "profile_id": str(wanted.uuid)},
        headers=user_token_headers,
    )
    assert response.status_code == 200
    assert _names(response.json()) == {"Priya Patel"}


def test_profile_id_belonging_to_another_user_resolves_to_empty(
    client, db_session, normal_user, other_user, user_token_headers
):
    from app.models.media import SpeakerProfile

    someone_elses = SpeakerProfile(uuid=uuid_pkg.uuid4(), user_id=other_user.id, name="Not yours")
    db_session.add(someone_elses)
    db_session.commit()
    db_session.refresh(someone_elses)

    media = _make_file(db_session, normal_user)
    _add_speaker(db_session, media, normal_user, display_name="Priya Patel", name="SPEAKER_00")

    response = client.get(
        "/api/speakers",
        params={"for_filter": True, "profile_id": str(someone_elses.uuid)},
        headers=user_token_headers,
    )
    assert response.status_code == 200
    assert response.json() == []


def test_t4_leak_the_general_listing_excludes_a_quarantined_speaker(
    client, db_session, normal_user, user_token_headers
):
    """Adversarial-review follow-up: T4's quarantine check was applied only to
    ``_get_unique_speakers_for_filter`` (``for_filter=True``). The GENERAL
    listing (``GET /speakers`` with no ``for_filter``/``file_uuid``) scoped
    only by ``Speaker.user_id == current_user.id`` with no quarantine
    exclusion at all — so a caller's OWN quarantined file's speakers stayed
    fully visible through the default listing, even though the file itself
    404s everywhere else."""
    quarantined = _make_file(db_session, normal_user, title="Quarantined", quarantined=True)
    _add_speaker(db_session, quarantined, normal_user, display_name="Quinn Zhao")
    visible = _make_file(db_session, normal_user, title="Visible")
    _add_speaker(db_session, visible, normal_user, display_name="Priya Patel")

    response = client.get("/api/speakers", headers=user_token_headers)

    assert response.status_code == 200
    names = {row["display_name"] for row in response.json()}
    assert "Quinn Zhao" not in names
    assert "Priya Patel" in names
