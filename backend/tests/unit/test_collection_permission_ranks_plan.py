"""``collection_permission_ranks`` must not seq-scan ``collection`` (issue #1337, D2).

The rank CASE carries six correlated subplans; with no candidate predicate PostgreSQL ran them
against every collection row (29.6 ms mean over 2,634 calls at 99k files). The statement now
names the three ways a rank can be positive, which the planner can answer from indexes.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import text

from app.models.collection import Collection
from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.models.sharing import CollectionShare
from app.models.user import User
from app.services import collection_visibility as cv

pytestmark = pytest.mark.unit


def _user(db, label):
    u = User(
        email=f"rankplan_{label}_{uuid_pkg.uuid4().hex[:8]}@example.com",
        full_name=label,
        hashed_password="x",
        is_active=True,
        role="user",
    )
    db.add(u)
    db.flush()
    return u


def test_ranks_statement_avoids_seq_scan_and_keeps_results(db_session):
    db = db_session
    me, other = _user(db, "me"), _user(db, "other")
    org = Organization(name=f"rankplan-{uuid_pkg.uuid4().hex[:8]}", slug=uuid_pkg.uuid4().hex[:12])
    db.add(org)
    db.flush()
    db.add(OrganizationMembership(organization_id=org.id, user_id=me.id, role="member"))
    mine = Collection(user_id=me.id, name="mine", description="")
    in_org = Collection(user_id=other.id, organization_id=org.id, name="org", description="")
    shared = Collection(user_id=other.id, name="shared", description="")
    hidden = Collection(user_id=other.id, name="hidden", description="")
    db.add_all([mine, in_org, shared, hidden])
    db.flush()
    db.add(
        CollectionShare(
            collection_id=shared.id,
            uuid=uuid_pkg.uuid4(),
            target_type="user",
            target_user_id=me.id,
            shared_by_id=other.id,
            permission="viewer",
        )
    )
    db.flush()

    ranks = cv.collection_permission_ranks(db, me.id)
    assert set(ranks) >= {mine.id, in_org.id, shared.id}
    assert hidden.id not in ranks

    rank = func.greatest(cv._org_base_rank(me.id), cv._share_rank(me.id))
    stmt = select(Collection.id, rank).where(cv._may_hold_rank_pred(me.id), rank > 0)
    db.execute(text("SET LOCAL enable_seqscan = off"))
    plan = "\n".join(
        r[0]
        for r in db.execute(
            text(
                "EXPLAIN "
                + str(stmt.compile(db.get_bind(), compile_kwargs={"literal_binds": True}))
            )
        )
    )
    assert "Seq Scan on collection " not in plan, plan
