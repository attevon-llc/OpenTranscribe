"""Rename propagation: an unreadable privacy policy fails the task instead of guessing."""

import logging
from unittest.mock import MagicMock

import pytest
from kombu.exceptions import OperationalError as BrokerDown
from sqlalchemy.exc import OperationalError

from app.tasks import rename_propagation_task as rename


def _traceback_logged(caplog, fragment: str) -> bool:
    return any(fragment in r.getMessage() and r.exc_info for r in caplog.records)


def _db_down(monkeypatch):
    from app.db import session_utils

    def _scope():
        raise OperationalError("select", {}, Exception("db down"))

    monkeypatch.setattr(session_utils, "session_scope", _scope)


def test_unreadable_voiceprint_policy_propagates_instead_of_falling_back(monkeypatch, caplog):
    _db_down(monkeypatch)
    with caplog.at_level(logging.ERROR, logger=rename.logger.name), pytest.raises(OperationalError):
        rename._file_if_voiceprints_off("u-1")
    assert _traceback_logged(caplog, "Could not read the voiceprint policy")


def test_title_and_speaker_lookup_outage_fall_back_with_a_traceback(monkeypatch, caplog):
    _db_down(monkeypatch)
    with caplog.at_level(logging.WARNING, logger=rename.logger.name):
        assert rename._current_file_title("u-1") is None
        assert rename._current_speaker_name(3) is None
    assert _traceback_logged(caplog, "Could not re-resolve title")
    assert _traceback_logged(caplog, "Could not re-resolve speaker")


def test_dispatch_swallows_only_broker_outages(monkeypatch, caplog):
    task = MagicMock()
    monkeypatch.setattr(rename, "regenerate_rename_digests", task)
    task.delay.side_effect = BrokerDown("broker down")
    with caplog.at_level(logging.WARNING, logger=rename.logger.name):
        assert rename._dispatch_digest_regeneration(["u-1"], new_name="N", speaker_id=1) == 0
    assert _traceback_logged(caplog, "Could not queue digest regeneration")

    task.delay.side_effect = TypeError("bug")
    with pytest.raises(TypeError):
        rename._dispatch_digest_regeneration(["u-1"], new_name="N", speaker_id=1)


def test_chunk_index_refresh_outage_is_traced_and_the_corpus_version_still_bumps(
    monkeypatch, caplog
):
    from opensearchpy.exceptions import ConnectionError as OsDown

    from app.services import opensearch_service
    from app.services.chat import retrieval_cache

    client = MagicMock()
    client.indices.refresh.side_effect = OsDown("N/A", "refused", Exception("refused"))
    monkeypatch.setattr(opensearch_service, "get_opensearch_client", lambda: client)
    bumped = MagicMock()
    monkeypatch.setattr(retrieval_cache, "bump_corpus_version", bumped)
    with caplog.at_level(logging.WARNING, logger=rename.logger.name):
        rename._finish("idx", "u-1", 2)
    assert _traceback_logged(caplog, "Chunk index refresh failed")
    bumped.assert_called_once()
