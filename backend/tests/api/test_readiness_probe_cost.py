"""``/health/ready`` must stay cheap and bounded (issue #1000).

Load balancers and orchestrators probe readiness several times a minute, so two
properties matter more than for an ordinary endpoint:

- **No per-probe re-parse of the migration scripts.** The Alembic head is a
  property of the build, not of the request; loading every revision file on each
  probe cost ~45-50 ms of GIL-holding CPU for an answer that never changes.
- **Every dependency check is time-bounded.** With library defaults (no Redis
  socket timeout, OpenSearch's 10 s request timeout, minio-py's 300 s connect/read
  timeout plus 5 retries) one slow dependency pushes the probe past the prober's
  own timeout and the instance is pulled from service for the wrong reason.

The timeout tests capture the client each check actually talks to and assert on
its configuration, so they need no slow or black-holed service to run.
"""

from __future__ import annotations

import pytest
from alembic.script import ScriptDirectory

from app.db.migrations import get_alembic_config

#: The issue's contract, stated independently of the constant that implements it so
#: raising the constant past it is a visible test change, not a silent drift.
_MAX_PROBE_TIMEOUT_S = 2.0


@pytest.fixture
def healthy_db_redis(monkeypatch):
    """Stub the two critical checks so a test can focus on one other dependency."""

    class _FakeSession:
        def execute(self, *a, **k):
            return None

        def close(self):
            pass

    class _FakeRedis:
        def ping(self):
            return True

    monkeypatch.setattr("app.db.base.SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr("app.core.redis.get_probe_redis", lambda: _FakeRedis())


def test_repeated_probes_do_not_reparse_migration_scripts(client, monkeypatch):
    calls: list[int] = []
    original = ScriptDirectory.from_config

    def _counting_from_config(cls, *args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(ScriptDirectory, "from_config", classmethod(_counting_from_config))

    heads = set()
    for _ in range(3):
        body = client.get("/health/ready").json()
        heads.add(body["checks"].get("schema_head"))

    # At most one parse (zero when an earlier test in this process warmed the cache).
    assert len(calls) <= 1, f"migration scripts parsed {len(calls)} times for 3 probes"
    # And the cached value is the real head, not a placeholder.
    real_head = original(get_alembic_config()).get_current_head()
    assert heads == {real_head}


def test_redis_check_uses_short_socket_timeouts(client, monkeypatch):
    import redis

    seen: list[dict] = []

    def _recording_ping(self, **kwargs):
        seen.append(dict(self.connection_pool.connection_kwargs))
        return True

    monkeypatch.setattr(redis.Redis, "ping", _recording_ping)

    client.get("/health/ready")

    assert seen, "the readiness check never pinged Redis"
    kwargs = seen[0]
    assert kwargs.get("socket_timeout") is not None
    assert kwargs["socket_timeout"] <= _MAX_PROBE_TIMEOUT_S
    assert kwargs.get("socket_connect_timeout") is not None
    assert kwargs["socket_connect_timeout"] <= _MAX_PROBE_TIMEOUT_S


def test_opensearch_ping_passes_a_short_request_timeout(client, monkeypatch, healthy_db_redis):
    seen: list[dict] = []

    class _FakeOS:
        def ping(self, **kwargs):
            seen.append(kwargs)
            return True

    monkeypatch.setattr("app.services.opensearch_service.get_opensearch_client", lambda: _FakeOS())

    body = client.get("/health/ready").json()

    assert body["checks"]["opensearch"] == "ok"
    assert seen and seen[0].get("request_timeout") is not None
    assert seen[0]["request_timeout"] <= _MAX_PROBE_TIMEOUT_S


def test_object_storage_check_uses_short_timeouts_and_no_retries(
    client, monkeypatch, healthy_db_redis
):
    from minio import Minio

    seen: list = []

    def _recording_bucket_exists(self, bucket_name):
        seen.append(self._http)
        return True

    monkeypatch.setattr(Minio, "bucket_exists", _recording_bucket_exists)

    body = client.get("/health/ready").json()

    assert body["checks"]["minio"] == "ok"
    assert seen, "the readiness check never probed object storage"
    pool_kw = seen[0].connection_pool_kw
    timeout = pool_kw["timeout"]
    assert timeout.connect_timeout <= _MAX_PROBE_TIMEOUT_S
    assert timeout.read_timeout <= _MAX_PROBE_TIMEOUT_S
    # urllib3 retries multiply the bound: 5 retries x 2 s is a 12 s probe.
    retries = pool_kw["retries"]
    assert retries is False or retries.total in (0, False)
