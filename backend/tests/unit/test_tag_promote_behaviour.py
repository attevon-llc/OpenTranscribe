"""Characterization of ``promote_tags_to_shared`` at the service level.

Written before the tags backend rewrite. Promote reports every row it folded in
``deleted_uuids``, commits once, and folds two requested tags that share a normalized name
into the lower id instead of failing part-way (D7).
"""

from __future__ import annotations

import pytest
from fixtures.tag_recorders import count_commits
from fixtures.tag_recorders import record_hook
from fixtures.tag_world import attach
from fixtures.tag_world import build_org_world
from fixtures.tag_world import make_file
from fixtures.tag_world import make_tag
from fixtures.tag_world import word

from app.models.tag import FileTag
from app.models.tag import Tag
from app.services import tag_operations
from app.services.tag_operations import promote_tags_to_shared


@pytest.fixture
def hook(monkeypatch):
    return record_hook(monkeypatch, "app.services.tag_operations")


@pytest.fixture
def commits(monkeypatch, db_session) -> list[int]:
    seen: list[int] = count_commits(monkeypatch, db_session)
    return seen


def _row(db, tag_id: int) -> Tag | None:
    db.expire_all()
    row: Tag | None = db.query(Tag).filter(Tag.id == tag_id).first()
    return row


def _file_ids_with(db, tag_id: int) -> set[int]:
    return {
        int(f) for (f,) in db.query(FileTag.media_file_id).filter(FileTag.tag_id == tag_id).all()
    }


def test_promoting_an_already_system_tag_is_a_no_op_without_commit_or_hook(
    db_session, normal_user, hook, commits
):
    system = make_tag(db_session, word("already"), user_id=None)
    media_file = make_file(db_session, normal_user)
    attach(db_session, media_file, system)
    commits.clear()

    outcome = promote_tags_to_shared(db_session, [system.id], user_id=normal_user.id)

    assert commits == []
    assert hook.calls == []
    assert (outcome.tag, outcome.merged, outcome.requires_confirmation) == (None, False, False)
    assert outcome.deleted_uuids == []
    # The impact still describes the requested ids.
    assert [(e.uuid, e.total_file_count) for e in outcome.impact.tags] == [(system.uuid, 1)]
    assert (system.user_id, system.organization_id) == (None, None)


def test_promoting_nothing_returns_an_empty_outcome(db_session, normal_user, hook, commits):
    commits.clear()

    outcome = promote_tags_to_shared(db_session, [], user_id=normal_user.id)

    assert outcome.impact.tags == []
    assert (outcome.impact.accessible_file_count, outcome.impact.total_file_count) == (0, 0)
    assert commits == [] and hook.calls == []


def test_promote_nulls_the_owner_and_org_keeps_source_and_commits(
    db_session, normal_user, hook, commits
):
    tag = make_tag(db_session, word("promoted"), user_id=normal_user.id, source="ai_accepted")
    media_file = make_file(db_session, normal_user)
    attach(db_session, media_file, tag)
    commits.clear()

    outcome = promote_tags_to_shared(db_session, [tag.id], user_id=normal_user.id)

    row = _row(db_session, tag.id)
    assert row is not None
    assert (row.user_id, row.organization_id, row.source) == (None, None, "ai_accepted")
    assert commits
    assert outcome.tag is None and outcome.merged is False
    assert [(e.uuid, e.accessible_file_count, e.total_file_count) for e in outcome.impact.tags] == [
        (tag.uuid, 1, 1)
    ]


def test_promote_fires_the_hook_once_with_system_scope_and_the_tags_files(
    db_session, normal_user, hook
):
    tag = make_tag(db_session, word("hooked"), user_id=normal_user.id)
    first, second = make_file(db_session, normal_user), make_file(db_session, normal_user)
    attach(db_session, first, tag)
    attach(db_session, second, tag)

    promote_tags_to_shared(db_session, [tag.id], user_id=normal_user.id)

    assert len(hook.calls) == 1
    (call,) = hook.calls
    assert call["system_scope"] is True
    assert call["user_id"] == normal_user.id
    assert sorted(call["files"]) == sorted([first.id, second.id])


def test_promote_onto_an_existing_system_name_folds_into_that_system_tag(
    db_session, normal_user, hook
):
    name = word("collide")
    system = make_tag(db_session, name, user_id=None)
    personal = make_tag(db_session, name.upper(), user_id=normal_user.id)
    media_file = make_file(db_session, normal_user)
    attach(db_session, media_file, personal)
    personal_id = personal.id

    personal_uuid = personal.uuid

    outcome = promote_tags_to_shared(db_session, [personal_id], user_id=normal_user.id)

    assert _row(db_session, personal_id) is None
    assert _row(db_session, system.id) is not None
    assert _file_ids_with(db_session, system.id) == {media_file.id}
    assert outcome.deleted_uuids == [personal_uuid]
    assert outcome.merged is False and outcome.tag is None
    # One hook for the whole promotion, carrying the folded row's file.
    assert hook.calls == [
        {"files": [media_file.id], "user_id": normal_user.id, "system_scope": True}
    ]


