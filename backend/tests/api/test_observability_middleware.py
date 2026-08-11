"""API tests for the observability middleware, metrics, and readiness probe.

The controlled cases (unknown route, unhandled-exception re-raise, access-log
capture) use a small standalone app wired with the real middleware so they
don't depend on any particular production endpoint's behavior. The live cases
(``/metrics``, ``/health``, a DB-touching route) use the shared ``client``
fixture against the test database.

These pass under conftest's ``SKIP_REDIS=True`` by exercising the graceful
degradation paths and monkeypatching the readiness probe rather than relying on
live broker/search connectivity.
"""

import logging

import pytest
from fastapi import FastAPI
from prometheus_client.parser import text_string_to_metric_families
from starlette.testclient import TestClient

from app.middleware.audit import AuditMiddleware
from app.middleware.observability import ObservabilityMiddleware


@pytest.fixture
def obs_app():
    """A minimal app with Audit + Observability middleware (Audit inner)."""
    app = FastAPI()

    @app.get("/ok")
    def ok():
        return {"ok": True}

    @app.get("/boom")
    def boom():
        raise RuntimeError("intentional unhandled error")

    # Audit must be added FIRST so it ends up INNER (sets request_id/client_ip),
    # Observability added LAST so it runs OUTERMOST — same ordering as main.py.
    app.add_middleware(AuditMiddleware)
    app.add_middleware(ObservabilityMiddleware)
    return app


# --------------------------------------------------------------------------- #
# /metrics
# --------------------------------------------------------------------------- #


def test_metrics_endpoint_ok_and_parseable(client):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    families = list(text_string_to_metric_families(resp.text))
    names = {f.name for f in families}
    assert "http_request_duration_seconds" in names
    assert "db_queries_per_request" in names


def test_metrics_self_scrape_not_recorded(client):
    """The /metrics path is skipped entirely (no self-scrape series)."""
    client.get("/metrics")
    body = client.get("/metrics").text
    # No request series should carry route="/metrics".
    assert 'route="/metrics"' not in body


# --------------------------------------------------------------------------- #
# Route templating + label hygiene (live endpoints)
# --------------------------------------------------------------------------- #


def test_templated_route_labels_no_raw_ids(client, user_token_headers):
    """A templated API route is recorded with the TEMPLATE, not a concrete id."""
    headers = {"Authorization": user_token_headers["Authorization"]}
    # Hit a templated, DB-touching route with a concrete (nonexistent) UUID.
    client.get("/api/files/00000000-0000-0000-0000-000000000000", headers=headers)
    body = client.get("/metrics").text
    # The concrete id must never appear as a label value.
    assert "00000000-0000-0000-0000-000000000000" not in body
    # The template (with the /api prefix) is the label. The path param is named
    # file_uuid in this route, so the template is /api/files/{file_uuid}.
    assert 'route="/api/files/{file_uuid}"' in body


def test_health_route_recorded(client):
    client.get("/health")
    body = client.get("/metrics").text
    assert 'route="/health"' in body


# --------------------------------------------------------------------------- #
# Unknown route + exception re-raise (standalone app)
# --------------------------------------------------------------------------- #


def test_unknown_route_labelled_unknown(obs_app):
    with TestClient(obs_app) as tc:
        resp = tc.get("/this-route-does-not-exist")
        assert resp.status_code == 404
        metrics_text = _scrape()
    assert 'route="unknown"' in metrics_text


def test_unhandled_exception_records_500_and_reraises(obs_app):
    # raise_server_exceptions=False lets us observe the 500 instead of bubbling.
    with TestClient(obs_app, raise_server_exceptions=False) as tc:
        resp = tc.get("/boom")
        assert resp.status_code == 500
        metrics_text = _scrape()
    # The failing request was recorded with status 500.
    assert 'http_requests_total{method="GET",route="/boom",status="500"}' in metrics_text


def test_access_log_contains_structured_fields(obs_app, caplog):
    with caplog.at_level(logging.INFO, logger="access"):
        with TestClient(obs_app) as tc:
            tc.get("/ok")
    records = [r for r in caplog.records if r.name == "access"]
    assert records, "no access-log record emitted"
    rec = records[-1]
    # Both the human message and the structured extras must be present.
    assert "GET" in rec.getMessage()
    for field in ("route", "method", "status", "duration_ms", "db_query_count", "request_id"):
        assert hasattr(rec, field), f"access record missing {field}"
    assert rec.route == "/ok"
    assert rec.status == 200


# --------------------------------------------------------------------------- #
# db_queries_per_request
# --------------------------------------------------------------------------- #


def test_db_queries_per_request_observed(client, user_token_headers):
    """A DB-touching endpoint observes at least one statement; counts reset."""
    headers = {"Authorization": user_token_headers["Authorization"]}
    # /api/users/me looks up the user — guaranteed at least one query.
    resp = client.get("/api/users/me", headers=headers)
    assert resp.status_code == 200
    body = client.get("/metrics").text
    # The histogram for this route exists with a non-zero count.
    assert "db_queries_per_request_count" in body
    # Sanity: a second request still succeeds (counter reset between requests).
    resp2 = client.get("/api/users/me", headers=headers)
    assert resp2.status_code == 200


