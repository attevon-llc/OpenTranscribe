"""``POST /files/{uuid}/summarize`` fills in missing topic extraction (AI tags and collections).

Owner report: files uploaded before any LLM provider existed had topic extraction skipped (no
provider), then a provider was added and "Generate summary" produced only the summary. The
summary request now also queues topic extraction when the file never had it, the owner's
automatic topic extraction switch is on, and no run is already in flight
(``services/ai_insights_companion.py``, docs/design/llm_added_regenerate_plan.md).

Real ``MediaFile`` / ``TopicSuggestion`` / ``UserSetting`` / ``Task`` rows on the savepoint
session. Only the edges are replaced: the Celery ``.delay`` (recorded), the WebSocket send
(recorded), Redis (an in-memory NX store) and the LLM availability probes.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

from fastapi import status

from app.models.media import MediaFile
from app.models.media import Task
from app.models.media import TranscriptSegment
from app.models.prompt import UserSetting
from app.models.topic import TopicSuggestion


class _NxRedis:
    """The two Redis calls the guard makes, with real SET NX semantics."""

    def __init__(self) -> None:
        self.keys: dict[str, str] = {}

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool | None:
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True

    def delete(self, key: str) -> int:
        return 1 if self.keys.pop(key, None) is not None else 0


class _DownRedis:
    def set(self, *_a, **_k):
        raise ConnectionError("redis down")

    def delete(self, *_a, **_k):
        raise ConnectionError("redis down")


def _make_file(db_session, owner, **overrides) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    mf = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename="companion_test.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status="completed",
        **overrides,
    )
    db_session.add(mf)
    db_session.commit()
    db_session.refresh(mf)
    db_session.add(
        TranscriptSegment(media_file_id=mf.id, start_time=0.0, end_time=1.0, text="hello there")
    )
    db_session.commit()
    db_session.refresh(mf)
    return mf


@contextmanager
def _stack(*, llm_configured: bool = True, redis=None):
    """LLM available to the caller, owner configured or not, Celery + WS recorded."""
    from app.tasks import topic_extraction

    fake_redis = redis if redis is not None else _NxRedis()
    delay = MagicMock(return_value=MagicMock(id="topic-task"))
    notify = MagicMock(return_value=True)
    with (
        patch("app.api.endpoints.summarization.is_llm_available", new=AsyncMock(return_value=True)),
        patch(
            "app.services.llm_service.LLMService.is_configured_for_user",
            return_value=llm_configured,
        ),
        patch("app.core.redis.get_redis", return_value=fake_redis),
        patch.object(topic_extraction.extract_topics_task, "delay", delay),
        patch.object(topic_extraction, "send_topic_extraction_notification", notify),
    ):
        yield delay, notify


def _summarize(client, headers, mf, **body):
    return client.post(f"/api/files/{mf.uuid}/summarize", headers=headers, json=body or None)


def test_queues_topic_extraction_when_the_file_never_had_it(
    client, user_token_headers, normal_user, db_session
):
    mf = _make_file(db_session, normal_user)
    with _stack() as (delay, notify):
        response = _summarize(client, user_token_headers, mf)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["topic_extraction"] == "queued"
    delay.assert_called_once_with(file_uuid=str(mf.uuid), force_regenerate=False)
    # The chip on the consolidated card comes from the existing event, as "queued".
    notify.assert_called_once()
    assert notify.call_args.kwargs["status"] == "queued"
    assert notify.call_args.kwargs["file_id"] == mf.id
    assert notify.call_args.kwargs["user_id"] == normal_user.id


def test_regenerate_also_fills_in_missing_topics(
    client, user_token_headers, normal_user, db_session
):
    mf = _make_file(db_session, normal_user)
    with _stack() as (delay, _notify):
        response = _summarize(client, user_token_headers, mf, force_regenerate=True)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["topic_extraction"] == "queued"
    assert delay.call_count == 1


def test_does_not_rerun_when_a_suggestion_already_exists(
    client, user_token_headers, normal_user, db_session
):
    mf = _make_file(db_session, normal_user)
    db_session.add(
        TopicSuggestion(
            media_file_id=mf.id,
            user_id=normal_user.id,
            suggested_tags=[{"name": "budget", "confidence": 0.9}],
            suggested_collections=[],
            status="pending",
        )
    )
    db_session.commit()

    with _stack() as (delay, notify):
        response = _summarize(client, user_token_headers, mf)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["topic_extraction"] == "already_extracted"
    delay.assert_not_called()
    notify.assert_not_called()


def test_respects_the_owners_automatic_topic_extraction_switch(
    client, user_token_headers, normal_user, db_session
):
    mf = _make_file(db_session, normal_user)
    db_session.add(
        UserSetting(
            user_id=normal_user.id, setting_key="auto_topic_extraction", setting_value="false"
        )
    )
    db_session.commit()

    with _stack() as (delay, notify):
        response = _summarize(client, user_token_headers, mf)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["topic_extraction"] == "disabled_by_setting"
    delay.assert_not_called()
    notify.assert_not_called()


def test_a_second_click_does_not_queue_a_second_run(
    client, user_token_headers, normal_user, db_session
):
    mf = _make_file(db_session, normal_user)
    with _stack() as (delay, _notify):
        first = _summarize(client, user_token_headers, mf)
        second = _summarize(client, user_token_headers, mf)

    assert first.status_code == status.HTTP_200_OK, first.text
    assert second.status_code == status.HTTP_200_OK, second.text
    assert first.json()["topic_extraction"] == "queued"
    assert second.json()["topic_extraction"] == "in_progress"
    assert delay.call_count == 1


def test_a_run_already_in_flight_is_not_doubled(
    client, user_token_headers, normal_user, db_session
):
    mf = _make_file(db_session, normal_user)
    db_session.add(
        Task(
            id=f"topic-{uuid.uuid4()}",
            user_id=normal_user.id,
            media_file_id=mf.id,
            task_type="topic_extraction",
            status="in_progress",
        )
    )
    db_session.commit()

    with _stack() as (delay, _notify):
        response = _summarize(client, user_token_headers, mf)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["topic_extraction"] == "in_progress"
    delay.assert_not_called()


def test_redis_down_fails_closed_and_the_summary_still_queues(
    client, user_token_headers, normal_user, db_session
):
    mf = _make_file(db_session, normal_user)
    with _stack(redis=_DownRedis()) as (delay, _notify):
        response = _summarize(client, user_token_headers, mf)

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["message"] == "Summarization task started"
    assert body["topic_extraction"] == "unavailable"
    delay.assert_not_called()


def test_owner_without_an_llm_is_not_queued(client, user_token_headers, normal_user, db_session):
    """A run for an owner with no provider would only skip, stranding a queued chip."""
    mf = _make_file(db_session, normal_user)
    with _stack(llm_configured=False) as (delay, notify):
        response = _summarize(client, user_token_headers, mf)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["topic_extraction"] == "unavailable"
    delay.assert_not_called()
    notify.assert_not_called()


def test_quarantined_file_dispatches_nothing(client, user_token_headers, normal_user, db_session):
    mf = _make_file(db_session, normal_user, is_quarantined=True)
    with _stack() as (delay, notify):
        response = _summarize(client, user_token_headers, mf)

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    delay.assert_not_called()
    notify.assert_not_called()


def test_another_users_file_dispatches_nothing(
    client, other_user_auth_headers, normal_user, db_session
):
    mf = _make_file(db_session, normal_user)
    with _stack() as (delay, notify):
        response = _summarize(client, other_user_auth_headers, mf)

    assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
    delay.assert_not_called()
    notify.assert_not_called()


def test_a_file_in_another_tenant_dispatches_nothing(
    client, user_token_headers, normal_user, db_session
):
    """The owner acting in their personal workspace cannot reach a file of an organization."""
    from app.models.organization import Organization

    org = Organization(
        external_org_id=f"org_companion_{uuid.uuid4().hex[:8]}",
        name="Companion Org",
        is_active=True,
    )
    db_session.add(org)
    db_session.commit()
    mf = _make_file(db_session, normal_user, organization_id=org.id)

    with _stack() as (delay, notify):
        response = _summarize(client, user_token_headers, mf)

    assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
    delay.assert_not_called()
    notify.assert_not_called()
