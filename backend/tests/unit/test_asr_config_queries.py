"""ASR configuration queries in ``services.asr_config_queries``, against a real database."""

from __future__ import annotations

import uuid

from app.models.user_asr_settings import UserASRSettings
from app.services import asr_config_queries as q


def _config(db_session, user, name, **fields):
    row = UserASRSettings(user_id=user.id, name=name, provider="deepgram", model_name="m", **fields)
    db_session.add(row)
    db_session.flush()
    return row


def test_own_listing_and_existence_are_scoped_to_the_user(normal_user, admin_user, db_session):
    assert not q.user_has_asr_config(db_session, normal_user.id)
    mine = _config(db_session, normal_user, "mine")
    _config(db_session, admin_user, "theirs")
    assert q.user_has_asr_config(db_session, normal_user.id)
    assert [c.id for c in q.list_own_asr_configs(db_session, normal_user.id)] == [mine.id]


def test_lookup_by_uuid_hides_other_users_unless_shared_is_allowed(
    normal_user, admin_user, db_session
):
    private = _config(db_session, admin_user, "private")
    shared = _config(db_session, admin_user, "shared", is_shared=True)
    assert q.find_asr_config_by_uuid(db_session, private.uuid, normal_user.id) is None
    assert q.find_asr_config_by_uuid(db_session, shared.uuid, normal_user.id) is None
    found = q.find_asr_config_by_uuid(
        db_session, shared.uuid, normal_user.id, shared_org_id=None, include_shared=True
    )
    assert found is not None and found.id == shared.id
    assert (
        q.find_asr_config_by_uuid(
            db_session, private.uuid, normal_user.id, shared_org_id=None, include_shared=True
        )
        is None
    )


def test_shared_listing_and_tenant_lookup_see_only_other_users_shared_rows(
    normal_user, admin_user, db_session
):
    theirs = _config(db_session, admin_user, "theirs", is_shared=True)
    _config(db_session, admin_user, "private")
    _config(db_session, normal_user, "mine", is_shared=True)
    assert [c.id for c in q.list_shared_asr_configs(db_session, normal_user.id, None)] == [
        theirs.id
    ]
    found = q.find_shared_asr_config_in_tenant(db_session, theirs.id, None)
    assert found is not None and found.id == theirs.id


def test_admin_shared_listing_returns_config_owner_pairs_for_elevated_roles(
    normal_user, admin_user, db_session
):
    _config(db_session, normal_user, "user-shared", is_shared=True)
    admin_cfg = _config(db_session, admin_user, "admin-shared", is_shared=True)
    pairs = q.list_admin_shared_asr_configs(db_session, None, [admin_user.role])
    assert [(c.id, o.id) for c, o in pairs if c.id == admin_cfg.id] == [
        (admin_cfg.id, admin_user.id)
    ]
    assert all(o.role == admin_user.role for _, o in pairs)


def test_name_lookup_excludes_the_row_being_renamed(normal_user, db_session):
    mine = _config(db_session, normal_user, "mine")
    assert q.find_own_asr_config_by_name(db_session, normal_user.id, "mine") is not None
    assert (
        q.find_own_asr_config_by_name(db_session, normal_user.id, "mine", exclude_id=mine.id)
        is None
    )


def test_promotable_configs_skip_the_deleted_one_and_inactive_rows(normal_user, db_session):
    gone = _config(db_session, normal_user, "gone")
    ok = _config(db_session, normal_user, "ok")
    _config(db_session, normal_user, "off", is_active=False)
    assert [c.id for c in q.list_promotable_asr_configs(db_session, normal_user.id, gone.id)] == [
        ok.id
    ]


def test_uuid_lookup_by_id_and_delete_all(normal_user, db_session):
    mine = _config(db_session, normal_user, "mine")
    assert q.asr_config_uuid_by_id(db_session, mine.id) == mine.uuid
    assert q.asr_config_uuid_by_id(db_session, -1) is None
    q.delete_all_own_asr_configs(db_session, normal_user.id)
    assert not q.user_has_asr_config(db_session, normal_user.id)
    assert isinstance(mine.uuid, uuid.UUID)
