"""Duplicate clusters, the stored-normalization repair, usage counts and the tag read lists.

Every test names its tags under a fresh stem and reads back only rows under that stem, so
seeded system tags and rows from other tests cannot satisfy or break an assertion.
"""

from __future__ import annotations

from difflib import SequenceMatcher

import pytest
from fixtures.tag_recorders import count_commits
from fixtures.tag_recorders import share_file
from fixtures.tag_world import attach
from fixtures.tag_world import make_file
from fixtures.tag_world import make_tag
from fixtures.tag_world import word

from app.models.tag import Tag
from app.services.tag_collisions import TAG_SCOPES
from app.services.tag_collisions import accessible_usage_counts
from app.services.tag_collisions import colliding_tag_ids
from app.services.tag_collisions import files_for_tag
from app.services.tag_collisions import find_tag_collisions
from app.services.tag_collisions import list_tags_filtered
from app.services.tag_collisions import list_unused_tag_rows
from app.services.tag_collisions import refresh_stored_normalization
from app.services.tag_collisions import tags_on_files
from app.services.tag_service import normalize_tag_name


def _ours(clusters, stem: str):
    return [c for c in clusters if c.normalized_name.startswith(normalize_tag_name(stem))]


def _named(entries, stem: str) -> list:
    return [e for e in entries if e.name.lower().startswith(stem)]


def test_tag_scopes_are_the_list_vocabulary():
    assert TAG_SCOPES == ("all", "mine", "system", "shared_with_me")


# ---------------------------------------------------------------------------
# Stored normalization repair
# ---------------------------------------------------------------------------


def test_the_repair_fixes_null_and_stale_forms_commits_and_is_idempotent(
    db_session, normal_user, other_user, monkeypatch
):
    stem = word("rp")
    null_form = Tag(name=f"{stem}-Null", user_id=normal_user.id, normalized_name=None)
    stale = Tag(name=f"{stem}-Edited", user_id=normal_user.id, normalized_name="old form")
    system_stale = Tag(name=f"{stem}-System", user_id=None, normalized_name="stale system")
    theirs = Tag(name=f"{stem}-Theirs", user_id=other_user.id, normalized_name="untouched")
    db_session.add_all([null_form, stale, system_stale, theirs])
    db_session.commit()
    commits = count_commits(monkeypatch, db_session)

    first = refresh_stored_normalization(db_session, user_id=normal_user.id, organization_id=None)
    after_first = len(commits)
    second = refresh_stored_normalization(db_session, user_id=normal_user.id, organization_id=None)

    assert first >= 3
    assert second == 0
    assert after_first == 1 and len(commits) == 1
    db_session.expire_all()
    assert [
        db_session.get(Tag, t.id).normalized_name for t in (null_form, stale, system_stale)
    ] == [
        f"{normalize_tag_name(stem)} null",
        f"{normalize_tag_name(stem)} edited",
        f"{normalize_tag_name(stem)} system",
    ]
    assert db_session.get(Tag, theirs.id).normalized_name == "untouched"


# ---------------------------------------------------------------------------
# Clusters
# ---------------------------------------------------------------------------


def test_variants_cluster_with_one_survivor_and_a_near_match_is_only_a_suggestion(
    db_session, normal_user
):
    stem = word("cl")
    plain = make_tag(db_session, f"{stem}-q3-earnings", user_id=normal_user.id)
    shouty = make_tag(db_session, f"{stem.upper()}_Q3_EARNINGS", user_id=normal_user.id)
    near = make_tag(db_session, f"{stem}-q4-earnings", user_id=normal_user.id)
    used = make_file(db_session, normal_user)
    attach(db_session, used, shouty)

    clusters = _ours(
        find_tag_collisions(db_session, user_id=normal_user.id, organization_id=None), stem
    )

    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.normalized_name == f"{normalize_tag_name(stem)} q3 earnings"
    assert [(m.uuid, m.usage_count, m.suggested_survivor) for m in cluster.members] == [
        (shouty.uuid, 1, True),
        (plain.uuid, 0, False),
    ]
    assert cluster.suggested_survivor_uuid == shouty.uuid
    assert {m.ownership for m in cluster.members} == {"mine"}
    expected = round(
        SequenceMatcher(
            None,
            f"{normalize_tag_name(stem)} q3 earnings",
            f"{normalize_tag_name(stem)} q4 earnings",
        ).ratio(),
        4,
    )
    assert [(s.uuid, s.similarity) for s in cluster.suggestions] == [(near.uuid, expected)]
    assert near.uuid not in {m.uuid for m in cluster.members}


