"""Issue #1017: the recovery sweep must not loop on topic extraction with no usable LLM.

With ``LLM_PROVIDER`` set but no credentials, three defects combined into an
unbounded loop: every 10-minute post-transcription sweep re-dispatched
``ai.extract_topics`` for every completed file, and each dispatch left one more
``task`` row stuck ``in_progress``.

1. The task created its row (and set ``MediaFile.active_task_id``) *before*
   resolving a provider, and its "no LLM" early return never closed it.
2. ``missing_topics`` ignored ``recently_attempted``, unlike every other
   post-transcription check.
3. The detector's "LLM configured" check was ``bool(settings.LLM_PROVIDER)``,
   while the task builds its client through ``LLMService.create_from_settings``,
   which also needs credentials or an endpoint.

Settings are pinned per test (never read from the host ``.env``), and
``app.db.base.SessionLocal`` — which the LLM factories open for themselves — is
pointed at the savepointed test session so they see the rows each test creates.
Candidate files are dated to year 1000 so they sort ahead of anything else in
the ``completed_at ASC`` candidate window.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime

import pytest

from app.core.config import settings
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.models.prompt import UserSetting
from app.models.user_llm_settings import UserLLMSettings
from app.services.llm_service import LLMService
from app.services.task_detection_service import TaskDetectionService
from app.services.task_recovery_service import TaskRecoveryService
from app.utils.encryption import encrypt_api_key

VERY_OLD = datetime(1000, 1, 1, tzinfo=UTC)

#: Every setting the system-level resolution reads, at a "nothing configured" value.
_BLANK_SYSTEM_LLM = {
    "LLM_PROVIDER": "",
    "OPENAI_API_KEY": None,
    "OPENAI_MODEL_NAME": "gpt-4o-mini",
    "OPENAI_BASE_URL": "https://api.openai.com/v1",
    "ANTHROPIC_API_KEY": None,
    "ANTHROPIC_MODEL_NAME": "claude-test",
    "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
    "OPENROUTER_API_KEY": None,
    "OPENROUTER_MODEL_NAME": "openrouter/test",
    "OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1",
    "VLLM_API_KEY": None,
    "VLLM_MODEL_NAME": "gpt-oss",
    "VLLM_BASE_URL": "http://localhost:8012/v1",
    "OLLAMA_MODEL_NAME": "",
    "OLLAMA_BASE_URL": "http://ollama:11434",
    "BEDROCK_MODEL_NAME": "",
    "BEDROCK_REGION": "",
}


class _NoCloseSession:
    """The fixture session, with close() neutered — the factories close their own."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def close(self) -> None:
        pass


@pytest.fixture
def system_llm(monkeypatch, db_session):
    """Pin the system LLM settings; returns a setter for per-test overrides."""
    monkeypatch.setattr("app.db.base.SessionLocal", lambda: _NoCloseSession(db_session))
    for key, value in _BLANK_SYSTEM_LLM.items():
        monkeypatch.setattr(settings, key, value)

    def _set(**overrides):
        for key, value in overrides.items():
            monkeypatch.setattr(settings, key, value)

    return _set


def _completed_file(db, user) -> MediaFile:
    media_file = MediaFile(
        user_id=user.id,
        filename=f"f-{uuid.uuid4().hex[:8]}.wav",
        storage_path=f"user_{user.id}/{uuid.uuid4().hex[:8]}.wav",
        file_size=1024,
        content_type="audio/wav",
        status=FileStatus.COMPLETED,
        completed_at=VERY_OLD,
    )
    db.add(media_file)
    db.commit()
    db.refresh(media_file)
    return media_file


def _sweep(db) -> tuple[list, dict[str, int]]:
    """One post-transcription recovery tick: detect, then dispatch."""
    incomplete = TaskDetectionService().identify_incomplete_post_transcription_files(db)
    stats = TaskRecoveryService().recover_incomplete_post_transcription_files(db, incomplete)
    return incomplete, stats


def _ours(incomplete, media_file):
    matches = [r for r in incomplete if r.media_file_id == media_file.id]
    assert len(matches) == 1, "the file must be reported: analytics and indexing are missing"
    return matches[0]


# --------------------------------------------------------------------------- #
# (a) the skip path leaves no in_progress row and no active_task_id
# --------------------------------------------------------------------------- #
def test_skipped_topic_extraction_leaves_no_in_progress_row(
    db_session, normal_user, system_llm, monkeypatch
):
    from app.tasks import topic_extraction as tex

    system_llm(LLM_PROVIDER="anthropic")  # provider named, no API key

    @contextmanager
    def _scope():
        yield db_session

    monkeypatch.setattr(tex, "session_scope", _scope)
    media_file = _completed_file(db_session, normal_user)

    result = tex.extract_topics_task.apply(args=[str(media_file.uuid)]).get()

    assert result == {"status": "skipped", "reason": "LLM not configured"}
    db_session.expire_all()
    open_rows = (
        db_session.query(Task)
        .filter(
            Task.media_file_id == media_file.id,
            Task.task_type == "topic_extraction",
            Task.status.in_(["pending", "in_progress"]),
        )
        .count()
    )
    assert open_rows == 0, "a skipped run must not leave a task row open"
    refreshed = db_session.query(MediaFile).filter(MediaFile.id == media_file.id).one()
    assert refreshed.active_task_id is None


