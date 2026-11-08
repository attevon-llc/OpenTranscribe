"""A language forced for one run reaches the ASR call, local and cloud (issue #1263).

The user's saved language is ``en`` in every test and the forced one is ``fr``, so a path that
ignores the forced language shows up as ``en``. Real ``UserSetting`` rows, real resolvers.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from unittest.mock import MagicMock

import pytest

from app.models.prompt import UserSetting
from app.services.asr.base import ASRProvider
from app.services.asr.locales import to_locale
from app.services.asr.types import ASRConfig
from app.services.asr.types import ASRResult
from app.tasks.transcription import cloud_asr
from app.tasks.transcription import pipelines
from app.transcription.config import TranscriptionConfig
from tests.unit._plan_contexts import make_plan


@contextlib.contextmanager
def _scope_of(db):
    yield db


class _StopError(Exception):
    pass


@pytest.fixture
def saved_english(db_session, normal_user, monkeypatch):
    db_session.add(
        UserSetting(
            user_id=normal_user.id,
            setting_key="transcription_source_language",
            setting_value="en",
        )
    )
    db_session.commit()
    monkeypatch.setattr("app.db.session_utils.session_scope", lambda: _scope_of(db_session))
    for module in (pipelines, cloud_asr):
        monkeypatch.setattr(module, "session_scope", lambda: _scope_of(db_session))
    monkeypatch.setattr(cloud_asr, "send_progress_notification", lambda *a, **k: None)
    monkeypatch.setattr(cloud_asr, "update_task_status", lambda *a, **k: None)
    return normal_user


class _CapturingProvider(ASRProvider):
    def __init__(self) -> None:
        self.config: ASRConfig | None = None

    @property
    def provider_name(self) -> str:
        return "deepgram"

    def supports_translation(self) -> bool:
        return True

    def supports_diarization(self) -> bool:
        return True

    def supports_vocabulary(self) -> bool:
        return False

    def validate_connection(self) -> tuple[bool, str, float]:
        return True, "", 0.0

    def transcribe(
        self,
        audio_path: str,
        config: ASRConfig,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> ASRResult:
        self.config = config
        raise _StopError


def _cloud_language(user, **kwargs) -> str:
    provider = _CapturingProvider()
    ctx = MagicMock(task_id="t", file_id=1, user_id=user.id)
    with pytest.raises(_StopError):
        cloud_asr._run_cloud_asr_pipeline(
            ctx,
            "/a.wav",
            None,
            None,
            None,
            provider=provider,
            plan=make_plan("cloud_speakers"),
            **kwargs,
        )
    assert provider.config is not None
    return provider.config.language


def test_cloud_asr_is_asked_for_the_forced_language(saved_english):
    assert _cloud_language(saved_english, source_language="fr") == "fr"


def test_cloud_asr_forced_auto_beats_a_saved_language(saved_english):
    assert _cloud_language(saved_english, source_language="auto") == "auto"


def test_cloud_asr_without_a_forced_language_uses_the_saved_one(saved_english):
    assert _cloud_language(saved_english) == "en"


def test_local_asr_is_configured_with_the_forced_language(saved_english, monkeypatch):
    seen: list[dict] = []

    def _record(cls, **overrides):
        seen.append(overrides)
        raise _StopError

    monkeypatch.setattr(TranscriptionConfig, "from_environment", classmethod(_record))
    monkeypatch.setattr(pipelines, "update_task_status", lambda *a, **k: None)
    monkeypatch.setattr(pipelines, "send_progress_notification", lambda *a, **k: None)
    monkeypatch.setattr(pipelines, "_load_file_vocabulary", lambda ctx: None)
    ctx = MagicMock(task_id="t", file_id=1, user_id=saved_english.id)
    with pytest.raises(_StopError):
        pipelines._run_transcription_pipeline(ctx, "/a.wav", None, None, None, source_language="fr")
    assert seen[0]["source_language"] == "fr"


@pytest.mark.parametrize(
    ("code", "locale"),
    [("fr", "fr-FR"), ("pt", "pt-BR"), ("fr-CA", "fr-CA"), ("xx", "xx"), ("EN", "en-US")],
)
def test_vendors_that_want_a_locale_get_one(code, locale):
    assert to_locale(code) == locale
