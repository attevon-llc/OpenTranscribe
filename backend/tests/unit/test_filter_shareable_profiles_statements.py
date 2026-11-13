"""``filter_shareable_profiles`` resolves privacy in one batch, not per (owner, org) group.

It called ``resolve_privacy_policy`` once per distinct owner, so a suggestions read over many
owners' profiles issued statements proportional to the owner count (loadrun A2).
"""

import uuid as uuid_pkg

from app.core.security import get_password_hash
from app.models.speaker import SpeakerProfile
from app.models.user import User
from app.services.privacy import policy
from tests.query_budget import count_statements


def _owners_profiles(db_session, n: int) -> list[int]:
    ids = []
    for _ in range(n):
        owner = User(
            email=f"owner_{uuid_pkg.uuid4().hex[:10]}@example.com",
            full_name="Owner",
            hashed_password=get_password_hash("password123"),
            is_active=True,
        )
        db_session.add(owner)
        db_session.flush()
        profile = SpeakerProfile(user_id=owner.id, name=f"P {uuid_pkg.uuid4().hex[:6]}")
        db_session.add(profile)
        db_session.flush()
        ids.append(int(profile.id))
    db_session.commit()
    return ids


def _run(db_session, requester_id: int, n: int):
    ids = _owners_profiles(db_session, n)
    db_session.expire_all()
    with count_statements(db_session.connection()) as log:
        kept = policy.filter_shareable_profiles(db_session, requester_id, ids)
    return log, kept, ids


def test_statement_count_does_not_grow_with_distinct_owners(db_session, normal_user):
    small, small_kept, small_ids = _run(db_session, normal_user.id, 2)
    large, large_kept, large_ids = _run(db_session, normal_user.id, 12)
    # control: the verdict is real (default policy allows sharing), for every owner
    assert small_kept == small_ids and large_kept == large_ids
    assert large.count == small.count, large.describe()
