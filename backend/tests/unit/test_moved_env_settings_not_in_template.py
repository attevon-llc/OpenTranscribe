"""Settings that moved from ``.env`` to the admin UI must not come back as active env lines.

Sibling of ``test_env_ui_setting_drift.py``, which only covers the auth schemas. The YouTube
policy and the speech-detection (VAD) deployment defaults are plain ``SystemSettings`` keys
with no schema registry, so this derives the retired env names from the code that migrates or
replaces them (``youtube_policy.POLICY_KEYS``) and from the VAD field names.
"""

from __future__ import annotations

from tests.unit.test_env_ui_setting_drift import ENV_EXAMPLE
from tests.unit.test_env_ui_setting_drift import _active_env_keys


def _moved_env_names() -> set[str]:
    from app.services.transcription_defaults import VAD_FIELDS
    from app.services.youtube_policy import POLICY_KEYS

    youtube = {env for _key, env, _desc in POLICY_KEYS.values()}
    vad = {field.upper() for field in VAD_FIELDS}
    return youtube | vad


def test_the_registry_of_moved_names_is_populated():
    names = _moved_env_names()
    assert "YOUTUBE_USER_RATE_LIMIT_PER_HOUR" in names
    assert "VAD_MIN_SILENCE_MS" in names
    assert len(names) == 9


def test_the_detector_fires_on_a_moved_name():
    found = _active_env_keys("VAD_MIN_SILENCE_MS=1234\n")
    assert set(found) & _moved_env_names() == {"VAD_MIN_SILENCE_MS"}


def test_a_commented_moved_name_is_not_a_finding():
    assert not set(_active_env_keys("# YOUTUBE_AUTO_RETRY_ENABLED=true\n")) & _moved_env_names()


def test_no_moved_setting_is_an_active_line_in_env_example():
    active = _active_env_keys(ENV_EXAMPLE.read_text(encoding="utf-8"))
    back = {k: n for k, n in active.items() if k in _moved_env_names()}
    assert not back, f"moved to the admin UI (issue #566), remove from .env.example: {back}"
