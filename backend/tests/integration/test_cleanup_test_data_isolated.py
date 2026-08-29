"""Integration proof for ``scripts/cleanup-test-data.py`` against a throwaway Postgres
PLUS a stdlib HTTP stub standing in for the backend API (issue #629).

Mirrors ``test_cleanup_test_users_isolated_db.py``'s isolation posture (throwaway,
loopback-published, tmpfs-backed Postgres container; explicit env, never ``.env``
fallback; allocated port asserted to differ from the live dev stack BEFORE ``docker
run`` executes). The API plane needs its own stand-in because this script's media/
collection/tag/watch-source/speaker-profile/conversation deletions all go over HTTP,
never direct SQL — ``scripts/mock-llm-server.py`` is the in-repo precedent for a
stdlib ``http.server`` stub playing this role.

Deliberately no ``RUN_*`` gate (issue #431 precedent): this needs Docker for Postgres
and nothing else.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import pytest
from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.engine import Engine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available"),
]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "cleanup-test-data.py"
_REGISTRY_SCRIPT = _REPO_ROOT / "scripts" / "testrun-registry.sh"
_COMPOSE_FILE = _REPO_ROOT / "docker-compose.yml"
_DB_USER = "postgres"

_SEED_STATEMENTS = (
    'CREATE TABLE "user" (id serial PRIMARY KEY, email text UNIQUE NOT NULL)',
    (
        "CREATE TABLE media_file (id serial PRIMARY KEY, uuid uuid NOT NULL, "
        'user_id int NOT NULL REFERENCES "user"(id), filename text, '
        "upload_time timestamptz NOT NULL DEFAULT now())"
    ),
    (
        "CREATE TABLE collection (id serial PRIMARY KEY, uuid uuid NOT NULL, "
        'user_id int NOT NULL REFERENCES "user"(id), name text, '
        "created_at timestamptz NOT NULL DEFAULT now())"
    ),
    (
        "CREATE TABLE tag (id serial PRIMARY KEY, uuid uuid NOT NULL, "
        'user_id int REFERENCES "user"(id), name text, '
        "created_at timestamptz NOT NULL DEFAULT now())"
    ),
    (
        "CREATE TABLE watch_source (id serial PRIMARY KEY, uuid uuid NOT NULL, "
        'user_id int NOT NULL REFERENCES "user"(id), name text, '
        "created_at timestamptz NOT NULL DEFAULT now())"
    ),
    (
        "CREATE TABLE speaker_profile (id serial PRIMARY KEY, uuid uuid NOT NULL, "
        'user_id int NOT NULL REFERENCES "user"(id), name text, '
        "created_at timestamptz NOT NULL DEFAULT now())"
    ),
    (
        "CREATE TABLE chat_conversation (id serial PRIMARY KEY, uuid uuid NOT NULL, "
        'user_id int NOT NULL REFERENCES "user"(id), title text, '
        "created_at timestamptz NOT NULL DEFAULT now())"
    ),
    (
        "CREATE TABLE user_llm_settings (id serial PRIMARY KEY, "
        'user_id int NOT NULL REFERENCES "user"(id) ON DELETE CASCADE, '
        "name text NOT NULL, base_url text, is_active boolean NOT NULL DEFAULT false)"
    ),
)


def _load_cleanup_data_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cleanup_test_data", _SCRIPT)
    assert spec is not None and spec.loader is not None, f"cannot load {_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cleanup_data = _load_cleanup_data_module()


def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)  # noqa: S603


def _postgres_image_tag() -> str:
    compose = _COMPOSE_FILE.read_text(encoding="utf-8")
    match = re.search(r"image:\s*(postgres:\S+)", compose)
    assert match, "could not find an `image: postgres:<tag>` line in docker-compose.yml"
    return match.group(1)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_ready(port: int, password: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    consecutive = 0
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            probe = create_engine(
                f"postgresql+psycopg2://{_DB_USER}:{password}@127.0.0.1:{port}/postgres"
            )
            try:
                with probe.connect() as conn:
                    conn.execute(text("SELECT 1"))
            finally:
                probe.dispose()
        except Exception as exc:
            last_exc = exc
            consecutive = 0
        else:
            consecutive += 1
            if consecutive >= 2:
                return
        time.sleep(0.3)
    raise RuntimeError(f"postgres on 127.0.0.1:{port} never became ready: {last_exc}")


@pytest.fixture(scope="module")
def throwaway_pg() -> Iterator[dict[str, Any]]:
    name = f"ot-cleanup-data-test-{uuid.uuid4().hex[:12]}"
    image = _postgres_image_tag()
    password = uuid.uuid4().hex
    port = _free_port()

    live_port = int(os.environ.get("POSTGRES_PORT", "5176"))
    assert port not in {5176, live_port}, (
        f"allocated port {port} collides with the live dev stack's port ({live_port}) — "
        "refusing to start a throwaway container that could be mistaken for it"
    )

    started = _run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{port}:5432",
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "--tmpfs",
            "/var/lib/postgresql/data",
            image,
        ]
    )
    assert started.returncode == 0, f"failed to start throwaway postgres: {started.stderr}"
    try:
        _wait_ready(port, password)
        yield {"container": name, "port": port, "password": password}
    finally:
        _run(["docker", "rm", "-f", name])


class _StubApiHandler(BaseHTTPRequestHandler):
    """Records every DELETE it receives; answers login/logout/GET-detail so
    ``cleanup-test-data.py``'s ``ApiSession`` completes its real flow against it.
    """

    server: _StubApiServer

    def log_message(self, *_args: Any) -> None:  # silence stdout noise
        pass

    def _send_json(self, code: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/api/auth/token":
            self.send_response(200)
            self.send_header("Set-Cookie", "csrf_token=stubcsrf; Path=/")
            self.send_header("Content-Type", "application/json")
            body = json.dumps({"access_token": "stub"}).encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/auth/logout":
            self.server.logout_calls += 1
            self._send_json(200, {})
            return
        self._send_json(404, {"detail": "not found"})

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path.startswith("/api/files/"):
            self._send_json(200, {"active_task_id": None})
            return
        self._send_json(404, {"detail": "not found"})

    def do_DELETE(self) -> None:  # noqa: N802
        split = urlsplit(self.path)
        path = split.path
        query = parse_qs(split.query)
        with self.server.lock:
            if path == "/api/tags":
                self.server.deleted.setdefault("tag", []).extend(query.get("tag_uuids", []))
                self._send_json(200, {"deleted": len(query.get("tag_uuids", []))})
                return
            for resource, prefix in self.server.DELETE_PREFIXES.items():
                if path.startswith(prefix):
                    resource_uuid = path[len(prefix) :].rstrip("/").removesuffix("/force")
                    self.server.deleted.setdefault(resource, []).append(resource_uuid)
                    self._send_json(200, {})
                    return
        self._send_json(404, {"detail": "not found"})


class _StubApiServer(ThreadingHTTPServer):
    DELETE_PREFIXES = {
        "media_file": "/api/files/",
        "collection": "/api/collections/",
        "watch_source": "/api/watch-sources/",
        "speaker_profile": "/api/speaker-profiles/profiles/",
        "chat_conversation": "/api/chat/conversations/",
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.deleted: dict[str, list[str]] = {}
        self.logout_calls = 0
        self.lock = threading.Lock()


@pytest.fixture
def stub_api() -> Iterator[_StubApiServer]:
    server = _StubApiServer(("127.0.0.1", 0), _StubApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _wait_for_path(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.1)


@pytest.fixture
def live_run_marker(tmp_path: Path) -> Iterator[Path]:
    """A REAL flock-held testrun-registry.sh marker (same mechanism proven in
    ``test_testrun_registry.py``), used here to prove the cutoff actually protects a
    row created while a run is still in progress — not just that the pure
    ``resolve_cutoff`` function does the right arithmetic.

    Yields the ``.testruns`` directory once the marker is confirmed live, and holds
    it until the test finishes. A 1.5 s settle after confirming liveness gives any DB
    row inserted immediately afterward a start time strictly AFTER the marker's
    whole-second ``started_at`` — the margin ``resolve_cutoff`` needs to place that
    row on the "still live, don't touch" side of the cutoff.
    """
    testruns_dir = tmp_path / ".testruns"
    testruns_dir.mkdir()
    script = f"""
        source "{_REGISTRY_SCRIPT}"
        testrun_begin
        echo "$TESTRUN_MARKER"
        sleep 300
    """
    proc = subprocess.Popen(  # noqa: S603
        ["bash", "-c", script],
        env={**os.environ, "TESTRUN_REGISTRY_DIR": str(testruns_dir)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        marker_line = proc.stdout.readline().strip() if proc.stdout else ""
        assert marker_line, "marker holder produced no marker path"
        _wait_for_path(Path(marker_line))
        assert cleanup_data.live_marker_start_times(testruns_dir), (
            "the spawned holder's marker must be reported live before the test proceeds"
        )
        time.sleep(1.5)
        yield testruns_dir
    finally:
        if proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=10)


@pytest.fixture
def seeded_db(throwaway_pg: dict[str, Any]) -> Iterator[dict[str, Any]]:
    port = throwaway_pg["port"]
    password = throwaway_pg["password"]
    dbname = f"otcleandata_{uuid.uuid4().hex[:8]}"

    admin_engine = create_engine(
        f"postgresql+psycopg2://{_DB_USER}:{password}@127.0.0.1:{port}/postgres",
        isolation_level="AUTOCOMMIT",
    )
    try:
        with admin_engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    finally:
        admin_engine.dispose()

    engine = create_engine(f"postgresql+psycopg2://{_DB_USER}:{password}@127.0.0.1:{port}/{dbname}")
    try:
        with engine.begin() as conn:
            for stmt in _SEED_STATEMENTS:
                conn.execute(text(stmt))

            owner_email = "tierdata-owner@example.com"
            owner_id = conn.execute(
                text('INSERT INTO "user" (email) VALUES (:e) RETURNING id'),
                {"e": owner_email},
            ).scalar_one()

            old_uuid = str(uuid.uuid4())
            human_uuid = str(uuid.uuid4())
            tag_uuid = str(uuid.uuid4())
            collection_uuid = str(uuid.uuid4())

            # Old Tier-A media (past cutoff) — must be swept.
            conn.execute(
                text(
                    "INSERT INTO media_file (uuid, user_id, filename, upload_time) "
                    "VALUES (:u, :owner, :fn, now() - interval '1 hour')"
                ),
                {"u": old_uuid, "owner": owner_id, "fn": "e2e-owned-a3fbdada.wav"},
            )
            # A human-plausible filename sharing the prefix but not the shape — must
            # never be swept even though it is old.
            conn.execute(
                text(
                    "INSERT INTO media_file (uuid, user_id, filename, upload_time) "
                    "VALUES (:u, :owner, :fn, now() - interval '1 hour')"
                ),
                {"u": human_uuid, "owner": owner_id, "fn": "e2e-owned-notes.wav"},
            )
            # Old Tier-A tag.
            conn.execute(
                text(
                    "INSERT INTO tag (uuid, user_id, name, created_at) "
                    "VALUES (:u, :owner, :n, now() - interval '1 hour')"
                ),
                {"u": tag_uuid, "owner": owner_id, "n": "e2e-tag-a3fbdada"},
            )
            # Old Tier-A collection.
            conn.execute(
                text(
                    "INSERT INTO collection (uuid, user_id, name, created_at) "
                    "VALUES (:u, :owner, :n, now() - interval '1 hour')"
                ),
                {"u": collection_uuid, "owner": owner_id, "n": "e2e-collection-a3fbdada"},
            )

        yield {
            "engine": engine,
            "port": port,
            "dbname": dbname,
            "owner_id": owner_id,
            "owner_email": owner_email,
            "old_media_uuid": old_uuid,
            "human_media_uuid": human_uuid,
            "tag_uuid": tag_uuid,
            "collection_uuid": collection_uuid,
        }
    finally:
        engine.dispose()


def _run_script(
    env: dict[str, str], stub_api: _StubApiServer, testruns_dir: Path, *flags: str
) -> subprocess.CompletedProcess[str]:
    full_env = {
        **env,
        "E2E_BACKEND_URL": f"http://127.0.0.1:{stub_api.server_address[1]}",
        # Isolates the liveness-cutoff computation from the real repo's .testruns/ —
        # see cleanup-test-data.py's identical override, honoured for exactly this.
        "TESTRUN_REGISTRY_DIR": str(testruns_dir),
    }
    return subprocess.run(  # noqa: S603
        [sys.executable, str(_SCRIPT), *flags],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _snapshot(engine: Engine) -> dict[str, list[tuple[Any, ...]]]:
    with engine.connect() as conn:
        return {
            "media_file": sorted(
                tuple(row)
                for row in conn.execute(text("SELECT id, uuid FROM media_file ORDER BY id")).all()
            ),
            "tag": sorted(
                tuple(row)
                for row in conn.execute(text("SELECT id, uuid FROM tag ORDER BY id")).all()
            ),
            "collection": sorted(
                tuple(row)
                for row in conn.execute(text("SELECT id, uuid FROM collection ORDER BY id")).all()
            ),
        }


# ---------------------------------------------------------------------------------------------
# 1. Dry run mutates zero rows in any table, and issues zero API deletes.
# ---------------------------------------------------------------------------------------------


def test_dry_run_mutates_nothing_and_deletes_nothing_over_the_api(
    seeded_db: dict[str, Any], stub_api: _StubApiServer, tmp_path: Path
) -> None:
    before = _snapshot(seeded_db["engine"])
    env = {
        **os.environ,
        "POSTGRES_USER": _DB_USER,
        "POSTGRES_PASSWORD": seeded_db["engine"].url.password,
        "POSTGRES_DB": seeded_db["dbname"],
        "POSTGRES_PORT": str(seeded_db["port"]),
        "POSTGRES_HOST": "127.0.0.1",
    }

    result = _run_script(env, stub_api, tmp_path / ".testruns")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WOULD DELETE" in result.stdout

    after = _snapshot(seeded_db["engine"])
    assert after == before, "a dry run must not change a single row, in ANY table"
    assert stub_api.deleted == {}, "a dry run must never issue a real API delete"


# ---------------------------------------------------------------------------------------------
# 2. --execute-unambiguous removes exactly the OLD Tier-A rows — not the fresh one, not
#    the human-plausible near-miss.
# ---------------------------------------------------------------------------------------------


def test_execute_unambiguous_removes_only_old_tier_a_rows(
    seeded_db: dict[str, Any], stub_api: _StubApiServer, live_run_marker: Path
) -> None:
    # A file created AFTER the (real, flock-held) marker's started_at — the liveness
    # cutoff proof: this row must survive even though it is Tier-A-shaped, because it
    # was created while a run is still in progress.
    fresh_uuid = str(uuid.uuid4())
    with seeded_db["engine"].begin() as conn:
        conn.execute(
            text(
                "INSERT INTO media_file (uuid, user_id, filename, upload_time) "
                "VALUES (:u, :owner, :fn, now())"
            ),
            {"u": fresh_uuid, "owner": seeded_db["owner_id"], "fn": "e2e-owned-bbbbbbbb.wav"},
        )

    env = {
        **os.environ,
        "POSTGRES_USER": _DB_USER,
        "POSTGRES_PASSWORD": seeded_db["engine"].url.password,
        "POSTGRES_DB": seeded_db["dbname"],
        "POSTGRES_PORT": str(seeded_db["port"]),
        "POSTGRES_HOST": "127.0.0.1",
    }
    result = _run_script(env, stub_api, live_run_marker, "--execute-unambiguous")
    assert result.returncode == 0, result.stdout + result.stderr

    assert stub_api.deleted.get("media_file") == [seeded_db["old_media_uuid"]], (
        f"expected only the OLD tier-A file deleted, got {stub_api.deleted.get('media_file')}"
    )
    assert fresh_uuid not in stub_api.deleted.get("media_file", []), (
        "a file created inside a still-live run window must survive (liveness cutoff)"
    )
    assert seeded_db["human_media_uuid"] not in stub_api.deleted.get("media_file", []), (
        "a human-plausible filename sharing only the prefix must survive (shape check)"
    )
    assert stub_api.deleted.get("tag") == [seeded_db["tag_uuid"]]
    assert stub_api.deleted.get("collection") == [seeded_db["collection_uuid"]]
    assert stub_api.logout_calls >= 1, "the tool must log itself out when done (Decision 6)"


# ---------------------------------------------------------------------------------------------
# 3. The dry-run report names exactly the rows --execute-unambiguous removes.
# ---------------------------------------------------------------------------------------------


def test_dry_run_report_names_exactly_the_rows_execute_unambiguous_removes(
    seeded_db: dict[str, Any], stub_api: _StubApiServer, tmp_path: Path
) -> None:
    env = {
        **os.environ,
        "POSTGRES_USER": _DB_USER,
        "POSTGRES_PASSWORD": seeded_db["engine"].url.password,
        "POSTGRES_DB": seeded_db["dbname"],
        "POSTGRES_PORT": str(seeded_db["port"]),
        "POSTGRES_HOST": "127.0.0.1",
    }
    dry = _run_script(env, stub_api, tmp_path / ".testruns")
    assert "e2e-owned-a3fbdada.wav" in dry.stdout
    assert "e2e-owned-notes.wav" not in dry.stdout, (
        "the human-plausible near-miss must not be listed"
    )

    _run_script(env, stub_api, tmp_path / ".testruns", "--execute-unambiguous")
    assert stub_api.deleted.get("media_file") == [seeded_db["old_media_uuid"]]


# ---------------------------------------------------------------------------------------------
# 4. API-plane unavailability degrades rather than blocks — exit non-zero, DB untouched.
# ---------------------------------------------------------------------------------------------


def test_unreachable_api_degrades_to_db_only_and_reports_nonzero(
    seeded_db: dict[str, Any], tmp_path: Path
) -> None:
    unreachable_port = _free_port()
    env = {
        **os.environ,
        "POSTGRES_USER": _DB_USER,
        "POSTGRES_PASSWORD": seeded_db["engine"].url.password,
        "POSTGRES_DB": seeded_db["dbname"],
        "POSTGRES_PORT": str(seeded_db["port"]),
        "POSTGRES_HOST": "127.0.0.1",
        "E2E_BACKEND_URL": f"http://127.0.0.1:{unreachable_port}",
        "TESTRUN_REGISTRY_DIR": str(tmp_path / ".testruns"),
    }
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(_SCRIPT), "--execute-unambiguous"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0, "a partial (API-unavailable) sweep must not read as success"
    assert "API plane unavailable" in result.stdout

    after = _snapshot(seeded_db["engine"])
    assert after["media_file"], "rows must still exist — nothing to delete without the API"