def test_members_tied_on_usage_are_ordered_by_name(db_session, normal_user):
    stem = word("tie")
    underscore = make_tag(db_session, f"{stem}_alpha", user_id=normal_user.id)
    dash = make_tag(db_session, f"{stem}-Alpha", user_id=normal_user.id)
    clusters = _ours(
        find_tag_collisions(db_session, user_id=normal_user.id, organization_id=None), stem
    )
    assert [m.uuid for m in clusters[0].members] == [dash.uuid, underscore.uuid]
    assert clusters[0].suggested_survivor_uuid == dash.uuid


def test_clusters_are_ordered_by_normalized_name_and_stable(db_session, normal_user):
    stem = word("ord")
    for name in (f"{stem}-zeta", f"{stem}_ZETA", f"{stem}-alpha", f"{stem}_Alpha"):
        make_tag(db_session, name, user_id=normal_user.id)

    first = _ours(
        find_tag_collisions(db_session, user_id=normal_user.id, organization_id=None), stem
    )
    second = _ours(
        find_tag_collisions(db_session, user_id=normal_user.id, organization_id=None), stem
    )

    assert [c.normalized_name for c in first] == [
        f"{normalize_tag_name(stem)} alpha",
        f"{normalize_tag_name(stem)} zeta",
    ]
    assert first == second


def test_another_accounts_same_named_tag_is_not_a_member(db_session, normal_user, other_user):
    stem = word("own")
    make_tag(db_session, f"{stem}-solo", user_id=normal_user.id)
    make_tag(db_session, f"{stem}_SOLO", user_id=other_user.id)
    assert (
        _ours(find_tag_collisions(db_session, user_id=normal_user.id, organization_id=None), stem)
        == []
    )


def test_colliding_ids_are_exactly_the_cluster_members(db_session, normal_user):
    stem = word("cid")
    a = make_tag(db_session, f"{stem}-pair", user_id=normal_user.id)
    b = make_tag(db_session, f"{stem} PAIR", user_id=normal_user.id)
    lone = make_tag(db_session, f"{stem}-lone", user_id=normal_user.id)
    ids = colliding_tag_ids(db_session, user_id=normal_user.id, organization_id=None)
    assert {a.id, b.id} <= ids
    assert lone.id not in ids


# ---------------------------------------------------------------------------
# Usage and lists
# ---------------------------------------------------------------------------


def test_usage_counts_distinct_accessible_files_only(db_session, normal_user, other_user):
    tag = make_tag(db_session, word("use"), user_id=normal_user.id)
    hidden_only = make_tag(db_session, word("hid"), user_id=normal_user.id)
    mine_1, mine_2 = make_file(db_session, normal_user), make_file(db_session, normal_user)
    theirs = make_file(db_session, other_user)
    for media_file in (mine_1, mine_2, theirs):
        attach(db_session, media_file, tag)
    attach(db_session, theirs, hidden_only)

    usage = accessible_usage_counts(db_session, user_id=normal_user.id, organization_id=None)

    assert usage[tag.id] == 2
    assert hidden_only.id not in usage


def test_scopes_partition_the_list_and_all_is_their_union(db_session, normal_user, other_user):
    stem = word("sc")
    mine = make_tag(db_session, f"{stem}-mine", user_id=normal_user.id)
    system = make_tag(db_session, f"{stem}-system", user_id=None)
    shared_file = make_file(db_session, other_user)
    theirs = make_tag(db_session, f"{stem}-theirs", user_id=other_user.id)
    attach(db_session, shared_file, theirs)
    share_file(db_session, other_user, normal_user, shared_file)

    by_scope = {
        scope: {
            e.uuid: e.ownership
            for e in _named(
                list_tags_filtered(
                    db_session, user_id=normal_user.id, organization_id=None, scope=scope
                ),
                stem,
            )
        }
        for scope in TAG_SCOPES
    }

    assert by_scope["mine"] == {mine.uuid: "mine"}
    assert by_scope["system"] == {system.uuid: "system"}
    assert by_scope["shared_with_me"] == {theirs.uuid: "shared_with_me"}
    assert by_scope["all"] == {
        **by_scope["mine"],
        **by_scope["system"],
        **by_scope["shared_with_me"],
    }


def test_an_unknown_scope_is_rejected(db_session, normal_user):
    with pytest.raises(ValueError):
        list_tags_filtered(db_session, user_id=normal_user.id, organization_id=None, scope="ours")


