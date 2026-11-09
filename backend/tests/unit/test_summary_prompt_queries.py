"""Service-level behaviour of the summary-prompt queries."""

from __future__ import annotations

import uuid

from sqlalchemy import true

from app import models
from app.services import summary_prompt_queries as q


def _prompt(db, user_id, **kw) -> models.SummaryPrompt:
    p = models.SummaryPrompt(
        user_id=user_id,
        name=kw.pop("name", f"p-{uuid.uuid4().hex[:8]}"),
        prompt_text="Summarize.",
        content_type=kw.pop("content_type", "general"),
        is_system_default=kw.pop("is_system_default", False),
        is_active=kw.pop("is_active", True),
        is_shared=kw.pop("is_shared", False),
        tags=kw.pop("tags", []),
    )
    db.add(p)
    db.flush()
    return p


def test_active_setting_is_inserted_then_updated_not_duplicated(db_session, normal_user):
    first = _prompt(db_session, normal_user.id)
    second = _prompt(db_session, normal_user.id)
    q.set_active_prompt_setting(db_session, normal_user.id, first.id)
    db_session.flush()
    q.set_active_prompt_setting(db_session, normal_user.id, second.id)
    db_session.flush()
    rows = (
        db_session.query(models.UserSetting)
        .filter_by(user_id=normal_user.id, setting_key=q.ACTIVE_PROMPT_SETTING_KEY)
        .all()
    )
    assert [r.setting_value for r in rows] == [str(second.id)]


def test_clear_active_settings_can_spare_one_user(db_session, normal_user, admin_user):
    shared = _prompt(db_session, normal_user.id)
    q.set_active_prompt_setting(db_session, normal_user.id, shared.id)
    q.set_active_prompt_setting(db_session, admin_user.id, shared.id)
    db_session.flush()
    q.clear_active_prompt_settings(db_session, shared.id, except_user_id=normal_user.id)
    assert q.get_active_prompt_setting(db_session, normal_user.id) is not None
    assert q.get_active_prompt_setting(db_session, admin_user.id) is None
    q.clear_active_prompt_settings(db_session, shared.id)
    assert q.get_active_prompt_setting(db_session, normal_user.id) is None


def test_count_active_user_prompts_ignores_inactive(db_session, normal_user):
    before = q.count_active_user_prompts(db_session, normal_user.id)
    _prompt(db_session, normal_user.id)
    _prompt(db_session, normal_user.id, is_active=False)
    assert q.count_active_user_prompts(db_session, normal_user.id) == before + 1


def test_prompt_matches_applies_the_criterion(db_session, normal_user):
    p = _prompt(db_session, normal_user.id, is_shared=True)
    assert q.prompt_matches(db_session, p.id, models.SummaryPrompt.is_shared.is_(True))
    assert not q.prompt_matches(db_session, p.id, models.SummaryPrompt.is_shared.is_(False))
    assert not q.active_prompt_matches_uuid(db_session, str(uuid.uuid4()), true())


def test_active_prompt_matches_uuid_requires_active(db_session, normal_user):
    live = _prompt(db_session, normal_user.id)
    dead = _prompt(db_session, normal_user.id, is_active=False)
    assert q.active_prompt_matches_uuid(db_session, str(live.uuid), true())
    assert not q.active_prompt_matches_uuid(db_session, str(dead.uuid), true())


def test_users_by_id_empty_set_runs_no_lookup(db_session, normal_user):
    assert q.users_by_id(db_session, set()) == {}
    assert set(q.users_by_id(db_session, {normal_user.id})) == {normal_user.id}


def test_content_type_listing_filters_and_orders(db_session, normal_user):
    _prompt(db_session, normal_user.id, name="zz", content_type="podcast")
    _prompt(db_session, normal_user.id, name="aa", content_type="podcast")
    _prompt(db_session, normal_user.id, name="mid", content_type="meeting")
    rows = q.prompts_for_content_type(
        db_session, "podcast", models.SummaryPrompt.user_id == normal_user.id
    )
    assert [r.name for r in rows] == ["aa", "zz"]


def test_shared_library_tags_and_search(db_session, normal_user):
    _prompt(db_session, normal_user.id, is_shared=True, tags=["alpha", "beta"], name="needle one")
    visible = models.SummaryPrompt.user_id == normal_user.id
    page, total = q.shared_library_page(
        db_session,
        visible,
        content_type=None,
        tags="alpha",
        search="needle",
        sort_by="name",
        skip=0,
        limit=10,
    )
    assert total == 1 and page[0].name == "needle one"
    assert q.shared_library_tags(db_session, visible) == ["alpha", "beta"]


def _system_prompt(db, content_type: str) -> models.SummaryPrompt:
    """The system prompt for ``content_type`` (one per type is allowed), created when absent."""
    existing = (
        db.query(models.SummaryPrompt)
        .filter(
            models.SummaryPrompt.is_system_default,
            models.SummaryPrompt.content_type == content_type,
        )
        .first()
    )
    return existing or _prompt(db, None, is_system_default=True, content_type=content_type)


def test_default_system_prompt_falls_through_to_any_active_system_prompt(db_session):
    system = db_session.query(models.SummaryPrompt).filter(models.SummaryPrompt.is_system_default)
    system.update({"is_active": False})
    db_session.flush()
    assert q.default_system_prompt(db_session) is None
    meeting = _system_prompt(db_session, "meeting")
    meeting.is_active = True
    db_session.flush()
    assert q.default_system_prompt(db_session) is meeting
    general = _system_prompt(db_session, "general")
    general.is_active = True
    db_session.flush()
    assert q.default_system_prompt(db_session) is general
