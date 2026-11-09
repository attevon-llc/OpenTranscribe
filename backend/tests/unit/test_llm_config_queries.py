"""LLM configuration queries in ``services.llm_config_queries``, against a real database."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime

from app.models.user_llm_settings import UserLLMSettings
from app.services import llm_config_queries as q


def _config(db_session, user, name, **fields):
    row = UserLLMSettings(
        user_id=user.id,
        name=name,
        provider="openai",
        model_name="m",
        **fields,
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_own_listing_counts_and_name_lookup_are_scoped_to_the_user(
    normal_user, admin_user, db_session
):
    mine = _config(db_session, normal_user, "mine")
    _config(db_session, admin_user, "mine")
    assert [c.id for c in q.list_own_llm_configs(db_session, normal_user.id)] == [mine.id]
    assert q.count_own_llm_configs(db_session, normal_user.id) == 1
    found = q.find_own_llm_config_by_name(db_session, normal_user.id, "mine")
    assert found is not None and found.id == mine.id


def test_name_lookup_can_exclude_the_row_being_renamed(normal_user, db_session):
    mine = _config(db_session, normal_user, "mine")
    assert (
        q.find_own_llm_config_by_name(db_session, normal_user.id, "mine", exclude_id=mine.id)
        is None
    )


def test_visibility_covers_own_rows_and_shared_rows_but_not_another_users_private_row(
    normal_user, admin_user, db_session
):
    private = _config(db_session, admin_user, "private", is_shared=False)
    shared = _config(db_session, admin_user, "shared", is_shared=True, shared_at=datetime.now(UTC))
    own = _config(db_session, normal_user, "own")
    assert q.find_visible_llm_config(db_session, own.id, normal_user.id, None) is not None
    assert q.llm_config_is_visible(db_session, shared.id, normal_user.id, None)
    assert not q.llm_config_is_visible(db_session, private.id, normal_user.id, None)
    assert q.find_visible_llm_config(db_session, private.id, normal_user.id, None) is None


def test_shared_listing_excludes_the_callers_own_shared_rows(normal_user, admin_user, db_session):
    _config(db_session, normal_user, "mine", is_shared=True, shared_at=datetime.now(UTC))
    theirs = _config(db_session, admin_user, "theirs", is_shared=True, shared_at=datetime.now(UTC))
    ids = [c.id for c in q.list_shared_llm_configs(db_session, normal_user.id, None)]
    assert theirs.id in ids
    assert all(
        c.user_id != normal_user.id
        for c in q.list_shared_llm_configs(db_session, normal_user.id, None)
    )


def test_deactivating_others_keeps_the_named_row_and_other_users_untouched(
    normal_user, admin_user, db_session
):
    keep = _config(db_session, normal_user, "keep", is_active=True)
    other = _config(db_session, normal_user, "other", is_active=True)
    foreign = _config(db_session, admin_user, "foreign", is_active=True)
    q.deactivate_other_llm_configs(db_session, normal_user.id, keep.id)
    db_session.refresh(keep)
    db_session.refresh(other)
    db_session.refresh(foreign)
    assert (keep.is_active, other.is_active, foreign.is_active) == (True, False, True)


def test_other_config_lookup_and_delete_all(normal_user, db_session):
    first = _config(db_session, normal_user, "first")
    second = _config(db_session, normal_user, "second")
    other = q.find_other_own_llm_config(db_session, normal_user.id, first.id)
    assert other is not None and other.id == second.id
    assert q.delete_all_own_llm_configs(db_session, normal_user.id) == 2
    assert q.count_own_llm_configs(db_session, normal_user.id) == 0
