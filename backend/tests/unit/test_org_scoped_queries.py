"""Organization-scoped query services: takedown listing, holds, audit scope, privacy uuid."""

from __future__ import annotations

import uuid

from app.models.content_hold import ContentHold
from app.services.content_hold_queries import find_org_hold
from app.services.content_hold_queries import org_files_by_uuid
from app.services.org_audit_view import org_member_user_ids
from app.services.org_takedown_queries import find_org_file
from app.services.org_takedown_queries import list_taken_down_files
from app.services.privacy import policy_admin
from tests.fixtures.tag_world import join
from tests.fixtures.tag_world import make_file
from tests.fixtures.tag_world import make_org
from tests.fixtures.tag_world import make_user


def test_org_member_user_ids_lists_only_that_organizations_members(db_session):
    org_a, org_b = make_org(db_session, "a"), make_org(db_session, "b")
    in_a, in_b = make_user(db_session, "ma"), make_user(db_session, "mb")
    join(db_session, org_a, in_a)
    join(db_session, org_b, in_b)

    assert org_member_user_ids(db_session, org_a.id) == [in_a.id]


def test_find_org_file_refuses_another_organizations_file(db_session):
    org_a, org_b = make_org(db_session, "a"), make_org(db_session, "b")
    owner = make_user(db_session, "owner")
    mine = make_file(db_session, owner, org_a.id)

    found = find_org_file(db_session, mine.uuid, org_a.id)
    assert found is not None
    assert found.id == mine.id
    assert find_org_file(db_session, mine.uuid, org_b.id) is None


def test_list_taken_down_files_scopes_pages_and_counts(db_session):
    org, other = make_org(db_session, "a"), make_org(db_session, "b")
    owner = make_user(db_session, "owner")
    held, down, clean = (make_file(db_session, owner, org.id) for _ in range(3))
    foreign = make_file(db_session, owner, other.id)
    held.legal_hold = True
    down.is_quarantined = True
    foreign.is_quarantined = True
    db_session.commit()

    rows, total = list_taken_down_files(
        db_session, org.id, include_legal_holds=False, offset=0, limit=10
    )
    assert total == 1
    assert [r[0].id for r in rows] == [down.id]
    assert rows[0][1].id == owner.id  # owner joined
    assert rows[0][3] is None  # owner is not a member of the organization

    rows, total = list_taken_down_files(
        db_session, org.id, include_legal_holds=True, offset=0, limit=1
    )
    assert total == 2
    assert len(rows) == 1
    assert clean.id not in {r[0].id for r in rows}


def test_find_org_hold_and_org_files_by_uuid_stay_inside_the_organization(db_session):
    org, other = make_org(db_session, "a"), make_org(db_session, "b")
    owner = make_user(db_session, "owner")
    mine, theirs = make_file(db_session, owner, org.id), make_file(db_session, owner, other.id)
    hold = ContentHold(organization_id=org.id, scope="file", reason="pending litigation matter")
    db_session.add(hold)
    db_session.commit()

    found_hold = find_org_hold(db_session, org.id, hold.uuid)
    assert found_hold is not None
    assert found_hold.id == hold.id
    assert find_org_hold(db_session, other.id, hold.uuid) is None
    found = org_files_by_uuid(db_session, org.id, [str(mine.uuid), str(theirs.uuid)])
    assert [f.id for f in found] == [mine.id]


def test_organization_uuid_is_none_for_no_or_unknown_organization(db_session):
    org = make_org(db_session, "a")

    assert policy_admin.organization_uuid(db_session, org.id) == org.uuid
    assert policy_admin.organization_uuid(db_session, None) is None
    assert policy_admin.organization_uuid(db_session, 2_000_000_000) is None
    assert uuid.UUID(str(org.uuid))
