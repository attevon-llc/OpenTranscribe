"""Prove the #914 service-layer exception-echo fixes actually sanitize responses.

Sibling of ``test_error_detail_sanitization.py`` (#859/#891, the API-layer half);
this file covers the Tier-1 (caller-reachable, non-privileged) service-layer
chains fixed in the #914 sweep. Every test asserts THREE things: the status
code, that a planted SENTINEL is ABSENT from the response body, and that it IS
present in ``caplog`` -- a test that only checks the status code would pass
against the pre-fix code too and prove nothing.
"""

from __future__ import annotations

import logging
import uuid as uuid_pkg

from fastapi import status

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import Task as TaskModel
from app.models.watch_source import WatchSource
from app.models.watch_source import WatchSourceFile

#: The #891 exemplar shape: a broker URL with an embedded credential.
BROKER_SENTINEL = "redis://:hunter2@broker-internal:6379/0"
#: A host filesystem path -- never safe to echo.
PATH_SENTINEL = "/mnt/nas/secret-share"
#: An internal hostname:port -- the deployment's own LLM endpoint.
LLM_HOST_SENTINEL = "http://internal-vllm.lan:5195"


def _make_file(db_session, owner, **overrides) -> MediaFile:
    file_uuid = str(uuid_pkg.uuid4())
    defaults = {
        "uuid": file_uuid,
        "user_id": owner.id,
        "filename": "tier1_sanitize_test.wav",
        "storage_path": f"media/test/{file_uuid}.wav",
        "content_type": "audio/wav",
        "file_size": 1024,
        "status": "completed",
    }
    defaults.update(overrides)
    media_file = MediaFile(**defaults)
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


def _make_watch_source(db_session, owner, **overrides) -> WatchSource:
    defaults = {
        "uuid": uuid_pkg.uuid4(),
        "user_id": owner.id,
        "created_by": owner.id,
        "name": f"watch-{uuid_pkg.uuid4().hex[:8]}",
        "source_type": "local",
        "is_enabled": True,
        "local_path": ".",
        "auto_transcribe": True,
    }
    defaults.update(overrides)
    source = WatchSource(**defaults)
    db_session.add(source)
    db_session.commit()
    db_session.refresh(source)
    return source


def _make_watch_source_file(db_session, source, **overrides) -> WatchSourceFile:
    defaults = {
        "uuid": uuid_pkg.uuid4(),
        "watch_source_id": source.id,
        "remote_path": f"/watch/{uuid_pkg.uuid4().hex}.wav",
        "filename": "recording.wav",
        "status": "importing",
    }
    defaults.update(overrides)
    row = WatchSourceFile(**defaults)
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


# --------------------------------------------------------------------------- #
# 1. Watch-source pipeline dispatch -> GET /watch-sources/{uuid}/files
# --------------------------------------------------------------------------- #


