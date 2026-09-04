# mypy: disable-error-code="arg-type"
# This suite passes structural stand-ins (fake sessions, fake users, namespace
# requests) to signatures that declare Session/User/Request, and indexes
# HTTPException.detail, which is typed str while every lifecycle gate raises an
# object. Declared once here rather than as a cast at every call site — casts
# bury the assertion, and widening a production signature to suit a test is worse.
import os
import socket
import sys
import tempfile
from pathlib import Path

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Add backend directory to Python path for imports
_backend_dir = Path(__file__).parent.parent
if str(_backend_dir) not in sys.path:
    sys.path.insert(0, str(_backend_dir))

# Read database credentials from .env file without loading all variables
# This avoids polluting the environment with variables that aren't in Settings
_project_root = _backend_dir.parent
_env_file = _project_root / ".env"
_env_values = {}
if _env_file.exists():
    _env_values = dotenv_values(_env_file)

# Set only the service credentials we need from .env (DB always; MinIO for the
# S3-backed tests that activate when the dev stack is reachable). Explicitly
# exported values win over .env so CI / throwaway-DB runs can override.
_db_vars = ["POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"]
_minio_vars = ["MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD", "MEDIA_BUCKET_NAME"]
for _var in _db_vars + _minio_vars:
    if _var in _env_values and _env_values[_var]:
        os.environ.setdefault(_var, _env_values[_var])

# Create temporary directories for testing
_test_temp_dir = tempfile.mkdtemp(prefix="opentranscribe_test_")
_test_data_dir = os.path.join(_test_temp_dir, "data")
_test_upload_dir = os.path.join(_test_data_dir, "uploads")
_test_models_dir = os.path.join(_test_temp_dir, "models")
_test_temp_subdir = os.path.join(_test_temp_dir, "temp")

# Create all directories
for _dir in [_test_data_dir, _test_upload_dir, _test_models_dir, _test_temp_subdir]:
    os.makedirs(_dir, exist_ok=True)