# --------------------------------------------------------------------------- #
# (b) consecutive sweeps
# --------------------------------------------------------------------------- #
def test_two_sweeps_without_llm_credentials_dispatch_no_llm_work(
    db_session, normal_user, system_llm
):
    system_llm(LLM_PROVIDER="anthropic")  # provider named, no API key
    media_file = _completed_file(db_session, normal_user)

    for sweep in (1, 2):
        incomplete, stats = _sweep(db_session)
        ours = _ours(incomplete, media_file)
        assert ours.missing_topics is False, f"sweep {sweep}"
        assert stats["topics_dispatched"] == 0, f"sweep {sweep}: {stats}"
        assert stats["summaries_dispatched"] == 0, f"sweep {sweep}: {stats}"
        assert stats["speaker_id_dispatched"] == 0, f"sweep {sweep}: {stats}"


def test_a_recent_topic_extraction_attempt_is_not_re_dispatched(
    db_session, normal_user, system_llm
):
    from app.utils.task_utils import create_task_record
    from app.utils.task_utils import update_task_status

    system_llm(LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="sk-test-not-real")
    media_file = _completed_file(db_session, normal_user)

    _, first = _sweep(db_session)
    assert first["topics_dispatched"] == 1, first

    # The dispatched run records itself and fails, as it would against a
    # provider that is configured but erroring.
    task_id = f"topic-{uuid.uuid4()}"
    create_task_record(db_session, task_id, normal_user.id, media_file.id, "topic_extraction")
    update_task_status(db_session, task_id, "failed", error_message="provider 502", completed=True)

    incomplete, second = _sweep(db_session)
    assert _ours(incomplete, media_file).missing_topics is False
    assert second["topics_dispatched"] == 0, second


# --------------------------------------------------------------------------- #
# (c) "LLM configured" means what create_from_settings builds
# --------------------------------------------------------------------------- #
def test_llm_provider_without_credentials_is_not_configured_for_detection(
    db_session, normal_user, system_llm
):
    system_llm(LLM_PROVIDER="anthropic")
    media_file = _completed_file(db_session, normal_user)

    incomplete = TaskDetectionService().identify_incomplete_post_transcription_files(db_session)

    assert _ours(incomplete, media_file).llm_configured is False


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, False),
        ({"LLM_PROVIDER": "anthropic"}, False),
        ({"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "sk-test"}, True),
        ({"LLM_PROVIDER": "openai"}, False),
        ({"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-test"}, True),
        ({"LLM_PROVIDER": "openrouter"}, False),
        ({"LLM_PROVIDER": "vllm"}, False),  # shipped placeholder model + localhost URL
        (
            {
                "LLM_PROVIDER": "vllm",
                "VLLM_MODEL_NAME": "served-model",
                "VLLM_BASE_URL": "http://vllm:8000/v1",
            },
            True,
        ),
        ({"LLM_PROVIDER": "ollama"}, False),  # no model
        ({"LLM_PROVIDER": "ollama", "OLLAMA_MODEL_NAME": "llama3"}, True),
        ({"LLM_PROVIDER": "bedrock", "BEDROCK_MODEL_NAME": "anthropic.claude"}, False),
        (
            {
                "LLM_PROVIDER": "bedrock",
                "BEDROCK_MODEL_NAME": "anthropic.claude",
                "BEDROCK_REGION": "us-east-1",
            },
            True,
        ),
        ({"LLM_PROVIDER": "custom"}, False),  # needs a per-user configuration
        ({"LLM_PROVIDER": "not-a-provider"}, False),
    ],
)
def test_system_settings_predicate_matches_the_factory(
    db_session, normal_user, system_llm, overrides, expected
):
    system_llm(**overrides)

    built = LLMService.create_from_settings(user_id=normal_user.id)

    assert (built is not None) is expected
    assert LLMService.is_configured_for_user(db_session, normal_user.id) is expected


def _user_config(db, owner, *, provider="openai", base_url=None, api_key="sk-test", shared=False):
    config = UserLLMSettings(
        user_id=owner.id,
        name=f"cfg-{uuid.uuid4().hex[:6]}",
        provider=provider,
        model_name="some-model",
        api_key=encrypt_api_key(api_key) if api_key else None,
        base_url=base_url,
        max_tokens=8192,
        temperature="0.3",
        is_shared=shared,
    )
    db.add(config)
    db.commit()
    db.refresh(config)
    return config


def _activate(db, user, value: str) -> None:
    db.add(UserSetting(user_id=user.id, setting_key="active_llm_config_id", setting_value=value))
    db.commit()


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("own_config", True),
        ("own_vllm_without_base_url", False),
        ("own_vllm_with_base_url", True),
        ("other_users_private_config", False),
        ("other_users_shared_config", True),
        ("dangling_id", False),
        ("non_integer_id", False),
    ],
)
def test_user_settings_predicate_matches_the_factory(
    db_session, normal_user, other_user, system_llm, case, expected
):
    if case == "own_config":
        _activate(db_session, normal_user, str(_user_config(db_session, normal_user).id))
    elif case == "own_vllm_without_base_url":
        cfg = _user_config(db_session, normal_user, provider="vllm", api_key=None)
        _activate(db_session, normal_user, str(cfg.id))
    elif case == "own_vllm_with_base_url":
        cfg = _user_config(
            db_session, normal_user, provider="vllm", api_key=None, base_url="http://vllm:8000/v1"
        )
        _activate(db_session, normal_user, str(cfg.id))
    elif case == "other_users_private_config":
        _activate(db_session, normal_user, str(_user_config(db_session, other_user).id))
    elif case == "other_users_shared_config":
        cfg = _user_config(db_session, other_user, shared=True)
        _activate(db_session, normal_user, str(cfg.id))
    elif case == "dangling_id":
        _activate(db_session, normal_user, "2147483000")
    elif case == "non_integer_id":
        _activate(db_session, normal_user, "not-a-number")

    built = LLMService.create_from_settings(user_id=normal_user.id)

    assert (built is not None) is expected
    assert LLMService.is_configured_for_user(db_session, normal_user.id) is expected
