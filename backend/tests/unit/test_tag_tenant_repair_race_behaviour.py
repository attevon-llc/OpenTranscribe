"""Characterization of the tenant-repair fallback when a concurrent insert wins the race.

Written before the tags backend rewrite. The lookup is made to miss exactly once while the
competing row already exists, so the repair's own insert hits the partial unique index
(``IntegrityError``) deterministically; the pass must roll back only its savepoint, re-read
the winner, repoint onto it, count no created tag, and be a no-op on a second run.
"""

from __future__ import annotations

import pytest
from fixtures.tag_world import attach
from fixtures.tag_world import build_org_world
from fixtures.tag_world import make_file
from fixtures.tag_world import make_tag
from fixtures.tag_world import word
from sqlalchemy.exc import IntegrityError

from app.models.tag import FileTag
from app.models.tag import Tag
from app.services import tag_tenant_repair
from app.services.tag_tenant_repair import repair_cross_tenant_file_tags


@pytest.fixture
def repaired_events(monkeypatch):
    calls: list[list[int]] = []
    monkeypatch.setattr(tag_tenant_repair, "on_tags_changed", lambda db, ids: calls.append(ids))
    return calls


def _miss_first_lookup(monkeypatch) -> list[int]:
    """Make the first ``_find_target`` call answer None; later calls are real."""
    real = tag_tenant_repair._find_target
    calls: list[int] = []

    def flaky(db, name, org_id, owner_id):
        calls.append(1)
        return None if len(calls) == 1 else real(db, name, org_id, owner_id)

    monkeypatch.setattr(tag_tenant_repair, "_find_target", flaky)
    return calls


def _stray_on_org_file(db, name: str):
    """A personal tag attached to an org file, plus the org's own row already in place."""
    w = build_org_world(db)
    media = make_file(db, w.alice, org_id=w.org_a.id)
    stray = make_tag(db, name, user_id=w.alice.id, org_id=None)
    link = attach(db, media, stray)
    winner = make_tag(db, name, user_id=w.alice.id, org_id=w.org_a.id)
    return w, media, stray, link, winner


def test_a_lost_creation_race_repoints_onto_the_winner_and_creates_nothing(
    db_session, monkeypatch, repaired_events
):
    name = word("race")
    w, media, stray, link, winner = _stray_on_org_file(db_session, name)
    lookups = _miss_first_lookup(monkeypatch)

    report = repair_cross_tenant_file_tags(db_session)

    assert len(lookups) == 2  # the miss, then the re-read after the IntegrityError
    assert (report.repointed, report.collapsed, report.tags_created) == (1, 0, 0)
    assert report.file_ids == [media.id]
    assert repaired_events == [[media.id]]
    db_session.expire_all()
    assert db_session.query(FileTag).filter_by(id=link.id).one().tag_id == winner.id
    in_org = db_session.query(Tag).filter_by(name=name, organization_id=w.org_a.id).all()
    assert [t.id for t in in_org] == [winner.id]
    assert db_session.query(Tag).filter_by(id=stray.id).count() == 1  # no tag row is deleted


def test_the_savepoint_rollback_keeps_the_batch_work_already_flushed(
    db_session, monkeypatch, repaired_events
):
    name = word("race")
    w, media, stray, link, winner = _stray_on_org_file(db_session, name)
    other_name = word("calm")
    other_stray = make_tag(db_session, other_name, user_id=w.alice.id, org_id=None)
    other_link = attach(db_session, media, other_stray)
    other_target = make_tag(db_session, other_name, user_id=w.alice.id, org_id=w.org_a.id)
    _miss_first_lookup(monkeypatch)

    report = repair_cross_tenant_file_tags(db_session)

    assert (report.repointed, report.collapsed, report.tags_created) == (2, 0, 0)
    db_session.expire_all()
    assert db_session.query(FileTag).filter_by(id=link.id).one().tag_id == winner.id
    assert db_session.query(FileTag).filter_by(id=other_link.id).one().tag_id == other_target.id


def test_a_second_pass_after_a_lost_race_changes_nothing(db_session, monkeypatch, repaired_events):
    name = word("race")
    _stray_on_org_file(db_session, name)
    _miss_first_lookup(monkeypatch)
    first = repair_cross_tenant_file_tags(db_session)
    repaired_events.clear()

    second = repair_cross_tenant_file_tags(db_session)

    assert first.repointed == 1
    assert (second.repointed, second.collapsed, second.tags_created) == (0, 0, 0)
    assert second.file_ids == []
    assert repaired_events == []


def test_a_lost_race_onto_a_file_already_carrying_the_winner_collapses_the_link(
    db_session, monkeypatch, repaired_events
):
    name = word("race")
    w, media, stray, link, winner = _stray_on_org_file(db_session, name)
    attach(db_session, media, winner, source="auto_ai")
    _miss_first_lookup(monkeypatch)

    report = repair_cross_tenant_file_tags(db_session)

    assert (report.repointed, report.collapsed, report.tags_created) == (0, 1, 0)
    db_session.expire_all()
    assert db_session.query(FileTag).filter_by(id=link.id).count() == 0
    kept = db_session.query(FileTag).filter_by(media_file_id=media.id, tag_id=winner.id).one()
    assert kept.source == "manual"  # the more human origin of the two wins


def test_an_integrity_error_with_no_row_to_re_read_propagates(db_session, monkeypatch):
    name = word("race")
    _stray_on_org_file(db_session, name)
    monkeypatch.setattr(tag_tenant_repair, "_find_target", lambda *a, **k: None)

    with pytest.raises(IntegrityError):
        repair_cross_tenant_file_tags(db_session)
