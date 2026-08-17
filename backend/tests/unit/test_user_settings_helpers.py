"""Tests for ``app/utils/user_settings_helpers.py`` (issue #474).

``get_user_llm_output_language`` is a thin DB read with a coded fallback. Real behavior
worth pinning: it returns the stored value when a row exists, falls back to
``DEFAULT_LLM_OUTPUT_LANGUAGE`` when it doesn't, is scoped to the requesting user (does
not leak another user's setting), and is scoped to the specific ``setting_key`` (does
not return an unrelated setting under the same user). Uses the real ``db_session``
fixture (savepoint-rolled-back Postgres) rather than mocking the ORM — this is a query
function, so the query itself is what's under test.
"""

from __future__ import annotations

from app.core.constants import DEFAULT_LLM_OUTPUT_LANGUAGE
from app.models.prompt import UserSetting
from app.utils.user_settings_helpers import get_user_llm_output_language
from tests.user_owned_rows import make_user


def test_returns_default_when_no_setting_row_exists(db_session):
    user = make_user(db_session, "no-lang-setting")

    result = get_user_llm_output_language(db_session, user.id)

    assert result == DEFAULT_LLM_OUTPUT_LANGUAGE


def test_returns_stored_value_when_a_setting_row_exists(db_session):
    user = make_user(db_session, "has-lang-setting")
    db_session.add(
        UserSetting(
            user_id=user.id,
            setting_key="transcription_llm_output_language",
            setting_value="fr",
        )
    )
    db_session.commit()

    result = get_user_llm_output_language(db_session, user.id)

    assert result == "fr"


def test_does_not_leak_another_users_setting(db_session):
    owner = make_user(db_session, "lang-owner")
    other = make_user(db_session, "lang-other")
    db_session.add(
        UserSetting(
            user_id=owner.id,
            setting_key="transcription_llm_output_language",
            setting_value="de",
        )
    )
    db_session.commit()

    result = get_user_llm_output_language(db_session, other.id)

    assert result == DEFAULT_LLM_OUTPUT_LANGUAGE


def test_ignores_a_differently_keyed_setting_for_the_same_user(db_session):
    user = make_user(db_session, "wrong-key-setting")
    db_session.add(
        UserSetting(
            user_id=user.id,
            setting_key="some_other_setting",
            setting_value="es",
        )
    )
    db_session.commit()

    result = get_user_llm_output_language(db_session, user.id)

    assert result == DEFAULT_LLM_OUTPUT_LANGUAGE


def test_nonexistent_user_id_falls_back_to_default(db_session):
    result = get_user_llm_output_language(db_session, user_id=-1)

    assert result == DEFAULT_LLM_OUTPUT_LANGUAGE
