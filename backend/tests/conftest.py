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
os.environ.setdefault("SKIP_S3", "False" if _service_reachable("localhost", 5178) else "True")
os.environ.setdefault(
    "SKIP_OPENSEARCH", "False" if _service_reachable("localhost", 5180) else "True"
)
if os.environ["SKIP_S3"] == "False":
    # The dev stack maps the MinIO S3 API to localhost:5178 (console is 5179).
    os.environ.setdefault("MINIO_HOST", "localhost")
    os.environ.setdefault("MINIO_PORT", "5178")
if os.environ["SKIP_OPENSEARCH"] == "False":
    # The dev stack exposes OpenSearch on localhost:5180 (not the in-cluster 9200).
    os.environ.setdefault("OPENSEARCH_HOST", "localhost")
    os.environ.setdefault("OPENSEARCH_PORT", "5180")
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
pytest_plugins = ["fixtures.mock_llm"]


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


@pytest.fixture(scope="function")
def db_session():
    """Fixture that provides a SQLAlchemy session for tests.

    Uses nested transactions (savepoints) for test isolation - all changes made during
    a test are rolled back at the end, leaving the database in its original state.

    This handles the case where the code under test calls commit() by using
    a savepoint that can be rolled back.
    """
    from sqlalchemy import event

    connection = engine.connect()
    transaction = connection.begin()

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
def test_wav_bytes() -> bytes:
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
def upload_test_file(client, test_wav_bytes):
    """Factory that uploads a real WAV via the API and cleans up MinIO afterwards.

    The DB row rolls back with the savepoint, but the MinIO object does not —
    the finalizer deletes the file through the API so the dev bucket stays clean.
    """
    import io

    uploaded: list[tuple[str, dict]] = []

    def _upload(headers: dict, filename: str = "test_audio.wav") -> dict:
        files = {"file": (filename, io.BytesIO(test_wav_bytes), "audio/wav")}
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
def test_user(normal_user):
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
