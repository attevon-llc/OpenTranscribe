"""Watch-source, import-history and email-link queries, each against a real database."""

from __future__ import annotations

import uuid

from app.models.email_notification_config import EmailNotificationConfig
from app.models.email_notification_config import WatchSourceEmail
from app.models.watch_source import WatchSource
from app.models.watch_source import WatchSourceFile
from app.services import organization_service
from app.services import watch_email_queries as eq
from app.services import watch_source_queries as wq
from tests.fixtures.tag_world import join
from tests.fixtures.tag_world import make_org
from tests.fixtures.tag_world import make_user


def _source(db, owner, org_id=None, name=None):
    row = WatchSource(
        uuid=uuid.uuid4(),
        user_id=owner.id,
        created_by=owner.id,
        organization_id=org_id,
        name=name or f"watch-{uuid.uuid4().hex[:8]}",
        source_type="local",
        is_enabled=True,
        local_path=".",
        auto_transcribe=True,
    )
    db.add(row)
    db.commit()
    return row


def _tracked(db, source, filename, status="imported"):
    row = WatchSourceFile(
        watch_source_id=source.id,
        remote_path=f"in/{filename}",
        filename=filename,
        status=status,
    )
    db.add(row)
    db.commit()
    return row


def _config(db, name):
    row = EmailNotificationConfig(name=name, provider="smtp", is_enabled=True)
    db.add(row)
    db.commit()
    return row


def test_list_sources_is_tenant_scoped_and_owner_scoped_unless_widened(db_session):
    me, other = make_user(db_session, "me"), make_user(db_session, "other")
    org = make_org(db_session, "a")
    personal = _source(db_session, me)
    _source(db_session, me, org.id)
    theirs = _source(db_session, other)

    own = wq.list_sources(db_session, user_id=me.id, organization_id=None, all_owners=False)
    assert [s.id for s in own] == [personal.id]
    everyone = wq.list_sources(db_session, user_id=me.id, organization_id=None, all_owners=True)
    assert {theirs.id, personal.id} <= {s.id for s in everyone}
    assert all(s.organization_id is None for s in everyone)
    in_org = wq.list_sources(db_session, user_id=me.id, organization_id=org.id, all_owners=False)
    assert [s.organization_id for s in in_org] == [org.id]


def test_find_source_by_uuid(db_session):
    source = _source(db_session, make_user(db_session, "me"))

    found = wq.find_source(db_session, str(source.uuid))
    assert found is not None
    assert found.id == source.id
    assert wq.find_source(db_session, str(uuid.uuid4())) is None


def test_file_history_filters_by_status_and_literal_name_and_pages(db_session):
    source = _source(db_session, make_user(db_session, "me"))
    _tracked(db_session, source, "a_100%.wav", "imported")
    _tracked(db_session, source, "abc.wav", "error")
    _tracked(db_session, source, "skipped.wav", "skipped_duplicate")

    rows, total = wq.list_file_history(
        db_session, source.id, status="error", name_contains=None, page=1, page_size=10
    )
    assert (total, [r.filename for r in rows]) == (1, ["abc.wav"])
    rows, total = wq.list_file_history(
        db_session, source.id, status=None, name_contains=" _100% ", page=1, page_size=10
    )
    assert [r.filename for r in rows] == ["a_100%.wav"]
    rows, total = wq.list_file_history(
        db_session, source.id, status=None, name_contains=None, page=2, page_size=2
    )
    assert (total, len(rows)) == (3, 1)


def test_file_status_counts_and_tracked_file_lookups_stay_inside_the_source(db_session):
    me = make_user(db_session, "me")
    source, other = _source(db_session, me), _source(db_session, me)
    one = _tracked(db_session, source, "one.wav", "imported")
    _tracked(db_session, source, "two.wav", "imported")
    _tracked(db_session, source, "three.wav", "error")
    foreign = _tracked(db_session, other, "four.wav", "error")

    assert wq.file_status_counts(db_session, source.id) == {"imported": 2, "error": 1}
    found = wq.find_tracked_file(db_session, source.id, str(one.uuid))
    assert found is not None
    assert found.id == one.id
    assert wq.find_tracked_file(db_session, source.id, str(foreign.uuid)) is None
    got = wq.tracked_files_by_uuid(db_session, source.id, [str(one.uuid), str(foreign.uuid)])
    assert [r.id for r in got] == [one.id]


def test_email_config_lookups_and_links(db_session):
    me = make_user(db_session, "me")
    source = _source(db_session, me)
    first, second = _config(db_session, "aaa mailer"), _config(db_session, "zzz mailer")
    db_session.add(WatchSourceEmail(watch_source_id=source.id, email_config_id=first.id))
    db_session.commit()

    found = eq.find_email_config(db_session, str(first.uuid))
    assert found is not None
    assert found.id == first.id
    assert eq.find_email_config(db_session, str(uuid.uuid4())) is None
    names = [c.name for c in eq.list_all_configs_by_name(db_session)]
    assert names.index("aaa mailer") < names.index("zzz mailer")
    assert {c.id for c in eq.list_email_configs_with_links(db_session)} >= {first.id, second.id}
    assert eq.linked_config_ids(db_session, source.id) == {first.id}
    assert [link.email_config.id for link in eq.links_of_source(db_session, source.id)] == [
        first.id
    ]
    assert eq.find_link(db_session, source.id, first.id) is not None
    assert eq.find_link(db_session, source.id, second.id) is None


def test_active_organization_and_membership_lookups(db_session):
    org = make_org(db_session, "a")
    member, stranger = make_user(db_session, "member"), make_user(db_session, "stranger")
    join(db_session, org, member, "org:admin")
    external_id = org.external_org_id
    assert external_id is not None

    found = organization_service.find_active_org_by_external_id(db_session, external_id)
    assert found is not None
    assert found.id == org.id
    org.is_active = False
    db_session.commit()
    assert organization_service.find_active_org_by_external_id(db_session, external_id) is None
    membership = organization_service.find_membership(db_session, org.id, member.id)
    assert membership is not None
    assert membership.role == "org:admin"
    assert organization_service.find_membership(db_session, org.id, stranger.id) is None
