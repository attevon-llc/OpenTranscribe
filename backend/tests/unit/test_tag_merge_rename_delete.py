"""Merge, rename and delete at the service level: what moves, what is kept, what is announced.

Each mutation commits once and then hands exactly the files whose tag set changed to the
change hook. Merges keep one association per file carrying the more human origin and the
higher confidence, endorse an ``auto_ai`` survivor, refuse to fold across tenants, and
rewrite stored watch-source names. Every operation locks its rows in id order.
"""

from __future__ import annotations

import pytest
from fixtures.tag_recorders import count_commits
from fixtures.tag_recorders import record_hook
from fixtures.tag_recorders import record_reindex
from fixtures.tag_world import attach
from fixtures.tag_world import build_org_world
from fixtures.tag_world import make_file
from fixtures.tag_world import make_tag
from fixtures.tag_world import word
from fixtures.tenant_rows import make_watch_source
from sqlalchemy import event

from app.models.tag import FileTag
from app.models.tag import Tag
from app.models.watch_source import WatchSource
from app.services.tag_operations import TAG_NOT_FOUND
from app.services.tag_operations import TagNotFoundError
from app.services.tag_operations import TagTenantMismatchError
from app.services.tag_operations import _better_confidence
from app.services.tag_operations import _promote_origin
from app.services.tag_operations import delete_tags
from app.services.tag_operations import merge_tags
from app.services.tag_operations import promote_tags_to_shared
from app.services.tag_operations import rename_tag
from app.services.tag_service import InvalidTagNameError
from app.services.tag_service import normalize_tag_name

OPS = "app.services.tag_operations"


def _gone(db, tag_id: int) -> bool:
    db.expire_all()
    return db.query(Tag).filter(Tag.id == tag_id).first() is None


def _links_of(db, tag_id: int) -> list[FileTag]:
    db.expire_all()
    links: list[FileTag] = (
        db.query(FileTag).filter(FileTag.tag_id == tag_id).order_by(FileTag.media_file_id).all()
    )
    return links


def _stored_names(db, source: WatchSource) -> list:
    db.expire_all()
    return list(db.query(WatchSource).filter(WatchSource.id == source.id).one().tag_names)


# ---------------------------------------------------------------------------
# Origin and confidence when two associations collapse
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("keep", "other", "expected"),
    [
        ("auto_ai", "manual", "manual"),
        ("auto_ai", "ai_accepted", "ai_accepted"),
        ("ai_accepted", "manual", "manual"),
        ("manual", "auto_ai", "manual"),
        ("ai_accepted", "auto_ai", "ai_accepted"),
        ("auto_ai", None, "manual"),
        ("ai_accepted", None, "manual"),
        (None, "auto_ai", None),
        (None, None, None),
        ("manual", None, "manual"),
        ("auto_ai", "something-new", "something-new"),
        ("auto_ai", "auto_ai", "auto_ai"),
    ],
)
def test_promote_origin_keeps_the_more_human_origin(keep, other, expected):
    assert _promote_origin(keep, other) == expected


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [(0.2, 0.9, 0.9), (0.9, 0.2, 0.9), (None, 0.4, 0.4), (0.4, None, 0.4), (None, None, None)],
)
def test_better_confidence_is_the_larger_and_none_loses(a, b, expected):
    assert _better_confidence(a, b) == expected


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("survivor_origin", "doomed_origin", "kept_origin"),
    [("auto_ai", "manual", "manual"), ("auto_ai", None, "manual"), ("manual", "auto_ai", "manual")],
)
def test_merge_collapses_a_shared_file_onto_one_association(
    db_session, normal_user, monkeypatch, survivor_origin, doomed_origin, kept_origin
):
    record_hook(monkeypatch, OPS)
    survivor = make_tag(db_session, word("keep"), user_id=normal_user.id)
    doomed = make_tag(db_session, word("fold"), user_id=normal_user.id)
    both = make_file(db_session, normal_user)
    kept_link = attach(db_session, both, survivor, source=survivor_origin)
    other_link = attach(db_session, both, doomed, source=doomed_origin)
    kept_link.ai_confidence = 0.3
    other_link.ai_confidence = 0.8
    db_session.commit()

    merge_tags(db_session, survivor.id, [doomed.id], user_id=normal_user.id)

    links = _links_of(db_session, survivor.id)
    assert [(link.media_file_id, link.source, link.ai_confidence) for link in links] == [
        (both.id, kept_origin, 0.8)
    ]
    assert _gone(db_session, doomed.id)


