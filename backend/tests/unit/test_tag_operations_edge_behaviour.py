"""Characterization of rename-with-system-tags, impact preview and cleanup (service level).

Written before the tags backend rewrite. ``pins_current_behaviour`` names pin D13 (a
duplicated id yields a duplicated impact entry) which the rewrite may change.
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
from app.services.tag_operations import cleanup_unreferenced_tags
from app.services.tag_operations import merge_tags
from app.services.tag_operations import preview_tag_impact
from app.services.tag_operations import rename_tag


class _Hook:
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


def _stored(db, tag_id: int) -> Tag:
    row = _row(db, tag_id)
    assert row is not None
    return row


# ---------------------------------------------------------------------------
# Rename collisions that involve system tags
# ---------------------------------------------------------------------------


def test_personal_tag_renamed_onto_a_system_name_is_held(db_session, normal_user, hook, commits):
    system = make_tag(db_session, word("sys-held"), user_id=None)
    mine = make_tag(db_session, word("mine-held"), user_id=normal_user.id)
    media_file = make_file(db_session, normal_user)
    attach(db_session, media_file, mine)
    commits.clear()
    old_name = mine.name

    outcome = rename_tag(db_session, mine.id, system.name.upper(), user_id=normal_user.id)

    assert outcome.requires_confirmation is True
    assert outcome.merged is False
    assert outcome.tag is not None and outcome.tag.id == system.id
    assert [(e.uuid, e.total_file_count) for e in outcome.impact.tags] == [(mine.uuid, 1)]
    assert commits == [] and hook.calls == []
    assert _stored(db_session, mine.id).name == old_name


def test_personal_tag_renamed_onto_a_system_name_and_confirmed_folds_into_the_system_tag(
    db_session, normal_user, hook
):
    system = make_tag(db_session, word("sys-fold"), user_id=None)
    mine = make_tag(db_session, word("mine-fold"), user_id=normal_user.id)
    media_file = make_file(db_session, normal_user)
    attach(db_session, media_file, mine)
    mine_uuid, mine_id = mine.uuid, mine.id

    outcome = rename_tag(
        db_session, mine_id, system.name, confirm_merge=True, user_id=normal_user.id
    )

    assert outcome.merged is True
    assert outcome.tag is not None and outcome.tag.id == system.id
    assert outcome.deleted_uuids == [mine_uuid]
    assert _row(db_session, mine_id) is None
    carried = db_session.query(FileTag).filter(FileTag.tag_id == system.id).one()
    assert carried.media_file_id == media_file.id
    assert hook.calls[-1]["system_scope"] is True


def test_system_tag_renamed_onto_another_system_name_is_held_then_merged(
    db_session, normal_user, hook
):
    keeper = make_tag(db_session, word("sys-keeper"), user_id=None)
    moving = make_tag(db_session, word("sys-moving"), user_id=None)
    moving_id, moving_uuid = moving.id, moving.uuid

    held = rename_tag(db_session, moving_id, keeper.name, user_id=normal_user.id)
    assert held.requires_confirmation is True and held.tag is not None
    assert held.tag.id == keeper.id
    assert _row(db_session, moving_id) is not None

    confirmed = rename_tag(
        db_session, moving_id, keeper.name, confirm_merge=True, user_id=normal_user.id
    )
    assert confirmed.merged is True
    assert confirmed.deleted_uuids == [moving_uuid]
    assert _row(db_session, moving_id) is None


def test_system_tag_renamed_onto_a_word_only_a_tenant_owns_is_a_plain_rename(
    db_session, normal_user, hook
):
    """The collision lookup runs in the renamed tag's tenant (system), not the caller's."""
    system = make_tag(db_session, word("sys-plain"), user_id=None)
    owned_word = word("only-mine")
    personal = make_tag(db_session, owned_word, user_id=normal_user.id)

    outcome = rename_tag(db_session, system.id, owned_word, user_id=normal_user.id)

    assert outcome.requires_confirmation is False and outcome.merged is False
    assert outcome.tag is not None and outcome.tag.id == system.id
    assert _stored(db_session, system.id).name == owned_word
    assert _row(db_session, personal.id) is not None
    assert hook.calls[-1]["system_scope"] is True


def test_rename_to_its_own_case_variant_is_a_plain_rename_with_no_confirmation(
    db_session, normal_user, hook
):
    tag = make_tag(db_session, word("variant"), user_id=normal_user.id)
    media_file = make_file(db_session, normal_user)
    attach(db_session, media_file, tag)

    outcome = rename_tag(db_session, tag.id, tag.name.upper(), user_id=normal_user.id)

    assert (outcome.merged, outcome.requires_confirmation) == (False, False)
    assert _stored(db_session, tag.id).name == tag.name.upper()
    assert hook.calls == [
        {"files": [media_file.id], "user_id": normal_user.id, "system_scope": False}
    ]


def test_rename_collision_is_resolved_in_the_renamed_tags_tenant_not_the_callers(
    db_session, normal_user, other_user, hook
):
    """Renaming another tenant's tag onto the *caller's* word is a plain rename."""
    word_mine = word("callers-word")
    make_tag(db_session, word_mine, user_id=normal_user.id)
    theirs = make_tag(db_session, word("theirs"), user_id=other_user.id)

    outcome = rename_tag(db_session, theirs.id, word_mine, user_id=normal_user.id)

    assert (outcome.merged, outcome.requires_confirmation) == (False, False)
    assert _stored(db_session, theirs.id).name == word_mine


# ---------------------------------------------------------------------------
# Merge that names only the target
# ---------------------------------------------------------------------------


def test_merge_of_the_target_into_itself_does_nothing(db_session, normal_user, hook, commits):
    target = make_tag(db_session, word("self"), user_id=normal_user.id, source="auto_ai")
    commits.clear()

    outcome = merge_tags(db_session, target.id, [target.id, target.id], user_id=normal_user.id)

    assert (outcome.merged, outcome.deleted_uuids) == (False, [])
    assert outcome.tag is not None and outcome.tag.id == target.id
    assert outcome.impact.tags == []
    assert commits == [] and hook.calls == []
    # Not endorsed either: nothing was merged.
    assert _stored(db_session, target.id).source == "auto_ai"


# ---------------------------------------------------------------------------
# preview_tag_impact
# ---------------------------------------------------------------------------


def test_impact_of_no_ids_is_an_empty_report(db_session, normal_user):
    report = preview_tag_impact(db_session, [], user_id=normal_user.id)

    assert (report.tags, report.accessible_file_count, report.total_file_count) == ([], 0, 0)


def test_impact_skips_ids_that_do_not_exist(db_session, normal_user):
    real = make_tag(db_session, word("real"), user_id=normal_user.id)

    report = preview_tag_impact(db_session, [2_000_000_000, real.id], user_id=normal_user.id)

    assert [e.uuid for e in report.tags] == [real.uuid]


def test_impact_entries_follow_the_supplied_order(db_session, normal_user):
    first = make_tag(db_session, word("a"), user_id=normal_user.id)
    second = make_tag(db_session, word("b"), user_id=normal_user.id)

    report = preview_tag_impact(db_session, [second.id, first.id], user_id=normal_user.id)

    assert [e.uuid for e in report.tags] == [second.uuid, first.uuid]


def test_impact_duplicates_a_duplicated_id_pins_current_behaviour(db_session, normal_user):
    """D13: the same tag twice yields two entries (counts themselves are not doubled)."""
    tag = make_tag(db_session, word("dup"), user_id=normal_user.id)
    media_file = make_file(db_session, normal_user)
    attach(db_session, media_file, tag)

    report = preview_tag_impact(db_session, [tag.id, tag.id], user_id=normal_user.id)

    assert [e.uuid for e in report.tags] == [tag.uuid, tag.uuid]
    assert (report.accessible_file_count, report.total_file_count) == (1, 1)


def test_impact_union_counts_a_file_carrying_two_tags_once(db_session, normal_user):
    first = make_tag(db_session, word("u1"), user_id=normal_user.id)
    second = make_tag(db_session, word("u2"), user_id=normal_user.id)
    both, only_first = make_file(db_session, normal_user), make_file(db_session, normal_user)
    for tag in (first, second):
        attach(db_session, both, tag)
    attach(db_session, only_first, first)

    report = preview_tag_impact(db_session, [first.id, second.id], user_id=normal_user.id)

    assert [(e.total_file_count) for e in report.tags] == [2, 1]
    assert (report.accessible_file_count, report.total_file_count) == (2, 2)


def test_impact_separates_accessible_from_total(db_session, normal_user, other_user):
    tag = make_tag(db_session, word("split"), user_id=normal_user.id)
    mine, theirs = make_file(db_session, normal_user), make_file(db_session, other_user)
    attach(db_session, mine, tag)
    attach(db_session, theirs, tag)

    report = preview_tag_impact(db_session, [tag.id], user_id=normal_user.id)

    assert [(e.accessible_file_count, e.total_file_count) for e in report.tags] == [(1, 2)]
    assert (report.accessible_file_count, report.total_file_count) == (1, 2)


def test_impact_none_organization_means_personal_scope_not_unscoped(db_session):
    world = build_org_world(db_session)
    tag = make_tag(db_session, word("scope"), user_id=world.alice.id, org_id=world.org_a.id)
    org_file = make_file(db_session, world.alice, world.org_a.id)
    attach(db_session, org_file, tag)

    personal = preview_tag_impact(db_session, [tag.id], user_id=world.alice.id)
    in_org = preview_tag_impact(
        db_session, [tag.id], user_id=world.alice.id, organization_id=world.org_a.id
    )

    assert (personal.accessible_file_count, personal.total_file_count) == (0, 1)
    assert (in_org.accessible_file_count, in_org.total_file_count) == (1, 1)


def test_impact_association_source_filter_counts_only_matching_origins(db_session, normal_user):
    tag = make_tag(db_session, word("origin"), user_id=normal_user.id)
    ai, human, legacy = (make_file(db_session, normal_user) for _ in range(3))
    attach(db_session, ai, tag, source="auto_ai")
    attach(db_session, human, tag, source="manual")
    attach(db_session, legacy, tag, source=None)

    everything = preview_tag_impact(db_session, [tag.id], user_id=normal_user.id)
    only_ai = preview_tag_impact(
        db_session, [tag.id], user_id=normal_user.id, association_source="auto_ai"
    )
    only_manual = preview_tag_impact(
        db_session, [tag.id], user_id=normal_user.id, association_source="manual"
    )

    assert everything.total_file_count == 3
    assert only_ai.total_file_count == 1
    # A NULL association origin never matches, not even "manual".
    assert only_manual.total_file_count == 1
    assert [e.total_file_count for e in only_ai.tags] == [1]


# ---------------------------------------------------------------------------
# cleanup_unreferenced_tags in an organization
# ---------------------------------------------------------------------------


def test_cleanup_in_an_org_removes_only_the_actors_unreferenced_tags_of_that_org(db_session, hook):
    world = build_org_world(db_session)
    mine = make_tag(db_session, word("mine"), user_id=world.alice.id, org_id=world.org_a.id)
    colleague = make_tag(db_session, word("colleague"), user_id=world.bob.id, org_id=world.org_a.id)
    other_org = make_tag(
        db_session, word("other-org"), user_id=world.alice.id, org_id=world.org_b.id
    )
    personal = make_tag(db_session, word("personal"), user_id=world.alice.id)
    system = make_tag(db_session, word("system"), user_id=None)
    used = make_tag(db_session, word("used"), user_id=world.alice.id, org_id=world.org_a.id)
    attach(db_session, make_file(db_session, world.alice, world.org_a.id), used)
    ids = {t.id for t in (colleague, other_org, personal, system, used)}
    mine_id = mine.id

    deleted = cleanup_unreferenced_tags(
        db_session, acting_user_id=world.alice.id, organization_id=world.org_a.id
    )

    assert deleted == 1
    assert _row(db_session, mine_id) is None
    assert all(_row(db_session, kept) is not None for kept in ids)


def test_cleanup_that_deletes_nothing_skips_commit_and_hook(db_session, normal_user, hook, commits):
    make_tag(db_session, word("system-exempt"), user_id=None)
    used = make_tag(db_session, word("used"), user_id=normal_user.id)
    attach(db_session, make_file(db_session, normal_user), used)
    commits.clear()

    deleted = cleanup_unreferenced_tags(db_session, acting_user_id=normal_user.id)

    assert deleted == 0
    assert commits == [] and hook.calls == []


def test_cleanup_that_deletes_commits_and_runs_the_hook_without_files(
    db_session, normal_user, hook, commits
):
    make_tag(db_session, word("unused"), user_id=normal_user.id)
    commits.clear()

    deleted = cleanup_unreferenced_tags(db_session, acting_user_id=normal_user.id)

    assert deleted == 1
    assert commits
    assert hook.calls == [{"files": [], "user_id": normal_user.id, "system_scope": False}]


def test_cleanup_all_users_sweeps_everyone_but_system_and_uses_the_global_cache_scope(
    db_session, normal_user, other_user, hook
):
    mine = make_tag(db_session, word("mine"), user_id=normal_user.id)
    theirs = make_tag(db_session, word("theirs"), user_id=other_user.id)
    system = make_tag(db_session, word("system"), user_id=None)
    mine_id, theirs_id = mine.id, theirs.id

    deleted = cleanup_unreferenced_tags(db_session, acting_user_id=normal_user.id, all_users=True)

    assert deleted >= 2
    assert _row(db_session, mine_id) is None and _row(db_session, theirs_id) is None
    assert _row(db_session, system.id) is not None
    assert hook.calls == [{"files": [], "user_id": normal_user.id, "system_scope": True}]