def test_watch_source_dispatch_failure_never_leaks_the_broker_url(
    db_session, admin_user, admin_token_headers, client, monkeypatch, caplog
):
    """A second route leaking the #891 broker credential (the first was
    services/search/model_switch.py::dispatch_reindex_for_every_owner)."""
    from app.services.watch_sources import processing

    source = _make_watch_source(db_session, admin_user)
    media_file = _make_file(db_session, admin_user)
    row = _make_watch_source_file(
        db_session, source, status="importing", media_file_id=media_file.id
    )

    def _raise(*_args, **_kwargs):
        raise RuntimeError(f"Retry limit exceeded reconnecting to {BROKER_SENTINEL}")

    monkeypatch.setattr(
        "app.api.endpoints.files.upload.dispatch_upload_pipeline",
        _raise,
    )

    with caplog.at_level(logging.ERROR):
        dispatch_error = processing._dispatch_pipeline(media_file, admin_user.id, source)

    assert dispatch_error is not None
    assert BROKER_SENTINEL not in dispatch_error
    assert "hunter2" not in dispatch_error
    assert BROKER_SENTINEL in caplog.text

    row.error_message = dispatch_error[:2000]
    db_session.commit()

    response = client.get(f"/api/watch-sources/{source.uuid}/files", headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert BROKER_SENTINEL not in response.text
    assert "hunter2" not in response.text
    found = next(f for f in response.json()["files"] if f["uuid"] == str(row.uuid))
    assert found["error_message"] == dispatch_error[:2000]


# --------------------------------------------------------------------------- #
# 2. file_cleanup_service.purge_media_file -> DELETE /api/files/{uuid}
# --------------------------------------------------------------------------- #


def test_purge_media_file_failure_never_leaks_exception_text(
    db_session, normal_user, user_token_headers, client, monkeypatch, caplog
):
    media_file = _make_file(db_session, normal_user)

    def _raise(*_args, **_kwargs):
        raise RuntimeError(f"could not delete row: connection lost to {PATH_SENTINEL}")

    monkeypatch.setattr(db_session, "delete", _raise)

    with caplog.at_level(logging.ERROR):
        response = client.delete(f"/api/files/{media_file.uuid}", headers=user_token_headers)

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert PATH_SENTINEL not in response.text
    assert PATH_SENTINEL in caplog.text


# --------------------------------------------------------------------------- #
# 3. SpeakerClusteringService.batch_verify_speakers -> POST /speaker-clusters/batch-verify
# --------------------------------------------------------------------------- #


def test_batch_verify_speakers_commit_failure_never_leaks_exception_text(
    db_session, normal_user, user_token_headers, client, monkeypatch, caplog
):
    media_file = _make_file(db_session, normal_user)
    speaker = Speaker(
        uuid=uuid_pkg.uuid4(),
        user_id=normal_user.id,
        media_file_id=media_file.id,
        name="SPEAKER_00",
        suggested_name="Alice",
        verified=False,
    )
    db_session.add(speaker)
    db_session.commit()
    db_session.refresh(speaker)

    def _raise_commit(*_args, **_kwargs):
        raise RuntimeError(f"commit failed against {PATH_SENTINEL}")

    monkeypatch.setattr(db_session, "commit", _raise_commit)

    with caplog.at_level(logging.ERROR):
        response = client.post(
            "/api/speaker-clusters/batch-verify",
            headers=user_token_headers,
            json={"speaker_uuids": [str(speaker.uuid)], "action": "accept"},
        )

    assert response.status_code == status.HTTP_200_OK
    assert PATH_SENTINEL not in response.text
    assert PATH_SENTINEL in caplog.text
    body = response.json()
    assert body["failed_count"] == 1
    assert all(PATH_SENTINEL not in e for e in body["errors"])


def test_batch_verify_speakers_per_speaker_failure_never_leaks_exception_text(
    db_session, normal_user, user_token_headers, client, monkeypatch, caplog
):
    """The scanner-invisible finding: ``errors.append(...)`` inside the loop,
    reached via the happy-path ``return`` several lines later."""
    media_file = _make_file(db_session, normal_user)
    speaker = Speaker(
        uuid=uuid_pkg.uuid4(),
        user_id=normal_user.id,
        media_file_id=media_file.id,
        name="SPEAKER_00",
        display_name=None,
        suggested_name=None,  # no suggestion -> "accept" fails per-speaker, not via commit
        verified=False,
    )
    db_session.add(speaker)
    db_session.commit()
    db_session.refresh(speaker)

    from app.services import speaker_clustering_service as scs

    real_label = scs.canonical_speaker_label_for_row

    def _raise(*_args, **_kwargs):
        raise RuntimeError(f"label resolution failed near {PATH_SENTINEL}")

    monkeypatch.setattr(scs, "canonical_speaker_label_for_row", _raise)

    with caplog.at_level(logging.ERROR):
        response = client.post(
            "/api/speaker-clusters/batch-verify",
            headers=user_token_headers,
            json={"speaker_uuids": [str(speaker.uuid)], "action": "accept"},
        )

    monkeypatch.setattr(scs, "canonical_speaker_label_for_row", real_label)

    assert response.status_code == status.HTTP_200_OK
    assert PATH_SENTINEL not in response.text
    assert PATH_SENTINEL in caplog.text


# --------------------------------------------------------------------------- #
# 4. tag_bulk.apply_tag_to_file -> POST /files/management/bulk-action
# --------------------------------------------------------------------------- #


def test_bulk_tag_flush_failure_never_leaks_exception_text(
    db_session, normal_user, user_token_headers, client, monkeypatch, caplog
):
    media_file = _make_file(db_session, normal_user)

    def _raise_flush(*_args, **_kwargs):
        raise RuntimeError(f"flush failed against {PATH_SENTINEL}")

    monkeypatch.setattr(db_session, "flush", _raise_flush)

    with caplog.at_level(logging.ERROR):
        response = client.post(
            "/api/files/management/bulk-action",
            headers=user_token_headers,
            json={
                "file_uuids": [str(media_file.uuid)],
                "action": "add_tag",
                "tag_name": f"tier1-tag-{uuid_pkg.uuid4().hex[:8]}",
            },
        )

    assert response.status_code == status.HTTP_200_OK
    assert PATH_SENTINEL not in response.text
    assert PATH_SENTINEL in caplog.text
    result = response.json()[0]
    assert result["success"] is False
    assert PATH_SENTINEL not in result["message"]


# --------------------------------------------------------------------------- #
# 5. LLMService.validate_connection -> POST /api/llm-status/test-connection
# --------------------------------------------------------------------------- #


def test_llm_validate_connection_never_leaks_the_endpoint_host(
    user_token_headers, client, monkeypatch, caplog
):
    """create_from_settings can fall back to the deployment's SYSTEM settings,
    so a plain user testing their own config can end up dialing (and, before
    this fix, seeing the failure message for) the deployment's own LLM
    host:port.

    ``resolve_pinned_target`` is stubbed to succeed (it does REAL DNS
    resolution, and ``internal-vllm.lan`` resolves nowhere in a sandbox --
    without this the SSRF guard's own fixed-literal refusal fires first and
    the test would pass without ever reaching the code under test).
    """
    from types import SimpleNamespace

    from app.services.llm_service import LLMConfig
    from app.services.llm_service import LLMProvider
    from app.services.llm_service import LLMService

    def _fake_create_from_settings(*_args, **_kwargs):
        return LLMService(
            LLMConfig(provider=LLMProvider.OLLAMA, model="llama3", base_url=LLM_HOST_SENTINEL)
        )

    monkeypatch.setattr(
        LLMService, "create_from_settings", staticmethod(_fake_create_from_settings)
    )

    fake_target = SimpleNamespace(url=f"{LLM_HOST_SENTINEL}/api/tags", headers={})
    monkeypatch.setattr(
        "app.utils.url_validation.resolve_pinned_target",
        lambda *_a, **_k: (fake_target, ""),
    )

    def _raise_pinned_session(*_args, **_kwargs):
        raise RuntimeError(f"connection refused: {LLM_HOST_SENTINEL}")

    monkeypatch.setattr("app.utils.url_validation.pinned_requests_session", _raise_pinned_session)

    with caplog.at_level(logging.ERROR):
        response = client.post("/api/llm-status/test-connection", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    assert LLM_HOST_SENTINEL not in response.text
    assert LLM_HOST_SENTINEL in caplog.text
    assert response.json()["success"] is False


# --------------------------------------------------------------------------- #
# 6. LLMService._combine_sections -> media_file.summary_data -> GET /summarization/{uuid}/summary
# --------------------------------------------------------------------------- #


def test_combine_sections_failure_never_leaks_exception_text(
    db_session, normal_user, user_token_headers, client, monkeypatch, caplog
):
    from app.services.llm_service import LLMConfig
    from app.services.llm_service import LLMProvider
    from app.services.llm_service import LLMService

    media_file = _make_file(db_session, normal_user)

    llm_service = LLMService(LLMConfig(provider=LLMProvider.OLLAMA, model="llama3"))

    def _raise_chat_completion(*_args, **_kwargs):
        raise RuntimeError(f"generation failed reading prompt from {PATH_SENTINEL}")

    monkeypatch.setattr(llm_service, "chat_completion", _raise_chat_completion)

    with caplog.at_level(logging.ERROR):
        summary = llm_service._combine_sections(
            [{"content": "section one"}, {"content": "section two"}],
            None,
            "Combine: {transcript} {speaker_data}",
            2,
        )

    assert PATH_SENTINEL not in summary["metadata"]["error"]
    assert PATH_SENTINEL in caplog.text

    media_file.summary_data = summary
    db_session.commit()

    response = client.get(
        f"/api/summarization/{media_file.uuid}/summary", headers=user_token_headers
    )
    assert response.status_code == status.HTTP_200_OK
    assert PATH_SENTINEL not in response.text


# --------------------------------------------------------------------------- #
# 7. watch_sources local_client.test_connection -> POST /watch-sources/{uuid}/test
# --------------------------------------------------------------------------- #


def test_watch_source_local_test_connection_never_leaks_a_host_path(
    db_session, admin_user, admin_token_headers, client, monkeypatch, caplog
):
    """#859 already fixed the OUTER handler in api/endpoints/watch_sources.py;
    this proves the INNER half (the client's own try/except, which returns a
    tuple the outer handler never sees because nothing raised) is fixed too."""
    source = _make_watch_source(db_session, admin_user, local_path="../../etc")

    from app.models.watch_source import WatchSource as WatchSourceModel

    def _raise_root(self):
        raise ValueError(f"Resolved path {PATH_SENTINEL} escapes watch root /watch")

    monkeypatch.setattr(WatchSourceModel, "resolved_local_path", property(_raise_root))

    with caplog.at_level(logging.WARNING):
        response = client.post(
            f"/api/watch-sources/{source.uuid}/test", headers=admin_token_headers
        )

    assert response.status_code == status.HTTP_200_OK
    assert PATH_SENTINEL not in response.text
    assert PATH_SENTINEL in caplog.text
    assert response.json()["success"] is False


# --------------------------------------------------------------------------- #
# 8. tasks.recover_task re-dispatch -> POST /tasks/system/recover-task/{id}
# --------------------------------------------------------------------------- #


def test_recover_task_redispatch_failure_never_leaks_exception_text(
    db_session, admin_user, admin_token_headers, client, monkeypatch, caplog
):
    media_file = _make_file(db_session, admin_user, status="error")
    task = TaskModel(
        id=f"tier1-task-{uuid_pkg.uuid4().hex[:8]}",
        user_id=admin_user.id,
        media_file_id=media_file.id,
        task_type="transcription",
        status="failed",
    )
    db_session.add(task)
    db_session.commit()

    from app.services import task_recovery_service

    monkeypatch.setattr(
        task_recovery_service.task_recovery_service,
        "recover_stuck_task",
        lambda *_a, **_k: True,
    )

    def _raise_dispatch(*_args, **_kwargs):
        raise RuntimeError(f"dispatch failed talking to {BROKER_SENTINEL}")

    monkeypatch.setattr("app.tasks.transcription.dispatch_transcription_pipeline", _raise_dispatch)

    with caplog.at_level(logging.ERROR):
        response = client.post(
            f"/api/tasks/system/recover-task/{task.id}", headers=admin_token_headers
        )

    assert response.status_code == status.HTTP_200_OK
    assert BROKER_SENTINEL not in response.text
    assert "hunter2" not in response.text
    assert BROKER_SENTINEL in caplog.text
    body = response.json()
    assert body["dispatch_error"] is not None
    assert BROKER_SENTINEL not in body["dispatch_error"]