def test_merge_repoints_associations_and_reports_the_doomed_uuids_in_id_order(
    db_session, normal_user, monkeypatch
):
    hook = record_hook(monkeypatch, OPS)
    commits = count_commits(monkeypatch, db_session)
    survivor = make_tag(db_session, word("t"), user_id=normal_user.id)
    second = make_tag(db_session, word("s2"), user_id=normal_user.id)
    first = make_tag(db_session, word("s1"), user_id=normal_user.id)
    only_survivor, f_second, f_first = (make_file(db_session, normal_user) for _ in range(3))
    attach(db_session, only_survivor, survivor)
    attach(db_session, f_second, second)
    attach(db_session, f_first, first)
    commits.clear()

    outcome = merge_tags(db_session, survivor.id, [first.id, second.id], user_id=normal_user.id)

    assert outcome.merged is True
    assert outcome.tag is not None and outcome.tag.id == survivor.id
    assert outcome.deleted_uuids == [second.uuid, first.uuid]
    assert {e.uuid: e.total_file_count for e in outcome.impact.tags} == {
        second.uuid: 1,
        first.uuid: 1,
    }
    assert outcome.impact.total_file_count == 2
    carried = [link.media_file_id for link in _links_of(db_session, survivor.id)]
    assert len(carried) == 3
    assert set(carried) == {only_survivor.id, f_second.id, f_first.id}
    assert commits == [1]
    assert len(hook.calls) == 1
    assert sorted(hook.calls[0]["files"]) == sorted([f_second.id, f_first.id])
    assert hook.calls[0]["user_id"] == normal_user.id
    assert hook.calls[0]["system_scope"] is False


def test_merge_enqueues_one_reindex_of_exactly_the_affected_files(
    db_session, normal_user, monkeypatch
):
    reindex = record_reindex(monkeypatch)
    survivor = make_tag(db_session, word("rx"), user_id=normal_user.id)
    doomed = make_tag(db_session, word("rd"), user_id=normal_user.id)
    untouched, moved = make_file(db_session, normal_user), make_file(db_session, normal_user)
    attach(db_session, untouched, survivor)
    attach(db_session, moved, doomed)

    merge_tags(db_session, survivor.id, [doomed.id], user_id=normal_user.id)

    assert reindex.calls == [[moved.id]]


def test_the_second_of_two_opposite_merges_fails_and_keeps_the_first_survivor(
    db_session, normal_user, monkeypatch
):
    record_hook(monkeypatch, OPS)
    a = make_tag(db_session, word("a"), user_id=normal_user.id)
    b = make_tag(db_session, word("b"), user_id=normal_user.id)
    merge_tags(db_session, a.id, [b.id], user_id=normal_user.id)

    with pytest.raises(TagNotFoundError) as raised:
        merge_tags(db_session, b.id, [a.id], user_id=normal_user.id)

    assert str(raised.value) == TAG_NOT_FOUND
    assert not _gone(db_session, a.id)
    assert _gone(db_session, b.id)


def test_merge_with_a_vanished_source_raises_and_deletes_nothing(
    db_session, normal_user, monkeypatch
):
    record_hook(monkeypatch, OPS)
    a = make_tag(db_session, word("va"), user_id=normal_user.id)
    b = make_tag(db_session, word("vb"), user_id=normal_user.id)
    with pytest.raises(TagNotFoundError):
        merge_tags(db_session, a.id, [b.id, 2_000_000_000], user_id=normal_user.id)
    assert not _gone(db_session, b.id)


@pytest.mark.parametrize(("origin", "endorsed"), [("auto_ai", "ai_accepted"), ("manual", "manual")])
def test_merge_endorses_an_ai_survivor(db_session, normal_user, monkeypatch, origin, endorsed):
    record_hook(monkeypatch, OPS)
    survivor = make_tag(db_session, word("en"), user_id=normal_user.id, source=origin)
    doomed = make_tag(db_session, word("ed"), user_id=normal_user.id)
    merge_tags(db_session, survivor.id, [doomed.id], user_id=normal_user.id)
    db_session.expire_all()
    assert db_session.get(Tag, survivor.id).source == endorsed