def _service_reachable(host: str, port: int, timeout: float = 0.3) -> bool:
    """Return True if a TCP connection to host:port succeeds within timeout."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# Set testing environment flag and disable external services in tests
os.environ["TESTING"] = "True"
# Declare the suite as a relaxed environment. ENVIRONMENT defaults to "production"
# and fails closed (#284 A0.3), which would otherwise put Secure=True on session
# cookies — and the TestClient talks plain http://testserver, so it would silently
# drop them and every cookie-auth test would see an anonymous session.
os.environ["ENVIRONMENT"] = "testing"
os.environ["SKIP_CELERY"] = "True"
os.environ["SKIP_REDIS"] = "True"
os.environ["SKIP_WEBSOCKET"] = "True"
os.environ["RATE_LIMIT_ENABLED"] = "false"  # Disable rate limiting for tests

# Auto-detect MinIO and OpenSearch from the dev stack so the gated S3/search tests
# run when the services are up and skip when they aren't (e.g. bare CI runners).
# An explicit SKIP_S3 / SKIP_OPENSEARCH in the shell always wins over detection.
#
# Probe the endpoint the tests will ACTUALLY use, resolved from the environment first.
# The probe used to be hard-coded to the dev stack's 5178/5180 while the client honoured
# MINIO_PORT/OPENSEARCH_PORT, so detection and use could disagree. Against an isolated
# `--fresh --port-offset` stack that had two failure modes, both silent:
#   * forget to export MINIO_PORT and it probed 5178 and *used* 5178 — live storage paired
#     with the throwaway Postgres, which is exactly the mixed-stack state --fresh exists to
#     prevent;
#   * export it and the enable/disable decision still came from the *live* stack, so with
#     the fresh MinIO down the S3 suites ran against an unreachable port and failed
#     confusingly instead of skipping.
# Deriving both from one value removes the disagreement and lets `--port-offset` work.
# POSTGRES_PORT below has always behaved this way; this brings MinIO/OpenSearch in line.
_MINIO_PROBE_HOST = os.environ.get("MINIO_HOST", "localhost")
_MINIO_PROBE_PORT = int(os.environ.get("MINIO_PORT", "5178"))
_OPENSEARCH_PROBE_HOST = os.environ.get("OPENSEARCH_HOST", "localhost")
_OPENSEARCH_PROBE_PORT = int(os.environ.get("OPENSEARCH_PORT", "5180"))

os.environ.setdefault(
    "SKIP_S3", "False" if _service_reachable(_MINIO_PROBE_HOST, _MINIO_PROBE_PORT) else "True"
)
os.environ.setdefault(
    "SKIP_OPENSEARCH",
    "False" if _service_reachable(_OPENSEARCH_PROBE_HOST, _OPENSEARCH_PROBE_PORT) else "True",
)
if os.environ["SKIP_S3"] == "False":
    # The dev stack maps the MinIO S3 API to localhost:5178 (console is 5179).
    os.environ["MINIO_HOST"] = _MINIO_PROBE_HOST
    os.environ["MINIO_PORT"] = str(_MINIO_PROBE_PORT)
if os.environ["SKIP_OPENSEARCH"] == "False":
    # The dev stack exposes OpenSearch on localhost:5180 (not the in-cluster 9200).
    os.environ["OPENSEARCH_HOST"] = _OPENSEARCH_PROBE_HOST
    os.environ["OPENSEARCH_PORT"] = str(_OPENSEARCH_PROBE_PORT)
# NOTE on Celery dispatch in tests: endpoints that .delay() a task (e.g.
# PUT /speakers/{uuid}) used to publish into whatever Redis answered on the
# host's default localhost:6379 — an unrelated container on this machine —
# because SKIP_CELERY never covered the dispatch path and the stack's real
# Redis (localhost:5177) requires auth. That worked by accident. The honest
# fix is the autouse _skip_celery_dispatch fixture below, which no-ops
# Task.apply_async (and therefore every .delay) whenever SKIP_CELERY is set.
# The audit logger has its own OpenSearch switch (app/auth/audit.py). Always off
# in unit tests: savepoint rollback cannot undo OpenSearch writes, so leaving it
# on would pollute the live dev audit index with thousands of test login events.
os.environ.setdefault("AUDIT_LOG_TO_OPENSEARCH", "false")

# Set paths to use temporary directories for testing (must be set before importing config)
os.environ["DATA_DIR"] = _test_data_dir
os.environ["MODELS_DIR"] = _test_models_dir
os.environ["TEMP_DIR"] = _test_temp_subdir

# Database connection for local testing - use localhost instead of Docker service name
# The .env file has POSTGRES_HOST=postgres (Docker service) but we need localhost
os.environ["POSTGRES_HOST"] = "localhost"
# Ensure we use the correct port (Docker exposes on 5176)
if "POSTGRES_PORT" not in os.environ or os.environ.get("POSTGRES_PORT") == "5432":
    os.environ["POSTGRES_PORT"] = "5176"

# Import app modules after setting environment variables (they check env during import)
from app.core.config import settings  # noqa: E402
from app.core.security import get_password_hash  # noqa: E402
from app.db.base import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models.user import User  # noqa: E402

# Use PostgreSQL test database (same as main database but with test schema isolation)
# Models use PostgreSQL-specific types (JSONB) that SQLite doesn't support
SQLALCHEMY_TEST_DATABASE_URL = settings.DATABASE_URL
engine = create_engine(SQLALCHEMY_TEST_DATABASE_URL, pool_pre_ping=True)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# Shared fixture modules. Registered here rather than imported so they are
# available to every suite without a per-file import.
#
# NOTE: this line is why `tests/` must NOT gain an `__init__.py`. With one,
# prepend import mode roots `tests/conftest.py` at `backend/` instead of
# `backend/tests/`, so `backend/tests` never reaches sys.path and this dies with
# `ImportError: Error importing plugin "fixtures.mock_llm": No module named
# 'fixtures'` — the same reason `--import-mode=importlib` is unusable here.
#
# `fixtures.dir_collector_memo` contributes no fixtures at all: it is the
# workaround for the pytest 9.1 regression that makes every subdirectory
# conftest's fixtures vanish from a mixed file selection (issue #454). Read its
# docstring before touching it; `unit/test_conftest_fixture_visibility.py` pins
# both the workaround and the fact that pytest still needs it.
pytest_plugins = [
    "fixtures.mock_llm",
    "fixtures.mock_asr",
    "fixtures.dir_collector_memo",
    "fixtures.search_corpus_stack",
]


@pytest.fixture(autouse=True, scope="session")
def _skip_celery_dispatch():
    """No-op every Celery dispatch when SKIP_CELERY is set.

    SKIP_CELERY historically only skipped worker wiring — endpoints calling
    ``task.delay(...)`` still published to the broker. On a developer host that
    silently targeted whatever Redis answered on localhost:6379 (an unrelated
    container), and against the real stack Redis (localhost:5177, auth'd) it
    500s. Patching ``Task.apply_async`` (which ``.delay`` wraps) makes the
    skip-switch actually cover dispatch, with a fake AsyncResult id for code
    that records task ids.
    """
    if os.environ.get("SKIP_CELERY") != "True":
        yield
        return
    from unittest.mock import MagicMock
    from unittest.mock import patch

    fake_result = MagicMock(name="FakeAsyncResult")
    fake_result.id = "test-task-id"
    with patch("celery.app.task.Task.apply_async", return_value=fake_result):
        yield


@pytest.fixture(autouse=True)
def _clear_process_auth_cache():
    """Isolate the process-wide auth-config cache between tests.

    ``app.core.auth_settings`` caches effective auth config for the whole
    process, and under ``TESTING`` a cache generation never ages out (see
    ``_process_cache_ttl``) — deliberately, so a value primed from the test's own
    savepointed session survives the test. That makes clearing it here the thing
    that keeps one test's ``account_lockout_threshold=3`` out of the next test.
    """
    from app.core.auth_settings import clear_process_auth_settings_cache

    clear_process_auth_settings_cache()
    yield
    clear_process_auth_settings_cache()


@pytest.fixture(autouse=True)
def _clear_sidecar_readiness_cache():
    """Isolate the diar-native readiness/liveness TTL caches between tests.

    Same hazard as ``_clear_process_auth_cache`` above, for the same reason: ``_ready_cache``
    and ``_status_cache`` in ``app.transcription.diarizer_native`` are module-level dicts with
    a multi-second TTL, so a verdict cached by one test is visible to every later test in the
    same xdist worker. ``reset_readiness_cache()`` already existed but was called only by the
    handful of tests that flip a server's state mid-test — everything else inherited whatever
    the previous test left behind.

    That is not theoretical. `TestOverlapMidJobFailureDegradesSafely::
    test_release_and_fallback_happen_on_the_main_thread_after_transcription` failed a full
    gate run on `manager.release_calls == 1` while passing in isolation AND in a 13,019-test
    suite run of the same command — the signature of order-dependent state, not of a defect in
    the code under test. The caches are keyed by URL, and the tests in that module stand up
    real servers on `_free_port()` values that the OS is free to hand out again.

    Clearing costs two dict `.clear()` calls per test. Leaving it uncleared costs a failure
    that reproduces only under a particular test ordering, which is the most expensive kind.
    """
    from app.transcription.diarizer_native import reset_readiness_cache

    reset_readiness_cache()
    yield
    reset_readiness_cache()


#: The DDL isolation lock is defined in `tests/db_locks.py` so that this fixture and the
#: tests that open their own DB connection share ONE definition of the key. A second copy
#: of the literal would stop protecting anything the moment either copy changed.
from tests.db_locks import acquire_ddl_lock_exclusive  # noqa: E402
from tests.db_locks import acquire_ddl_lock_shared  # noqa: E402


@pytest.fixture(scope="function")
def db_session(request):
    """Fixture that provides a SQLAlchemy session for tests.

    Uses nested transactions (savepoints) for test isolation - all changes made during
    a test are rolled back at the end, leaving the database in its original state.

    This handles the case where the code under test calls commit() by using
    a savepoint that can be rolled back.

    Tests marked ``@pytest.mark.ddl_exclusive`` run schema DDL (``DROP TABLE`` /
    ``ALTER TABLE ... DROP CONSTRAINT``) that takes Postgres's ``ACCESS EXCLUSIVE``
    lock. That lock is not confined to the table named in the statement: dropping a
    table (or constraint) with a foreign key also drops the FK's enforcement trigger
    on the *referenced* table, which needs an ``ACCESS EXCLUSIVE`` lock there too
    (issue #389 — dropping `scim_token` this way also locks `user`, since
    `scim_token.created_by` references it). Nearly every other test creates or
    touches a `user` row, so under `-n auto` a `ddl_exclusive` test can deadlock
    against an unrelated worker's ordinary DML — a cross-table lock-wait cycle, not
    just a collision with another DDL test. The existing `xdist_group("migration_ddl")`
    marker only keeps DDL tests off each other on one worker; it does nothing about
    every OTHER worker's unmarked tests running at the same wall-clock moment.
    A Postgres advisory lock gives real mutual exclusion across every xdist worker
    (they all talk to the same Postgres instance): every ordinary test takes the
    lock's SHARED form for the life of its transaction; a `ddl_exclusive` test takes
    the EXCLUSIVE form, which blocks until every in-flight ordinary test finishes and
    blocks every new one from starting until the DDL transaction ends (commit or
    rollback — `pg_advisory_xact_lock*` auto-releases either way, so a failed test
    can't leave the suite wedged).

    Apply ``ddl_exclusive`` to the **individual tests that execute DDL, never to a
    module**. Every EXCLUSIVE acquisition is a stop-the-world barrier: it drains all
    other workers and queues every new one behind it, so a read-only schema assertion
    that carries the marker costs a full drain for nothing. Module-scope application is
    what made the `migration_ddl` group 414 s of a 511 s wall clock — 111 tests marked,
    ~12 actually running DDL (issue #431). `tests/unit/test_ddl_marker_discipline.py`
    enforces both directions and fails on either mistake.
    """
    from sqlalchemy import event

    connection = engine.connect()
    transaction = connection.begin()

    if request.node.get_closest_marker("ddl_exclusive") is not None:
        # Also sets lock_timeout, so a genuine cross-connection cycle fails loudly instead
        # of hanging. This lock only covers connections that opt in; ~20 app paths open
        # their own `SessionLocal()` during a TestClient request
        # (app/db/session_utils.py, app/utils/prompt_manager.py, …) and are invisible to it.
        acquire_ddl_lock_exclusive(connection)
    else:
        acquire_ddl_lock_shared(connection)

    # Create a session bound to this connection
    session = TestingSessionLocal(bind=connection)

    # Start a nested transaction (savepoint)
    session.begin_nested()

    # When the session commits, restart the nested transaction
    @event.listens_for(session, "after_transaction_end")
    def restart_savepoint(session, trans):
        if trans.nested and not trans._parent.nested:
            # The savepoint was committed, start a new one
            session.begin_nested()

    try:
        yield session
    finally:
        # Remove the event listener
        event.remove(session, "after_transaction_end", restart_savepoint)
        session.close()
        # Rollback the outer transaction to undo all test changes
        try:
            if transaction.is_active:
                transaction.rollback()
        except Exception:
            pass
        connection.close()


@pytest.fixture(scope="function")
def client(db_session):
    """Fixture that provides a FastAPI TestClient with test DB session"""

    # Override the get_db dependency to use test DB session
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db

    # Create test client
    with TestClient(app) as test_client:
        yield test_client

    # Remove only our override (race-safe for parallel workers)
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(scope="function")
def normal_user(db_session):
    """Fixture that creates a normal user in the test database.

    Uses a unique UUID-based email to avoid conflicts between parallel tests.
    """
    import uuid

    unique_id = str(uuid.uuid4())[:8]
    user = User(
        email=f"testuser_{unique_id}@example.com",
        full_name="Test User",
        hashed_password=get_password_hash("password123"),
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture(scope="function")
def admin_user(db_session):
    """Fixture that creates an admin user in the test database.

    Uses a unique UUID-based email to avoid conflicts between parallel tests.
    """
    import uuid

    unique_id = str(uuid.uuid4())[:8]
    user = User(
        email=f"testadmin_{unique_id}@example.com",
        full_name="Admin User",
        hashed_password=get_password_hash("adminpass"),
        is_active=True,
        # is_superuser mirrors (role == super_admin); an 'admin' is not a superuser.
        # Use the super_admin_user fixture for super_admin-tier endpoints.
        is_superuser=False,
        role="admin",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture(scope="function")
def user_token_headers(client, normal_user):
    """Fixture that returns auth headers for a regular user."""
    # Using form-encoded data for OAuth2 password flow
    response = client.post(
        "/api/auth/token",
        data={"username": normal_user.email, "password": "password123"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200, f"Login failed for {normal_user.email}: {response.json()}"
    tokens = response.json()
    access_token = tokens["access_token"]
    # Return headers along with the user email for tests that need to verify it
    headers = {"Authorization": f"Bearer {access_token}"}
    headers["_test_user_email"] = normal_user.email  # Metadata for tests
    return headers


@pytest.fixture(scope="function")
def admin_token_headers(client, admin_user):
    """Fixture that returns auth headers for an admin user."""
    # Using form-encoded data for OAuth2 password flow
    response = client.post(
        "/api/auth/token",
        data={"username": admin_user.email, "password": "adminpass"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200, f"Login failed for {admin_user.email}: {response.json()}"
    tokens = response.json()
    access_token = tokens["access_token"]
    # Return headers along with the user email for tests that need to verify it
    headers = {"Authorization": f"Bearer {access_token}"}
    headers["_test_user_email"] = admin_user.email  # Metadata for tests
    return headers


@pytest.fixture(scope="function")
def super_admin_user(db_session):
    """Fixture that creates a super_admin user in the test database.

    Uses a unique UUID-based email to avoid conflicts between parallel tests.
    """
    import uuid

    unique_id = str(uuid.uuid4())[:8]
    user = User(
        email=f"testsuperadmin_{unique_id}@example.com",
        full_name="Super Admin User",
        hashed_password=get_password_hash("superadminpass"),
        is_active=True,
        is_superuser=True,
        role="super_admin",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture(scope="function")
def super_admin_token_headers(client, super_admin_user):
    """Fixture that returns auth headers for a super_admin user."""
    response = client.post(
        "/api/auth/token",
        data={"username": super_admin_user.email, "password": "superadminpass"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200, (
        f"Login failed for {super_admin_user.email}: {response.json()}"
    )
    tokens = response.json()
    access_token = tokens["access_token"]
    headers = {"Authorization": f"Bearer {access_token}"}
    headers["_test_user_email"] = super_admin_user.email
    return headers


@pytest.fixture(scope="session")
def sample_wav_bytes() -> bytes:
    """A minimal valid PCM WAV file that passes magic-byte upload validation.

    0.1 s of 16 kHz mono silence (~3.2 KB). Generated with the stdlib so tests
    never depend on fixture files or external downloads.
    """
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 1600)
    return buf.getvalue()


@pytest.fixture
def upload_test_file(client, sample_wav_bytes):
    """Factory that uploads a real WAV via the API and cleans up MinIO afterwards.

    The DB row rolls back with the savepoint, but the MinIO object does not —
    the finalizer deletes the file through the API so the dev bucket stays clean.
    """
    import io

    uploaded: list[tuple[str, dict]] = []

    def _upload(headers: dict, filename: str = "test_audio.wav") -> dict:
        files = {"file": (filename, io.BytesIO(sample_wav_bytes), "audio/wav")}
        response = client.post("/api/files", headers=headers, files=files)
        assert response.status_code == 200, f"File upload failed: {response.json()}"
        data: dict = response.json()
        uploaded.append((str(data.get("uuid") or data.get("id")), headers))
        return data

    yield _upload

    for file_id, headers in uploaded:
        try:
            client.delete(f"/api/files/{file_id}", headers=headers)
        except Exception:
            pass  # DB rollback removes the row; MinIO orphans are best-effort


# --- Fixture aliases for test_media_security.py and other tests ---


@pytest.fixture(scope="function")
def sample_user(normal_user):
    """Alias for normal_user fixture."""
    return normal_user


@pytest.fixture(scope="function")
def auth_headers(user_token_headers):
    """Alias for user_token_headers fixture."""
    return user_token_headers


@pytest.fixture(scope="function")
def admin_auth_headers(admin_token_headers):
    """Alias for admin_token_headers fixture."""
    return admin_token_headers


@pytest.fixture(scope="function")
def other_user(db_session):
    """Create a second normal user for access-control tests."""
    import uuid

    unique_id = str(uuid.uuid4())[:8]
    user = User(
        email=f"otheruser_{unique_id}@example.com",
        full_name="Other User",
        hashed_password=get_password_hash("otherpass123"),
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture(scope="function")
def other_user_auth_headers(client, other_user):
    """Auth headers for the other_user (second normal user)."""
    response = client.post(
        "/api/auth/token",
        data={"username": other_user.email, "password": "otherpass123"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200, f"Login failed for {other_user.email}: {response.json()}"
    tokens = response.json()
    access_token = tokens["access_token"]
    headers = {"Authorization": f"Bearer {access_token}"}
    headers["_test_user_email"] = other_user.email
    return headers
