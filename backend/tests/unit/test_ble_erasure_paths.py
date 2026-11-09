"""Outage injection for the erasure / purge handlers (broad-except lane B7).

These handlers record the failure for the caller (a residual list, a ``fail`` callback,
an ``unverified`` count). The standard also requires the operator to get the traceback.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.services import account_purge
from app.services import erasure_index_reconcile
from app.services import gdpr_erasure_steps
from app.services import gdpr_voiceprint_erasure
from app.services import opensearch_erasure


def _with_traceback(caplog, logger_name):
    return [
        r
        for r in caplog.records
        # exc_text: MessageFreeTracebackFilter keeps the frames but drops exc_info and the message
        if r.name == logger_name and r.levelno >= logging.WARNING and (r.exc_info or r.exc_text)
    ]


def test_opensearch_transcript_delete_failure_is_recorded_and_traceback_logged(monkeypatch, caplog):
    monkeypatch.setattr(opensearch_erasure, "_opensearch_client", lambda: None)
    fail = MagicMock()
    with caplog.at_level(logging.ERROR, logger=opensearch_erasure.logger.name):
        opensearch_erasure._erase_transcript_doc("file-uuid", fail)
    fail.assert_called_once()
    assert fail.call_args.args[0] == "transcript"
    assert _with_traceback(caplog, opensearch_erasure.logger.name)


def test_opensearch_speaker_delete_failure_is_recorded_and_traceback_logged(monkeypatch, caplog):
    import app.services.opensearch_service as os_service

    boom = MagicMock(side_effect=RuntimeError("cluster down"))
    monkeypatch.setattr(os_service, "remove_speaker_embedding", boom)
    monkeypatch.setattr(opensearch_erasure, "_count_surviving", lambda idx, q: 0)
    fail = MagicMock()
    with caplog.at_level(logging.ERROR, logger=opensearch_erasure.logger.name):
        opensearch_erasure._erase_speaker_docs(["s1"], fail)
    assert any(c.args[0] == "speakers" for c in fail.call_args_list)
    assert _with_traceback(caplog, opensearch_erasure.logger.name)


def test_gdpr_avatar_delete_failure_is_recorded_and_traceback_logged(monkeypatch, caplog):
    import app.services.minio_service as minio_service

    monkeypatch.setattr(minio_service, "delete_file", MagicMock(side_effect=OSError("gone")))
    errors: list[dict] = []
    with caplog.at_level(logging.ERROR, logger=gdpr_erasure_steps.logger.name):
        gdpr_erasure_steps._erase_avatar_objects(["a/b.png"], errors)
    assert errors and errors[0]["stage"] == "avatar"
    assert _with_traceback(caplog, gdpr_erasure_steps.logger.name)


def test_profile_embedding_removal_failure_is_recorded_and_traceback_logged(monkeypatch, caplog):
    import app.services.opensearch_service as os_service

    monkeypatch.setattr(
        os_service, "remove_profile_embedding", MagicMock(side_effect=RuntimeError("down"))
    )
    errors: list[dict] = []
    with caplog.at_level(logging.ERROR, logger=gdpr_voiceprint_erasure.logger.name):
        gdpr_voiceprint_erasure._erase_profile_embedding("p-uuid", errors)
    assert errors and errors[0]["stage"] == "profile_embedding"
    assert _with_traceback(caplog, gdpr_voiceprint_erasure.logger.name)


def test_index_check_outage_is_unverified_and_traceback_logged(monkeypatch, caplog):
    def _boom():
        raise RuntimeError("cluster down")

    monkeypatch.setattr(erasure_index_reconcile, "_client", _boom)
    subject = erasure_index_reconcile.IndexSubject(
        entry_id=1, subject_type="user", user_id=1, organization_id=None, completed_at=None
    )
    with caplog.at_level(logging.ERROR, logger=erasure_index_reconcile.logger.name):
        result = erasure_index_reconcile.check_subjects([subject])
    assert result.unverified == 1
    assert _with_traceback(caplog, erasure_index_reconcile.logger.name)


def test_account_purge_avatar_failure_is_residual_and_traceback_logged(monkeypatch, caplog):
    import app.services.minio_service as minio_service

    monkeypatch.setattr(minio_service, "delete_file", MagicMock(side_effect=OSError("gone")))
    plan = account_purge.AccountPurgePlan(
        files=[],
        speaker_uuids=[],
        profile_uuids=[],
        cluster_uuids=[],
        user_id=None,
        avatar_paths=["avatars/1.png"],
    )
    with caplog.at_level(logging.ERROR, logger=account_purge.logger.name):
        residual = account_purge.purge_account_external_copies(plan)
    assert [r["stage"] for r in residual] == ["avatar"]
    assert _with_traceback(caplog, account_purge.logger.name)


def test_account_purge_cache_outage_is_logged_and_a_bug_is_not_swallowed(monkeypatch, caplog):
    import app.services.redis_cache_service as cache_module

    plan = account_purge.AccountPurgePlan(
        files=[],
        speaker_uuids=[],
        profile_uuids=[],
        cluster_uuids=[],
        user_id=7,
        avatar_paths=[],
    )
    monkeypatch.setattr(
        cache_module.redis_cache,
        "invalidate_all_for_user",
        MagicMock(side_effect=RedisConnectionError("redis down")),
    )
    with caplog.at_level(logging.WARNING, logger=account_purge.logger.name):
        account_purge.purge_account_external_copies(plan)
    assert _with_traceback(caplog, account_purge.logger.name)

    monkeypatch.setattr(
        cache_module.redis_cache,
        "invalidate_all_for_user",
        MagicMock(side_effect=AttributeError("bug")),
    )
    with pytest.raises(AttributeError):
        account_purge.purge_account_external_copies(plan)
