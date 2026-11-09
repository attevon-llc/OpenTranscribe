"""Per-user key/value setting queries in ``services.user_setting_queries``, against a real database."""

from __future__ import annotations

from sqlalchemy import event

from app.services import user_setting_queries as q


def _commits(db_session, action):
    commits: list[int] = []

    def count(_session):
        commits.append(1)

    event.listen(db_session, "after_commit", count)
    try:
        action()
    finally:
        event.remove(db_session, "after_commit", count)
    return len(commits)


def test_upsert_inserts_then_updates_the_same_row(normal_user, db_session):
    q.upsert_user_setting(db_session, normal_user.id, "k1", "a")
    db_session.flush()
    q.upsert_user_setting(db_session, normal_user.id, "k1", "b")
    db_session.flush()
    rows = q.list_user_settings(db_session, normal_user.id, ["k1"])
    assert [(r.setting_key, r.setting_value) for r in rows] == [("k1", "b")]


def test_upsert_stores_booleans_as_lowercase_words_and_other_values_as_str(normal_user, db_session):
    q.upsert_user_setting(db_session, normal_user.id, "flag_on", True)
    q.upsert_user_setting(db_session, normal_user.id, "flag_off", False)
    q.upsert_user_setting(db_session, normal_user.id, "count", 7)
    db_session.flush()
    values = q.user_setting_values(db_session, normal_user.id, ["flag_on", "flag_off", "count"])
    assert values == {"flag_on": "true", "flag_off": "false", "count": "7"}


def test_listing_is_scoped_to_the_user_and_to_the_requested_keys(
    normal_user, admin_user, db_session
):
    q.upsert_user_setting(db_session, normal_user.id, "mine", "1")
    q.upsert_user_setting(db_session, normal_user.id, "other_key", "2")
    q.upsert_user_setting(db_session, admin_user.id, "mine", "3")
    db_session.flush()
    only_mine = q.list_user_settings(db_session, normal_user.id, ["mine"])
    assert [r.setting_value for r in only_mine] == ["1"]
    assert len(q.list_user_settings(db_session, normal_user.id)) == 2


def test_delete_removes_only_the_named_keys_of_that_user(normal_user, admin_user, db_session):
    for user in (normal_user, admin_user):
        q.upsert_user_setting(db_session, user.id, "a", "x")
        q.upsert_user_setting(db_session, user.id, "b", "x")
    db_session.flush()
    assert q.delete_user_settings(db_session, normal_user.id, ["a"]) == 1
    assert [r.setting_key for r in q.list_user_settings(db_session, normal_user.id)] == ["b"]
    assert len(q.list_user_settings(db_session, admin_user.id)) == 2


def test_pointer_cleanup_spares_the_excluded_user(normal_user, admin_user, db_session):
    q.upsert_user_setting(db_session, normal_user.id, "ptr", "5")
    q.upsert_user_setting(db_session, admin_user.id, "ptr", "5")
    db_session.flush()
    assert sorted(q.list_user_ids_holding_value(db_session, "ptr", "5")) == sorted(
        [normal_user.id, admin_user.id]
    )
    q.delete_settings_holding_value(db_session, "ptr", "5", exclude_user_id=normal_user.id)
    assert q.list_user_ids_holding_value(db_session, "ptr", "5") == [normal_user.id]


def test_value_map_skips_empty_values(normal_user, db_session):
    q.upsert_user_setting(db_session, normal_user.id, "empty", "")
    q.upsert_user_setting(db_session, normal_user.id, "full", "v")
    db_session.flush()
    assert q.user_setting_values(db_session, normal_user.id, ["empty", "full"]) == {"full": "v"}


def test_users_by_id_returns_empty_for_no_ids_and_maps_known_ids(normal_user, db_session):
    assert q.users_by_id(db_session, set()) == {}
    assert q.users_by_id(db_session, {normal_user.id})[normal_user.id].id == normal_user.id


def test_no_query_helper_commits(normal_user, db_session):
    def run():
        q.upsert_user_setting(db_session, normal_user.id, "c", "1")
        q.find_user_setting(db_session, normal_user.id, "c")
        q.delete_user_settings(db_session, normal_user.id, ["c"])
        q.delete_settings_holding_value(db_session, "c", "1")

    assert _commits(db_session, run) == 0
