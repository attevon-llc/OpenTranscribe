"""Usage event reads and the support-access organization search."""

from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from app.models.usage_event import UsageEvent
from app.services.support_access_queries import search_active_organizations
from app.services.usage_service import daily_token_totals
from app.services.usage_service import user_events_since
from tests.fixtures.tag_world import make_org
from tests.fixtures.tag_world import make_user


def _event(db, user, event_type, quantity, created_at, organization_id=None):
    row = UsageEvent(
        id=uuid.uuid4(),
        user_id=user.id,
        organization_id=organization_id,
        event_type=event_type,
        quantity=quantity,
        unit="tokens",
        event_metadata={},
        created_at=created_at,
    )
    db.add(row)
    db.flush()
    return row


def test_user_events_since_filters_user_window_and_tenant(db_session):
    now = datetime.now(UTC)
    me, other = make_user(db_session, "me"), make_user(db_session, "other")
    org = make_org(db_session, "a")
    kept = _event(db_session, me, "chat.tokens", 5, now)
    _event(db_session, me, "chat.tokens", 5, now - timedelta(days=10))
    _event(db_session, other, "chat.tokens", 5, now)
    _event(db_session, me, "chat.tokens", 5, now, organization_id=org.id)

    got = user_events_since(db_session, me.id, None, now - timedelta(days=1))
    assert [e.id for e in got] == [kept.id]


def test_daily_token_totals_groups_by_day_and_type_and_skips_other_types(db_session):
    now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    me = make_user(db_session, "me")
    _event(db_session, me, "chat.tokens", 10, now)
    _event(db_session, me, "chat.tokens", 5, now)
    _event(db_session, me, "summary.tokens", 7, now)
    _event(db_session, me, "upload.bytes", 99, now)

    rows = daily_token_totals(
        db_session, me.id, None, now - timedelta(days=1), ("chat.tokens", "summary.tokens")
    )
    assert {(r[1], int(r[2]), int(r[3])) for r in rows} == {
        ("chat.tokens", 15, 2),
        ("summary.tokens", 7, 1),
    }


def test_search_active_organizations_matches_literally_and_orders_by_name(db_session):
    tag = uuid.uuid4().hex[:8]
    alpha = make_org(db_session, f"{tag}alpha")
    beta = make_org(db_session, f"{tag}beta")
    gone = make_org(db_session, f"{tag}gone")
    gone.is_active = False
    db_session.commit()

    names = [o.name for o in search_active_organizations(db_session, tag, 25)]
    assert names == [alpha.name, beta.name]
    assert gone.name not in names
    assert search_active_organizations(db_session, f"{tag}%", 25) == []
    assert len(search_active_organizations(db_session, tag, 1)) == 1