# --------------------------------------------------------------------------- #
# /health/ready
# --------------------------------------------------------------------------- #


def test_readiness_happy_path(client, monkeypatch):
    """All probes green → 200 with status=ready and every check reported."""
    import app.main as main_module

    # Make every dependency probe deterministically succeed regardless of which
    # services happen to be reachable from the test process.
    class _FakeRedis:
        def ping(self):
            return True

    class _FakeOS:
        def ping(self):
            return True

    class _FakeMinio:
        def bucket_exists(self, name):
            return True

    class _FakeSession:
        def execute(self, *a, **k):
            return None

        def close(self):
            pass

    monkeypatch.setattr(main_module, "SessionLocal", lambda: _FakeSession(), raising=False)
    monkeypatch.setattr("app.db.base.SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr("app.core.redis.get_redis", lambda: _FakeRedis())
    monkeypatch.setattr("app.services.opensearch_service.get_opensearch_client", lambda: _FakeOS())
    monkeypatch.setattr("app.services.minio_service.minio_client", _FakeMinio())

    resp = client.get("/health/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    # Schema state is reported UNCONDITIONALLY now, not only when a migrate job
    # owns migrations (RUN_MIGRATIONS_ON_STARTUP=false). Reporting it always is
    # what lets the release harness assert over HTTP that an upgrade actually
    # migrated, instead of shelling `docker exec opentranscribe-postgres psql`
    # (which hardcodes a container name and breaks on --fresh stacks). It stays
    # 503-CRITICAL only in the gated-off case, so self-host readiness semantics
    # are unchanged — see the critical-failure tests below.
    assert {"postgres", "redis", "opensearch", "minio"} <= set(body["checks"])
    assert body["checks"]["postgres"] == "ok"
    assert body["checks"]["redis"] == "ok"
    assert "schema" in body["checks"]
    # Present together or not at all: they are only set when the revision was
    # actually read, so a partial pair would mean the reporting path is broken.
    assert ("schema_revision" in body["checks"]) == ("schema_head" in body["checks"])


def test_readiness_critical_db_failure_returns_503(client, monkeypatch):
    """A failed CRITICAL dependency (Postgres) → 503 not_ready."""

    def _broken_session():
        raise RuntimeError("db down")

    monkeypatch.setattr("app.db.base.SessionLocal", _broken_session)

    resp = client.get("/health/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["postgres"].startswith("error")


def test_readiness_degraded_opensearch_still_ready(client, monkeypatch):
    """OpenSearch/MinIO down but DB+Redis up → still 200 (degraded-but-ready)."""

    class _FakeRedis:
        def ping(self):
            return True

    class _FakeSession:
        def execute(self, *a, **k):
            return None

        def close(self):
            pass

    monkeypatch.setattr("app.db.base.SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr("app.core.redis.get_redis", lambda: _FakeRedis())
    monkeypatch.setattr("app.services.opensearch_service.get_opensearch_client", lambda: None)

    def _broken_minio_exists(name):
        raise RuntimeError("minio down")

    monkeypatch.setattr(
        "app.services.minio_service.minio_client.bucket_exists", _broken_minio_exists
    )

    resp = client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


# --------------------------------------------------------------------------- #
# Request-ID propagation into Celery tasks
# --------------------------------------------------------------------------- #


def test_celery_publish_injects_request_id_header():
    """before_task_publish stamps the current request_id onto task headers."""
    from app.core.celery import inject_request_id_header
    from app.middleware.audit import set_request_id

    set_request_id("req-publish-1")
    try:
        headers: dict = {}
        inject_request_id_header(headers=headers)
        assert headers["request_id"] == "req-publish-1"
    finally:
        set_request_id("")


def test_celery_publish_no_request_id_is_noop():
    """Beat-scheduled tasks (no request context) inject nothing."""
    from app.core.celery import inject_request_id_header
    from app.middleware.audit import set_request_id

    set_request_id("")
    headers: dict = {}
    inject_request_id_header(headers=headers)
    assert "request_id" not in headers


def test_celery_prerun_adopts_request_id_from_header():
    """task_prerun adopts the inbound header into the audit ContextVar."""
    from app.core.celery import adopt_request_id
    from app.middleware.audit import get_request_id
    from app.middleware.audit import set_request_id

    class _Req:
        request_id = "req-prerun-9"

    class _Task:
        request = _Req()

    set_request_id("")
    try:
        adopt_request_id(task=_Task())
        assert get_request_id() == "req-prerun-9"
    finally:
        set_request_id("")


def _scrape() -> str:
    """Render the default registry to text (helper for standalone-app cases)."""
    from prometheus_client import REGISTRY
    from prometheus_client import generate_latest

    return generate_latest(REGISTRY).decode()