def test_unused_and_colliding_filters_combine(db_session, normal_user):
    stem = word("fl")
    used_twin = make_tag(db_session, f"{stem}-twin", user_id=normal_user.id)
    unused_twin = make_tag(db_session, f"{stem}_TWIN", user_id=normal_user.id)
    lone = make_tag(db_session, f"{stem}-lone", user_id=normal_user.id)
    attach(db_session, make_file(db_session, normal_user), used_twin)

    def names(**filters) -> list[str]:
        rows = list_tags_filtered(
            db_session, user_id=normal_user.id, organization_id=None, **filters
        )
        return sorted(e.name for e in _named(rows, stem))

    assert names(unused=True) == sorted([unused_twin.name, lone.name])
    assert names(colliding=True) == sorted([used_twin.name, unused_twin.name])
    assert names(unused=True, colliding=True) == [unused_twin.name]


def test_the_list_orders_by_usage_then_name_and_reports_counts(db_session, normal_user):
    stem = word("lo")
    heavy = make_tag(db_session, f"{stem}-zz", user_id=normal_user.id)
    b = make_tag(db_session, f"{stem}-bb", user_id=normal_user.id)
    a = make_tag(db_session, f"{stem}-aa", user_id=normal_user.id)
    for _ in range(2):
        attach(db_session, make_file(db_session, normal_user), heavy)

    rows = _named(
        list_tags_filtered(db_session, user_id=normal_user.id, organization_id=None), stem
    )

    assert [(e.uuid, e.usage_count) for e in rows] == [(heavy.uuid, 2), (a.uuid, 0), (b.uuid, 0)]


def test_unused_rows_count_only_accessible_files_and_are_ordered_by_name(
    db_session, normal_user, other_user
):
    stem = word("un")
    on_hidden_file = make_tag(db_session, f"{stem}-b-hidden", user_id=normal_user.id)
    never_used = make_tag(db_session, f"{stem}-a-never", user_id=normal_user.id)
    on_own_file = make_tag(db_session, f"{stem}-c-own", user_id=normal_user.id)
    attach(db_session, make_file(db_session, other_user), on_hidden_file)
    attach(db_session, make_file(db_session, normal_user), on_own_file)

    rows = [
        t
        for t in list_unused_tag_rows(db_session, user_id=normal_user.id, organization_id=None)
        if t.name.startswith(stem)
    ]

    assert [t.id for t in rows] == [never_used.id, on_hidden_file.id]


# ---------------------------------------------------------------------------
# Selection and per-tag file listings
# ---------------------------------------------------------------------------


def test_tags_on_files_counts_only_accessible_files(db_session, normal_user, other_user):
    stem = word("sel")
    common = make_tag(db_session, f"{stem}-common", user_id=normal_user.id)
    single = make_tag(db_session, f"{stem}-a-single", user_id=normal_user.id)
    hidden = make_tag(db_session, f"{stem}-hidden", user_id=other_user.id)
    f1, f2 = make_file(db_session, normal_user), make_file(db_session, normal_user)
    theirs = make_file(db_session, other_user)
    attach(db_session, f1, common)
    attach(db_session, f2, common)
    attach(db_session, theirs, common)
    attach(db_session, f2, single)
    attach(db_session, theirs, hidden)

    rows = tags_on_files(
        db_session, [f1.id, f2.id, theirs.id], user_id=normal_user.id, organization_id=None
    )

    assert [(tag.id, count) for tag, count in rows] == [(common.id, 2), (single.id, 1)]
    assert tags_on_files(db_session, [], user_id=normal_user.id, organization_id=None) == []


def test_files_for_tag_lists_newest_first_with_the_true_total(db_session, normal_user, other_user):
    tag = make_tag(db_session, word("ff"), user_id=None)
    files = [make_file(db_session, normal_user) for _ in range(3)]
    for media_file in files:
        attach(db_session, media_file, tag)
    attach(db_session, make_file(db_session, other_user), tag)

    listed, total = files_for_tag(
        db_session, tag.id, user_id=normal_user.id, organization_id=None, limit=2
    )

    assert total == 3
    newest_first = sorted(files, key=lambda f: (f.upload_time, f.id), reverse=True)
    assert [f.id for f in listed] == [f.id for f in newest_first[:2]]


def test_a_tag_reached_through_one_shared_file_lists_only_that_file(
    db_session, normal_user, other_user
):
    tag = make_tag(db_session, word("sh"), user_id=other_user.id)
    shared, private = make_file(db_session, other_user), make_file(db_session, other_user)
    attach(db_session, shared, tag)
    attach(db_session, private, tag)
    share_file(db_session, other_user, normal_user, shared)

    listed, total = files_for_tag(db_session, tag.id, user_id=normal_user.id, organization_id=None)

    assert ([f.id for f in listed], total) == ([shared.id], 1)
