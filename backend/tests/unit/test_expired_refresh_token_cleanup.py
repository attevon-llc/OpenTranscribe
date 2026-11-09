"""Expired refresh-token rows are deleted on a schedule, and only the expired ones.

``TokenService.cleanup_expired_tokens`` existed with a docstring asking to be called
periodically, but no beat entry or caller did, so ``refresh_token`` only ever grew.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from app.auth.token_service import token_service
from app.core.celery import celery_app
from app.models.refresh_token import RefreshToken

pytestmark = pytest.mark.unit

TASK = "auth.cleanup_expired_refresh_tokens"


def _token(user_id: int, *, expires_in: timedelta, revoked: bool = False) -> RefreshToken:
    now = datetime.now(UTC)
    return RefreshToken(
        user_id=user_id,
        token_hash=uuid.uuid4().hex,
        jti=str(uuid.uuid4()),
        expires_at=now + expires_in,
        revoked_at=now if revoked else None,
    )


def test_the_task_is_scheduled_daily_on_the_utility_queue():
    entries = [e for e in celery_app.conf.beat_schedule.values() if e["task"] == TASK]

    assert len(entries) == 1
    assert entries[0]["options"]["queue"] == "utility"
    assert celery_app.conf.task_routes[TASK]["queue"] == "utility"
    assert "app.tasks.token_cleanup" in celery_app.conf.include
    assert TASK in celery_app.tasks


def test_only_expired_rows_are_deleted(db_session, normal_user):
    expired = _token(normal_user.id, expires_in=timedelta(days=-2))
    expired_revoked = _token(normal_user.id, expires_in=timedelta(minutes=-1), revoked=True)
    live = _token(normal_user.id, expires_in=timedelta(days=5))
    live_revoked = _token(normal_user.id, expires_in=timedelta(days=5), revoked=True)
    db_session.add_all([expired, expired_revoked, live, live_revoked])
    db_session.commit()
    kept_ids = {live.id, live_revoked.id}
    gone_ids = {expired.id, expired_revoked.id}

    deleted = token_service.cleanup_expired_tokens(db_session)

    remaining = {
        row.id
        for row in db_session.query(RefreshToken).filter(RefreshToken.id.in_(kept_ids | gone_ids))
    }
    assert remaining == kept_ids
    assert deleted >= len(gone_ids)


def test_a_database_failure_is_not_reported_as_zero_deleted():
    db = MagicMock(spec=Session)
    db.query.side_effect = RuntimeError("database down")

    with pytest.raises(RuntimeError, match="database down"):
        token_service.cleanup_expired_tokens(db)


def test_the_task_skips_when_the_lock_is_held(monkeypatch):
    from app.tasks import token_cleanup as task_mod

    @contextlib.contextmanager
    def _never_acquire(_key, timeout=0, blocking_timeout=0):
        yield False

    monkeypatch.setattr(task_mod.task_lock_manager, "acquire_lock", _never_acquire, raising=False)
    monkeypatch.setattr(
        task_mod.token_service,
        "cleanup_expired_tokens",
        lambda _db: pytest.fail("must not delete while another pass holds the lock"),
    )

    assert task_mod.cleanup_expired_refresh_tokens_task() == {
        "status": "skipped",
        "reason": "token cleanup already running",
    }
