"""``/health/ready`` keeps a non-migrating replica out of rotation until the schema is at head.

With the one-shot migrate step, API replicas run with ``RUN_MIGRATIONS_ON_STARTUP=false``
and never migrate. A replica that starts before the migrate step finished (or against a
database it never reached) must answer 503, or the load balancer sends it traffic against an
old schema. ``test_celery_reliability`` only checked the handler's source text; this calls it.

Postgres is real (the suite database, at head); Redis is replaced by a probe that answers, so
the only critical check that can fail is the one under test.
"""

from __future__ import annotations

import json

import pytest
from alembic.migration import MigrationContext

from app.core.config import settings
from app.db import migrations
from app.db.base import engine


class _Pong:
    def ping(self) -> bool:
        return True


@pytest.fixture
def current_revision() -> str:
    with engine.connect() as conn:
        rev = MigrationContext.configure(conn).get_current_revision()
    assert rev, "the suite database must be migrated for this test"
    return rev


@pytest.fixture
def ready(monkeypatch):
    import app.core.redis as redis_module
    from app.main import readiness_check

    monkeypatch.setattr(redis_module, "get_probe_redis", lambda: _Pong())

    def call(*, migrate_on_startup: bool, head: str) -> tuple[int, dict]:
        monkeypatch.setattr(settings, "RUN_MIGRATIONS_ON_STARTUP", migrate_on_startup)
        monkeypatch.setattr(migrations, "get_alembic_head", lambda: head)
        result = readiness_check()
        if isinstance(result, dict):
            return 200, result
        return result.status_code, json.loads(bytes(result.body))

    return call


def test_a_replica_that_does_not_migrate_is_not_ready_on_a_stale_schema(ready, current_revision):
    status, body = ready(migrate_on_startup=False, head="v999_not_applied_yet")
    assert status == 503
    assert body["status"] == "not_ready"
    assert body["checks"]["schema"].startswith("stale:")
    assert body["checks"]["schema_revision"] == current_revision


def test_the_same_replica_is_ready_once_the_schema_is_at_head(ready, current_revision):
    status, body = ready(migrate_on_startup=False, head=current_revision)
    assert status == 200, body
    assert body["checks"]["schema"] == "ok"


def test_a_self_migrating_process_reports_but_does_not_gate_on_the_schema(ready, current_revision):
    status, body = ready(migrate_on_startup=True, head="v999_not_applied_yet")
    assert status == 200, body
    assert body["checks"]["schema"].startswith("stale:")