def test_promote_folds_other_users_same_named_tags_and_keeps_their_files(
    db_session, normal_user, other_user, hook
):
    name = word("fold")
    mine = make_tag(db_session, name, user_id=normal_user.id)
    theirs = make_tag(db_session, name, user_id=other_user.id)
    my_file = make_file(db_session, normal_user)
    their_file = make_file(db_session, other_user)
    attach(db_session, my_file, mine)
    attach(db_session, their_file, theirs)
    theirs_id, theirs_uuid = theirs.id, theirs.uuid

    outcome = promote_tags_to_shared(db_session, [mine.id], user_id=normal_user.id)

    assert outcome.deleted_uuids == [theirs_uuid]
    assert _row(db_session, theirs_id) is None
    assert _file_ids_with(db_session, mine.id) == {my_file.id, their_file.id}
    promoted = _row(db_session, mine.id)
    assert promoted is not None and promoted.user_id is None


def test_promote_without_cross_tenant_folding_folds_only_the_promoted_tags_own_tenant(
    db_session, normal_user, other_user, hook
):
    name = word("tenantfold")
    world = build_org_world(db_session)
    mine = make_tag(db_session, name, user_id=normal_user.id)
    my_variant = make_tag(db_session, name.upper(), user_id=normal_user.id)
    other_users = make_tag(db_session, name, user_id=other_user.id)
    in_org = make_tag(db_session, name, user_id=world.alice.id, org_id=world.org_a.id)
    variant_id, other_id, org_id = my_variant.id, other_users.id, in_org.id

    promote_tags_to_shared(db_session, [mine.id], user_id=normal_user.id, fold_across_tenants=False)

    # A personal tenant is one user, so another user's row is a different tenant too.
    assert _row(db_session, variant_id) is None
    assert _row(db_session, other_id) is not None
    assert _row(db_session, org_id) is not None


def test_promote_with_cross_tenant_folding_folds_org_rows_too(db_session, normal_user, hook):
    name = word("widefold")
    world = build_org_world(db_session)
    mine = make_tag(db_session, name, user_id=normal_user.id)
    in_org = make_tag(db_session, name, user_id=world.alice.id, org_id=world.org_a.id)
    in_org_id = in_org.id

    promote_tags_to_shared(db_session, [mine.id], user_id=normal_user.id, fold_across_tenants=True)

    assert _row(db_session, in_org_id) is None


def test_promote_of_a_vanished_tag_raises_without_the_internal_id(db_session, normal_user, hook):
    """D16: the message is a fixed literal; no integer id can reach a 404 detail."""
    with pytest.raises(tag_operations.TagNotFoundError) as caught:
        promote_tags_to_shared(db_session, [2_000_000_000], user_id=normal_user.id)

    assert str(caught.value) == "Tag not found"


def test_promoting_two_same_named_tags_in_one_call_folds_them_into_the_lower_id(
    db_session, normal_user, other_user, hook, commits
):
    """D7: deterministic. The lower id becomes the system row; the other folds into it."""
    name = word("twice")
    first = make_tag(db_session, name, user_id=normal_user.id)
    second = make_tag(db_session, name.upper(), user_id=other_user.id)
    their_file = make_file(db_session, other_user)
    attach(db_session, their_file, second)
    first_id, second_id, second_uuid = first.id, second.id, second.uuid
    commits.clear()

    outcome = promote_tags_to_shared(db_session, [second_id, first_id], user_id=normal_user.id)

    survivor = _row(db_session, first_id)
    assert survivor is not None and (survivor.user_id, survivor.organization_id) == (None, None)
    assert _row(db_session, second_id) is None
    assert _file_ids_with(db_session, first_id) == {their_file.id}
    assert outcome.deleted_uuids == [second_uuid]
    assert len(commits) == 1
    assert hook.calls == [
        {"files": [their_file.id], "user_id": normal_user.id, "system_scope": True}
    ]


def test_promoting_two_same_named_tags_of_two_tenants_folds_both_requested_rows(
    db_session, normal_user, hook
):
    """Without cross-tenant folding a *requested* row is still folded: the caller named it."""
    name = word("twotenants")
    world = build_org_world(db_session)
    mine = make_tag(db_session, name, user_id=normal_user.id)
    in_org = make_tag(db_session, name, user_id=world.alice.id, org_id=world.org_a.id)
    bystander = make_tag(db_session, name.upper(), user_id=world.bob.id)
    mine_id, org_id, org_uuid, bystander_id = mine.id, in_org.id, in_org.uuid, bystander.id

    outcome = promote_tags_to_shared(
        db_session, [mine_id, org_id], user_id=normal_user.id, fold_across_tenants=False
    )

    assert outcome.deleted_uuids == [org_uuid]
    assert _row(db_session, org_id) is None
    assert _row(db_session, bystander_id) is not None


def test_a_promotion_that_fails_part_way_commits_nothing(
    db_session, normal_user, other_user, hook, commits
):
    """Atomic: an error on the second tag comes before any commit, so the request's rollback
    undoes the first tag's promotion and fold too, and no reindex is sent for them."""
    from sqlalchemy import event

    first = make_tag(db_session, word("atomic-a"), user_id=normal_user.id)
    make_tag(db_session, first.name.upper(), user_id=other_user.id)
    second = make_tag(db_session, word("atomic-b"), user_id=normal_user.id)
    first_id, second_id = first.id, second.id
    commits.clear()

    def refuse_the_second(session, _context, _instances):
        if any(
            isinstance(o, Tag) and o.id == second_id and o.user_id is None for o in session.dirty
        ):
            raise RuntimeError("refused")

    event.listen(db_session, "before_flush", refuse_the_second)
    try:
        with pytest.raises(RuntimeError, match="refused"):
            promote_tags_to_shared(db_session, [first_id, second_id], user_id=normal_user.id)
    finally:
        event.remove(db_session, "before_flush", refuse_the_second)

    assert commits == []
    assert hook.calls == []