def test_merge_refuses_to_fold_across_tenants_and_changes_nothing(db_session, monkeypatch):
    hook = record_hook(monkeypatch, OPS)
    w = build_org_world(db_session)
    org_a_tag = make_tag(db_session, word("oa"), user_id=w.alice.id, org_id=w.org_a.id)
    org_b_tag = make_tag(db_session, word("ob"), user_id=w.alice.id, org_id=w.org_b.id)
    personal = make_tag(db_session, word("pa"), user_id=w.alice.id)
    system = make_tag(db_session, word("sy"), user_id=None)

    for doomed in (org_b_tag, personal, system):
        with pytest.raises(TagTenantMismatchError):
            merge_tags(db_session, org_a_tag.id, [doomed.id], user_id=w.alice.id)

    assert [_gone(db_session, t.id) for t in (org_a_tag, org_b_tag, personal, system)] == [
        False,
        False,
        False,
        False,
    ]
    assert hook.calls == []


def test_anything_may_be_folded_into_a_system_tag(db_session, monkeypatch):
    hook = record_hook(monkeypatch, OPS)
    w = build_org_world(db_session)
    system = make_tag(db_session, word("sys"), user_id=None)
    org_tag = make_tag(db_session, word("org"), user_id=w.alice.id, org_id=w.org_a.id)
    personal = make_tag(db_session, word("own"), user_id=w.bob.id)

    outcome = merge_tags(db_session, system.id, [personal.id, org_tag.id], user_id=w.alice.id)

    assert outcome.merged is True
    assert outcome.deleted_uuids == [org_tag.uuid, personal.uuid]
    assert hook.calls[0]["system_scope"] is True


def test_merge_rewrites_stored_names_to_the_survivor_and_it_persists(
    db_session, normal_user, monkeypatch
):
    record_hook(monkeypatch, OPS)
    survivor = make_tag(db_session, word("Survivor"), user_id=normal_user.id)
    doomed = make_tag(db_session, word("Doomed"), user_id=normal_user.id)
    source = make_watch_source(
        db_session,
        user_id=normal_user.id,
        org_id=None,
        names=["first", doomed.name.upper(), "last"],
    )

    merge_tags(db_session, survivor.id, [doomed.id], user_id=normal_user.id)

    assert _stored_names(db_session, source) == ["first", survivor.name, "last"]


# ---------------------------------------------------------------------------
# Rename
# ---------------------------------------------------------------------------


def test_a_plain_rename_updates_name_normalization_and_stored_names(
    db_session, normal_user, monkeypatch
):
    hook = record_hook(monkeypatch, OPS)
    commits = count_commits(monkeypatch, db_session)
    tag = make_tag(db_session, word("before"), user_id=normal_user.id)
    media_file = make_file(db_session, normal_user)
    attach(db_session, media_file, tag)
    source = make_watch_source(
        db_session, user_id=normal_user.id, org_id=None, names=[tag.name, "other"]
    )
    new_name = f"  {word('After_Name')}  "
    commits.clear()

    outcome = rename_tag(db_session, tag.id, new_name, user_id=normal_user.id)

    db_session.expire_all()
    stored = db_session.get(Tag, tag.id)
    assert stored.name == new_name.strip()
    assert stored.normalized_name == normalize_tag_name(new_name)
    assert outcome.tag is not None and outcome.tag.id == tag.id
    assert (outcome.merged, outcome.requires_confirmation) == (False, False)
    assert [(e.uuid, e.total_file_count) for e in outcome.impact.tags] == [(tag.uuid, 1)]
    assert _stored_names(db_session, source) == [new_name.strip(), "other"]
    assert commits == [1]
    assert hook.calls == [
        {"files": [media_file.id], "user_id": normal_user.id, "system_scope": False}
    ]


def test_a_held_rename_changes_nothing_and_a_confirmed_one_merges(
    db_session, normal_user, monkeypatch
):
    hook = record_hook(monkeypatch, OPS)
    existing = make_tag(db_session, word("Existing"), user_id=normal_user.id)
    renamed = make_tag(db_session, word("Renamed"), user_id=normal_user.id)
    media_file = make_file(db_session, normal_user)
    attach(db_session, media_file, renamed)
    old_name = renamed.name

    held = rename_tag(db_session, renamed.id, existing.name.lower(), user_id=normal_user.id)
    assert held.requires_confirmation is True and held.merged is False
    assert held.tag is not None and held.tag.id == existing.id
    assert held.impact.total_file_count == 1 and held.impact.accessible_file_count == 1
    assert hook.calls == []
    db_session.expire_all()
    assert db_session.get(Tag, renamed.id).name == old_name

    renamed_uuid = renamed.uuid
    confirmed = rename_tag(
        db_session, renamed.id, existing.name, confirm_merge=True, user_id=normal_user.id
    )
    assert confirmed.merged is True
    assert confirmed.tag is not None and confirmed.tag.uuid == existing.uuid
    assert confirmed.deleted_uuids == [renamed_uuid]
    assert [link.media_file_id for link in _links_of(db_session, existing.id)] == [media_file.id]


