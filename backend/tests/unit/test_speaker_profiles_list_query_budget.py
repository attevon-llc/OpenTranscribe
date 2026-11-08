"""``GET /speaker-profiles/profiles``: statements do not grow with the owners shared in (#1337 P5).

Each owner is its own privacy scope, and the list resolved the gender policy of every scope
with its own handful of queries (5,420 statements for a member who sees about 1,000 owners).
"""

from __future__ import annotations

import datetime as dt
import uuid as uuid_pkg

import pytest

from app.core.security import get_password_hash
from tests.query_budget import count_statements

pytestmark = pytest.mark.unit


def _shared_profile(db, member, n):
    from app.models.collection import Collection
    from app.models.collection import CollectionMember
    from app.models.media import MediaFile
    from app.models.sharing import CollectionShare
    from app.models.speaker import Speaker
    from app.models.speaker import SpeakerProfile
    from app.models.user import User

    for i in range(n):
        owner = User(
            email=f"owner_{uuid_pkg.uuid4().hex[:10]}@example.com",
            full_name=f"Owner {i}",
            hashed_password=get_password_hash("x" * 12),
            is_active=True,
            role="user",
        )
        db.add(owner)
        db.flush()
        profile = SpeakerProfile(uuid=uuid_pkg.uuid4(), user_id=owner.id, name=f"Profile {i}")
        media = MediaFile(
            uuid=uuid_pkg.uuid4(),
            user_id=owner.id,
            filename=f"f{i}.mp4",
            title=f"f{i}",
            storage_path=f"media/test/{uuid_pkg.uuid4()}.mp4",
            content_type="video/mp4",
            file_size=1,
            status="completed",
            upload_time=dt.datetime.now(dt.UTC),
        )
        collection = Collection(user_id=owner.id, name=f"c{i}", description="")
        db.add_all([profile, media, collection])
        db.flush()
        db.add_all(
            [
                Speaker(
                    uuid=uuid_pkg.uuid4(),
                    user_id=owner.id,
                    media_file_id=media.id,
                    name="SPEAKER_00",
                    profile_id=profile.id,
                    verified=True,
                ),
                CollectionMember(collection_id=collection.id, media_file_id=media.id),
                CollectionShare(
                    collection_id=collection.id,
                    shared_by_id=owner.id,
                    target_type="user",
                    target_user_id=member.id,
                    permission="viewer",
                ),
            ]
        )
    db.commit()


def test_statements_do_not_grow_with_shared_owners(
    client, db_session, normal_user, user_token_headers
):
    _shared_profile(db_session, normal_user, 2)
    with count_statements(db_session.connection()) as small:
        r2 = client.get("/api/speaker-profiles/profiles", headers=user_token_headers)
    _shared_profile(db_session, normal_user, 10)
    with count_statements(db_session.connection()) as large:
        r12 = client.get("/api/speaker-profiles/profiles", headers=user_token_headers)

    assert (len(r2.json()), len(r12.json())) == (2, 12)  # control: the rows came back
    assert large.count == small.count, large.describe()
