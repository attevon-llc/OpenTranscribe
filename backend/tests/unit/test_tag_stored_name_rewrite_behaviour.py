"""Characterization of ``rewrite_stored_tag_names`` (watch-source tag name lists).

Written before the tags backend rewrite. The test whose name says ``pins_current_behaviour``
pins D14: a processed list also loses every unrelated normalized duplicate it holds.
"""

from __future__ import annotations

from typing import Any

from fixtures.tag_world import word
from fixtures.tenant_rows import make_watch_source

from app.models.watch_source import WatchSource
from app.services.tag_operations import rewrite_stored_tag_names


def _stored(db, source: WatchSource):
    db.expire_all()
    return db.query(WatchSource).filter(WatchSource.id == source.id).one().tag_names


def _rewrite(db, user, doomed: set[str], replacement, *, names=None, source=None):
    row = source or make_watch_source(db, user_id=user.id, org_id=None, names=names)
    changed = rewrite_stored_tag_names(db, doomed, replacement, tenant=("user", user.id))
    db.commit()
    return changed, _stored(db, row)


def test_an_empty_doomed_set_changes_nothing_and_returns_zero(db_session, normal_user):
    source = make_watch_source(db_session, user_id=normal_user.id, org_id=None, names=["a", "b"])

    changed = rewrite_stored_tag_names(db_session, set(), "x", tenant=("user", normal_user.id))

    assert changed == 0
    assert _stored(db_session, source) == ["a", "b"]


def test_non_string_entries_are_kept_untouched_and_in_place(db_session, normal_user):
    changed, stored = _rewrite(
        db_session, normal_user, {"old"}, "New", names=[None, 5, "Old", {"k": "old"}, ["old"]]
    )

    assert changed == 1
    assert stored == [None, 5, "New", {"k": "old"}, ["old"]]


def test_strings_that_normalize_empty_are_always_kept_even_repeated(db_session, normal_user):
    changed, stored = _rewrite(
        db_session, normal_user, {"old"}, "new", names=["-", "old", "-", "__", "   "]
    )

    assert changed == 1
    assert stored == ["-", "new", "-", "__", "   "]


def test_a_replacement_already_configured_later_collapses_onto_the_first_occurrence(
    db_session, normal_user
):
    changed, stored = _rewrite(db_session, normal_user, {"old"}, "New", names=["old", "x", "NEW"])

    assert changed == 1
    # The rewritten entry is seen first, so the later spelling of the survivor is dropped.
    assert stored == ["New", "x"]


def test_a_replacement_already_configured_earlier_swallows_the_rewritten_entry(
    db_session, normal_user
):
    changed, stored = _rewrite(db_session, normal_user, {"old"}, "new", names=["new", "x", "old"])

    assert changed == 1
    assert stored == ["new", "x"]


def test_relative_order_of_surviving_entries_is_preserved(db_session, normal_user):
    changed, stored = _rewrite(
        db_session, normal_user, {"b"}, "B2", names=["z", "a", "b", "y", "c"]
    )

    assert (changed, stored) == (1, ["z", "a", "B2", "y", "c"])


def test_stripping_removes_every_variant_and_keeps_the_others_in_order(db_session, normal_user):
    changed, stored = _rewrite(
        db_session, normal_user, {"old tag"}, None, names=["keep-1", "Old-Tag", "old_tag", "keep-2"]
    )

    assert (changed, stored) == (1, ["keep-1", "keep-2"])


def test_an_unrelated_normalized_duplicate_in_a_processed_list_is_also_collapsed_pins_current_behaviour(
    db_session, normal_user
):
    """D14: ``x`` and ``X`` have nothing to do with the rewrite, yet one of them goes."""
    changed, stored = _rewrite(
        db_session, normal_user, {"old"}, "new", names=["old", "x", "X", "x"]
    )

    assert changed == 1
    assert stored == ["new", "x"]


def test_a_list_without_a_doomed_name_and_without_duplicates_is_left_as_written(
    db_session, normal_user
):
    names = ["x", "y", "-", "-"]
    changed, stored = _rewrite(db_session, normal_user, {"old"}, "new", names=names)

    assert changed == 0
    assert stored == names


def test_a_list_that_never_mentions_the_doomed_name_still_loses_its_duplicates_pins_current_behaviour(
    db_session, normal_user
):
    """D14, wider than the spec says: every list of the tenant is processed, not only the
    lists that contain a doomed entry, so a duplicate in an unrelated list is dropped and the
    source is counted as changed."""
    changed, stored = _rewrite(db_session, normal_user, {"old"}, "new", names=["x", "X", "y"])

    assert changed == 1
    assert stored == ["x", "y"]


def test_the_result_is_persisted_by_wholesale_reassignment(db_session, normal_user):
    source = make_watch_source(db_session, user_id=normal_user.id, org_id=None, names=["Old"])

    rewrite_stored_tag_names(db_session, {"old"}, "Fresh", tenant=("user", normal_user.id))
    db_session.commit()
    db_session.expire_all()

    assert db_session.get(WatchSource, source.id).tag_names == ["Fresh"]


def test_the_count_is_the_number_of_sources_changed(db_session, normal_user):
    make_watch_source(db_session, user_id=normal_user.id, org_id=None, names=["old"])
    make_watch_source(db_session, user_id=normal_user.id, org_id=None, names=["OLD", "k"])
    untouched = make_watch_source(db_session, user_id=normal_user.id, org_id=None, names=["k"])

    changed = rewrite_stored_tag_names(db_session, {"old"}, "new", tenant=("user", normal_user.id))

    assert changed == 2
    assert _stored(db_session, untouched) == ["k"]


def test_sources_with_no_list_are_skipped(db_session, normal_user):
    null_source = make_watch_source(db_session, user_id=normal_user.id, org_id=None, names=None)
    not_a_list: Any = {"old": 1}
    dict_source = make_watch_source(
        db_session,
        user_id=normal_user.id,
        org_id=None,
        names=not_a_list,
    )

    changed = rewrite_stored_tag_names(db_session, {"old"}, "new", tenant=("user", normal_user.id))

    assert changed == 0
    assert _stored(db_session, null_source) is None
    assert _stored(db_session, dict_source) == {"old": 1}


def test_another_users_sources_are_not_touched(db_session, normal_user, other_user):
    name = word("shared-word")
    theirs = make_watch_source(db_session, user_id=other_user.id, org_id=None, names=[name])

    changed = rewrite_stored_tag_names(db_session, {name}, "new", tenant=("user", normal_user.id))

    assert changed == 0
    assert _stored(db_session, theirs) == [name]
