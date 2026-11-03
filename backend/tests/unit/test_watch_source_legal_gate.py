"""A watch source whose OWNER has not accepted a required document is not scanned at all.

The check runs when the scan plan is loaded, before the client is built or anything listed, so
no ``MediaFile`` is ever created that could only fail.
"""

from __future__ import annotations

import contextlib
import uuid as uuid_pkg

import pytest

from app.core.constants import LEGAL_PROCESSING_REQUIRED_KEYS_KEY
from app.legal import registry
from app.legal.acceptance import record_acceptance
from app.models.media import MediaFile
from app.models.system_settings import SystemSettings
from app.models.watch_source import WatchSource
from app.tasks import watch_source_tasks

pytestmark = pytest.mark.xdist_group("legal_processing_gate_settings")

TERMS = "opentranscribe-terms-of-use"


@pytest.fixture
def source(db_session, normal_user):
    row = WatchSource(
        uuid=uuid_pkg.uuid4(),
        user_id=normal_user.id,
        created_by=normal_user.id,
        name=f"watch-{uuid_pkg.uuid4().hex[:8]}",
        source_type="local",
        is_enabled=True,
        local_path=".",
        auto_transcribe=True,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture
def scan_env(monkeypatch, db_session):
    """Route the task's sessions to the test session; record client construction."""

    @contextlib.contextmanager
    def _scope():
        yield db_session

    built: list = []
    monkeypatch.setattr(watch_source_tasks, "session_scope", _scope)
    monkeypatch.setattr(watch_source_tasks, "create_client", lambda src: built.append(src))
    monkeypatch.setattr(watch_source_tasks, "_log_legal_refusal_hourly", lambda *a: None)

    @contextlib.contextmanager
    def _lock(*a, **kw):
        yield True

    monkeypatch.setattr(watch_source_tasks.task_lock_manager, "acquire_lock", _lock)
    return built


def _gate_on(db_session) -> None:
    db_session.add(SystemSettings(key=LEGAL_PROCESSING_REQUIRED_KEYS_KEY, value=TERMS))
    db_session.commit()


def test_a_refused_owner_is_skipped_before_listing(db_session, normal_user, source, scan_env):
    _gate_on(db_session)
    before = db_session.query(MediaFile).filter(MediaFile.user_id == normal_user.id).count()
    result = watch_source_tasks.scan_single.run(source.id)
    assert result == {"skipped": True, "reason": "legal_acceptance_required"}
    assert scan_env == []  # no client was built, so nothing was listed or imported
    assert db_session.query(MediaFile).filter(MediaFile.user_id == normal_user.id).count() == before
    db_session.refresh(source)
    assert source.last_scan_status is None  # never marked running


def test_an_accepted_owner_gets_a_plan(db_session, normal_user, source, scan_env):
    _gate_on(db_session)
    doc = registry.get_document(TERMS)
    assert doc is not None
    record_acceptance(db_session, normal_user.id, doc, None)
    plan = watch_source_tasks._load_scan_plan(source.id)
    assert plan is not None
    assert "legal_refusal" not in plan
    assert len(scan_env) == 1


def test_the_manual_scan_route_is_refused(client, user_token_headers, db_session, source):
    _gate_on(db_session)
    response = client.post(f"/api/watch-sources/{source.uuid}/scan", headers=user_token_headers)
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == {
        "code": "legal_acceptance_required",
        "documents": [TERMS],
    }