def test_rename_to_a_blank_name_raises_before_touching_anything(db_session, normal_user):
    tag = make_tag(db_session, word("keep"), user_id=normal_user.id)
    with pytest.raises(InvalidTagNameError):
        rename_tag(db_session, tag.id, " _ ", user_id=normal_user.id)


def test_rename_of_a_vanished_tag_raises_tag_not_found(db_session, normal_user):
    with pytest.raises(TagNotFoundError) as raised:
        rename_tag(db_session, 2_000_000_000, word("x"), user_id=normal_user.id)
    assert str(raised.value) == TAG_NOT_FOUND


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def test_delete_measures_impact_first_and_removes_rows_and_associations(
    db_session, normal_user, other_user, monkeypatch
):
    hook = record_hook(monkeypatch, OPS)
    first = make_tag(db_session, word("d1"), user_id=normal_user.id)
    second = make_tag(db_session, word("d2"), user_id=normal_user.id)
    mine, theirs = make_file(db_session, normal_user), make_file(db_session, other_user)
    attach(db_session, mine, first)
    attach(db_session, theirs, first)
    attach(db_session, mine, second)
    source = make_watch_source(
        db_session, user_id=normal_user.id, org_id=None, names=["a", first.name, "b", second.name]
    )

    outcome = delete_tags(db_session, [second.id, first.id, second.id], user_id=normal_user.id)

    assert outcome.deleted_uuids == [first.uuid, second.uuid]
    assert outcome.tag is None and outcome.merged is False
    assert (outcome.impact.accessible_file_count, outcome.impact.total_file_count) == (1, 2)
    assert _gone(db_session, first.id) and _gone(db_session, second.id)
    assert _links_of(db_session, first.id) == [] and _links_of(db_session, second.id) == []
    assert _stored_names(db_session, source) == ["a", "b"]
    assert len(hook.calls) == 1
    assert sorted(hook.calls[0]["files"]) == sorted([mine.id, theirs.id])
    assert hook.calls[0]["system_scope"] is False


def test_deleting_a_system_tag_uses_the_global_cache_scope(db_session, admin_user, monkeypatch):
    hook = record_hook(monkeypatch, OPS)
    system = make_tag(db_session, word("dsys"), user_id=None)
    delete_tags(db_session, [system.id], user_id=admin_user.id)
    assert hook.calls[0]["system_scope"] is True


def test_delete_of_nothing_is_an_empty_outcome(db_session, normal_user, monkeypatch):
    hook = record_hook(monkeypatch, OPS)
    outcome = delete_tags(db_session, [], user_id=normal_user.id)
    assert outcome.deleted_uuids == [] and outcome.impact.tags == []
    assert hook.calls == []


def test_delete_with_a_vanished_id_raises_and_keeps_the_rest(db_session, normal_user, monkeypatch):
    record_hook(monkeypatch, OPS)
    tag = make_tag(db_session, word("dk"), user_id=normal_user.id)
    with pytest.raises(TagNotFoundError):
        delete_tags(db_session, [tag.id, 2_000_000_000], user_id=normal_user.id)
    assert not _gone(db_session, tag.id)


# ---------------------------------------------------------------------------
# Row locks
# ---------------------------------------------------------------------------


@pytest.fixture
def statements(db_session):
    seen: list[str] = []
    engine = db_session.get_bind()

    def capture(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    yield seen
    event.remove(engine, "before_cursor_execute", capture)


def _locks_in_id_order(seen: list[str]) -> bool:
    return any("FOR UPDATE" in s and "FROM tag" in s and "ORDER BY tag.id" in s for s in seen)


@pytest.mark.parametrize("operation", ["rename", "merge", "delete", "promote"])
def test_every_mutation_locks_its_rows_in_id_order(
    db_session, normal_user, monkeypatch, statements, operation
):
    record_hook(monkeypatch, OPS)
    a = make_tag(db_session, word("la"), user_id=normal_user.id)
    b = make_tag(db_session, word("lb"), user_id=normal_user.id)
    statements.clear()

    if operation == "rename":
        rename_tag(db_session, a.id, word("lr"), user_id=normal_user.id)
    elif operation == "merge":
        merge_tags(db_session, a.id, [b.id], user_id=normal_user.id)
    elif operation == "delete":
        delete_tags(db_session, [b.id, a.id], user_id=normal_user.id)
    else:
        promote_tags_to_shared(db_session, [a.id], user_id=normal_user.id)

    assert _locks_in_id_order(statements)
