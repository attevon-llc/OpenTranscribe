"""Characterization of ``promote_tags_to_shared`` at the service level.

Written before the tags backend rewrite. ``test_..._pins_current_behaviour`` names pin D7:
promote reports nothing about the rows it folded, and promoting two tags that share a
normalized name in one call fails loudly part-way through.
"""

from __future__ import annotations

import pytest
from fixtures.tag_world import attach
from fixtures.tag_world import build_org_world
from fixtures.tag_world import make_file
from fixtures.tag_world import make_tag
from fixtures.tag_world import word

from app.models.tag import FileTag
from app.models.tag import Tag
from app.services.tag_operations import TagNotFoundError
from app.services.tag_operations import promote_tags_to_shared


class _Hook:
    """Stands in for ``on_tags_changed`` inside ``tag_operations``."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, db, file_ids=None, *, user_id=None, system_scope=False):
        self.calls.append(
            {"files": list(file_ids or []), "user_id": user_id, "system_scope": system_scope}
        )
        return list(file_ids or [])


@pytest.fixture
def hook(monkeypatch):
    recorder = _Hook()
    monkeypatch.setattr("app.services.tag_operations.on_tags_changed", recorder)
    return recorder


@pytest.fixture
def commits(monkeypatch, db_session) -> list[int]:
    seen: list[int] = []
    original = db_session.commit

    def counting():
        seen.append(1)
        return original()

    monkeypatch.setattr(db_session, "commit", counting)
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

    outcome = promote_tags_to_shared(db_session, [personal_id], user_id=normal_user.id)

    assert _row(db_session, personal_id) is None
    assert _row(db_session, system.id) is not None
    assert _file_ids_with(db_session, system.id) == {media_file.id}
    # The fold is invisible in the result (D7): nothing says a row was deleted.
    assert outcome.deleted_uuids == []
    assert outcome.merged is False and outcome.tag is None
    # merge_tags fires its own hook; promote itself then fires its final one with no files.
    assert [c["system_scope"] for c in hook.calls] == [True, True]
    assert hook.calls[0]["files"] == [media_file.id]
    assert hook.calls[-1]["files"] == []


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
    theirs_id = theirs.id

    promote_tags_to_shared(db_session, [mine.id], user_id=normal_user.id)

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


def test_promote_of_a_vanished_tag_raises_with_the_internal_id_pins_current_behaviour(
    db_session, normal_user, hook
):
    """D16: the message that becomes the 404 detail names the integer id."""
    with pytest.raises(TagNotFoundError) as caught:
        promote_tags_to_shared(db_session, [2_000_000_000], user_id=normal_user.id)

    assert str(caught.value) == "Tags no longer exist: [2000000000]"


def test_promoting_two_same_named_tags_in_one_call_fails_part_way_pins_current_behaviour(
    db_session, normal_user, other_user, hook
):
    """D7: the first promoted row folds the second, then the loop reaches the folded row."""
    name = word("twice")
    first = make_tag(db_session, name, user_id=normal_user.id)
    second = make_tag(db_session, name, user_id=other_user.id)
    first_id, second_id = first.id, second.id

    with pytest.raises(TagNotFoundError):
        promote_tags_to_shared(db_session, [first_id, second_id], user_id=normal_user.id)

    # The first fold had already been committed by merge_tags when the error surfaced.
    survivor = _row(db_session, first_id)
    assert survivor is not None and survivor.user_id is None
    assert _row(db_session, second_id) is None
