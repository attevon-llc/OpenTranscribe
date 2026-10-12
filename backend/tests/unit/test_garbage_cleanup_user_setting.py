"""The user's garbage-cleanup preference must change the transcript that is saved (#1199).

Before the fix the pipeline read only the admin ``transcription.*`` system settings, so the
Settings -> Transcription -> Accuracy & Cleanup toggle and threshold were stored and never consulted. These
tests run the real cleanup entry point both finalize paths call and compare the output
text, not just which setting was read.

Precedence under test: user preference > system default > constant.
"""

from __future__ import annotations

import pytest

from app.models.prompt import UserSetting
from app.services import system_settings_service
from app.tasks.transcription.finalize import apply_garbage_cleanup

NOISE_40 = "x" * 40
NOISE_60 = "x" * 60


def _segments() -> list[dict]:
    return [{"text": f"before {NOISE_40} middle {NOISE_60} after"}]


def _user_pref(db, user, key: str, value: str) -> None:
    db.add(UserSetting(user_id=user.id, setting_key=key, setting_value=value))
    db.commit()


def _system(db, enabled: bool | None = None, max_len: int | None = None) -> None:
    if enabled is not None:
        system_settings_service.set_setting(db, "transcription.garbage_cleanup_enabled", enabled)
    if max_len is not None:
        system_settings_service.set_setting(db, "transcription.max_word_length", max_len)
    db.commit()


def test_user_disabling_cleanup_wins_over_an_enabled_system_default(db_session, normal_user):
    _system(db_session, enabled=True, max_len=50)
    _user_pref(db_session, normal_user, "transcription_garbage_cleanup_enabled", "false")

    out, count = apply_garbage_cleanup(db_session, normal_user.id, _segments())

    assert count == 0
    assert out[0]["text"] == _segments()[0]["text"]


def test_user_threshold_replaces_a_token_the_system_value_would_keep(db_session, normal_user):
    _system(db_session, enabled=True, max_len=50)
    _user_pref(db_session, normal_user, "transcription_garbage_cleanup_threshold", "30")

    out, count = apply_garbage_cleanup(db_session, normal_user.id, _segments())

    assert count == 2
    assert out[0]["text"] == "before [background noise] middle [background noise] after"


def test_user_enabling_cleanup_wins_over_a_disabled_system_default(db_session, normal_user):
    _system(db_session, enabled=False, max_len=50)
    _user_pref(db_session, normal_user, "transcription_garbage_cleanup_enabled", "true")

    _, count = apply_garbage_cleanup(db_session, normal_user.id, _segments())

    assert count == 1  # only the 60-char token exceeds the inherited 50


def test_a_user_with_no_saved_value_inherits_the_system_default(db_session, normal_user):
    _system(db_session, enabled=True, max_len=35)

    out, count = apply_garbage_cleanup(db_session, normal_user.id, _segments())

    assert count == 2
    assert NOISE_40 not in out[0]["text"]


def test_system_disabled_and_no_user_value_leaves_text_alone(db_session, normal_user):
    _system(db_session, enabled=False)

    out, count = apply_garbage_cleanup(db_session, normal_user.id, _segments())

    assert count == 0
    assert out[0]["text"] == _segments()[0]["text"]


def test_another_users_preference_does_not_leak(db_session, normal_user, admin_user):
    _system(db_session, enabled=True, max_len=50)
    _user_pref(db_session, admin_user, "transcription_garbage_cleanup_enabled", "false")

    _, count = apply_garbage_cleanup(db_session, normal_user.id, _segments())

    assert count == 1


@pytest.mark.parametrize(
    ("stored", "replaced"),
    [
        ("5", 2),  # clamps up to 20: ordinary words are never "too long"
        ("9999", 0),  # clamps down to 200
        ("junk", 1),  # unparseable: falls back to the system 50
    ],
)
def test_out_of_range_or_garbled_stored_threshold_is_bounded(
    db_session, normal_user, stored, replaced
):
    _system(db_session, enabled=True, max_len=50)
    _user_pref(db_session, normal_user, "transcription_garbage_cleanup_threshold", stored)

    _, count = apply_garbage_cleanup(db_session, normal_user.id, _segments())

    assert count == replaced
