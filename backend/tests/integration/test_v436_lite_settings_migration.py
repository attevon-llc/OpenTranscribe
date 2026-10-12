"""v436 against a throwaway Postgres: up, down, up again, lite and full (issue #1228).

The revision rewrites a lite user's stored "this server" speaker detection to Automatic when
their active cloud model returns speakers, and touches nothing else. This runs the REAL
Alembic chain (``python -m alembic`` from ``backend/``, the production upgrade path) against
a throwaway, loopback-published Postgres container, never a live stack's database:

1. ``upgrade v434`` on an empty database, then seed users with every relevant combination
   plus a transcript, speaker and voiceprint-bearing rows that must survive;
2. ``upgrade head`` with ``DEPLOYMENT_MODE=lite``: exactly the expected users move;
3. ``downgrade`` back to v434: a no-op (nothing records which rows moved);
4. ``upgrade head`` again: idempotent, nothing else changes;
5. the startup runner's detection arm stamps the rewritten database at or after v436 and an
   unmigrated lite database earlier;
6. the same sequence on a second database with ``DEPLOYMENT_MODE=full`` changes nothing.

Safety posture mirrors ``test_cleanup_test_users_isolated_db.py``: loopback-only published
port asserted different from every live stack port BEFORE ``docker run``, ``--tmpfs`` PGDATA,
a uuid4-suffixed container name with the test-artifact label, removal in ``finally``, and
every connection setting passed explicitly to the subprocess (never left to ``.env``).
"""

from __future__ import annotations

import importlib
import os
import re
import shutil
import socket
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.docker_ops import TEST_ARTIFACT_LABEL
from tests.integration.throwaway_pg import _READY_TIMEOUT
from tests.integration.throwaway_pg import run as _run

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available"),
]

_BACKEND = Path(__file__).resolve().parents[2]
_COMPOSE_FILE = _BACKEND.parent / "docker-compose.yml"
_DB_USER = "postgres"
V434 = "v434_reset_unusable_pyannote_source"
V436 = "v436_lite_local_speaker_detection_to_automatic"
_SOURCE = "transcription_diarization_source"


def _postgres_image_tag() -> str:
    match = re.search(r"image:\s*(postgres:\S+)", _COMPOSE_FILE.read_text(encoding="utf-8"))
    assert match, "could not find an `image: postgres:<tag>` line in docker-compose.yml"
    return match.group(1)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _url(port: int, password: str, db: str) -> str:
    return f"postgresql+psycopg2://{_DB_USER}:{password}@127.0.0.1:{port}/{db}"


def _wait_ready(port: int, password: str) -> None:
    """Two consecutive successful queries, polled (never a bare sleep)."""
    deadline = time.monotonic() + _READY_TIMEOUT
    consecutive = 0
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        probe = create_engine(_url(port, password, "postgres"))
        try:
            with probe.connect() as conn:
                conn.execute(text("SELECT 1"))
            consecutive += 1
            if consecutive >= 2:
                return
        except Exception as exc:  # the server is still starting: polled again
            last_exc = exc
            consecutive = 0
        finally:
            probe.dispose()
        time.sleep(0.3)
    raise RuntimeError(f"postgres on 127.0.0.1:{port} never became ready: {last_exc}")


@pytest.fixture(scope="module")
def throwaway_pg() -> Iterator[dict[str, Any]]:
    name = f"ot-migration-tcp-{uuid.uuid4().hex[:12]}"
    password = uuid.uuid4().hex
    port = _free_port()
    live = {5176, 5276, 5376, int(os.environ.get("POSTGRES_PORT", "5176"))}
    assert port not in live, f"allocated port {port} collides with a live stack's port"
    try:
        started = _run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--label",
                TEST_ARTIFACT_LABEL,
                "-p",
                f"127.0.0.1:{port}:5432",
                "-e",
                f"POSTGRES_PASSWORD={password}",
                "--tmpfs",
                "/var/lib/postgresql/data",
                _postgres_image_tag(),
            ]
        )
        assert started.returncode == 0, f"failed to start throwaway postgres: {started.stderr}"
        _wait_ready(port, password)
        yield {"port": port, "password": password}
    finally:
        _run(["docker", "rm", "-f", name])


def _alembic(pg: dict[str, Any], db: str, mode: str, command_name: str, revision: str) -> None:
    """Run the real Alembic command against ``db``; ``env.py`` builds the URL from these.

    Every connection setting is set explicitly (an explicit variable beats ``.env``), and
    ``DEPLOYMENT_MODE`` is what the revision reads at migration time.
    """
    from alembic.config import Config

    # By module path: backend/alembic shadows the package name for mypy's import resolution.
    command = importlib.import_module("alembic.command")
    config = Config(str(_BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND / "alembic"))
    with pytest.MonkeyPatch.context() as env:
        env.setenv("POSTGRES_HOST", "127.0.0.1")
        env.setenv("POSTGRES_PORT", str(pg["port"]))
        env.setenv("POSTGRES_USER", _DB_USER)
        env.setenv("POSTGRES_PASSWORD", pg["password"])
        env.setenv("POSTGRES_DB", db)
        env.setenv("DEPLOYMENT_MODE", mode)
        getattr(command, command_name)(config, revision)


def _new_database(pg: dict[str, Any], suffix: str) -> tuple[str, Engine]:
    name = f"ot_v436_{suffix}_test"
    admin = create_engine(
        _url(pg["port"], pg["password"], "postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        admin.dispose()
    return name, create_engine(_url(pg["port"], pg["password"], name))


# --- seeding ------------------------------------------------------------------------------


def _user(conn, label: str) -> int:
    return int(
        conn.execute(
            text(
                'INSERT INTO "user" (email, hashed_password, is_active, is_superuser, role, '
                "auth_type) VALUES (:e, 'x', true, false, 'user', 'local') RETURNING id"
            ),
            {"e": f"v436-{label}-{uuid.uuid4().hex[:8]}@example.invalid"},
        ).scalar()
    )


def _setting(conn, user_id: int, key: str, value: str) -> None:
    conn.execute(
        text("INSERT INTO user_setting (user_id, setting_key, setting_value) VALUES (:u, :k, :v)"),
        {"u": user_id, "k": key, "v": value},
    )


def _active(conn, user_id: int, provider: str, model: str) -> None:
    config_id = int(
        conn.execute(
            text(
                "INSERT INTO user_asr_settings (user_id, name, provider, model_name, is_active) "
                "VALUES (:u, :n, :p, :m, true) RETURNING id"
            ),
            {"u": user_id, "n": f"{provider}-{model}", "p": provider, "m": model},
        ).scalar()
    )
    _setting(conn, user_id, "active_asr_config_id", str(config_id))


#: label -> (stored source, active (provider, model) or None, source after a LITE upgrade)
CASES: dict[str, tuple[str, tuple[str, str] | None, str]] = {
    "local-deepgram": ("local", ("deepgram", "nova-3"), "provider"),
    "local-azure": ("local", ("azure", "standard"), "provider"),
    "local-openai-diarize": ("local", ("openai", "gpt-4o-transcribe-diarize"), "provider"),
    "local-openai-whisper-1": ("local", ("openai", "whisper-1"), "local"),  # text only
    "local-no-config": ("local", None, "local"),  # nothing that could transcribe
    "local-local-config": ("local", ("local", "large-v3-turbo"), "local"),
    "pyannote-deepgram": ("pyannote", ("deepgram", "nova-3"), "pyannote"),
    "off-deepgram": ("off", ("deepgram", "nova-3"), "off"),
}


def _seed(engine: Engine) -> dict[str, int]:
    """Every CASES user, plus a file, segment and speaker that no revision may touch."""
    with engine.begin() as conn:
        ids = {}
        for label, (source, active, _after) in CASES.items():
            user_id = _user(conn, label)
            _setting(conn, user_id, _SOURCE, source)
            if active is not None:
                _active(conn, user_id, *active)
            ids[label] = user_id
        owner = ids["local-deepgram"]
        file_id = int(
            conn.execute(
                text(
                    "INSERT INTO media_file (user_id, filename, storage_path, file_size, "
                    "content_type, status) VALUES (:u, 'kept.wav', 'k/kept.wav', 1, "
                    "'audio/wav', 'completed') RETURNING id"
                ),
                {"u": owner},
            ).scalar_one()
        )
        speaker_id = int(
            conn.execute(
                text(
                    "INSERT INTO speaker (user_id, media_file_id, name, display_name) "
                    "VALUES (:u, :f, 'SPEAKER_00', 'Alice') RETURNING id"
                ),
                {"u": owner, "f": file_id},
            ).scalar_one()
        )
        conn.execute(
            text(
                "INSERT INTO transcript_segment (media_file_id, speaker_id, start_time, "
                "end_time, text) VALUES (:f, :s, 0.0, 1.0, 'hello')"
            ),
            {"f": file_id, "s": speaker_id},
        )
    return ids


def _sources(engine: Engine, ids: dict[str, int]) -> dict[str, str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT user_id, setting_value FROM user_setting "
                "WHERE setting_key = :k AND user_id = ANY(:ids)"
            ),
            {"k": _SOURCE, "ids": list(ids.values())},
        ).all()
    by_id = {int(user_id): str(value) for user_id, value in rows}
    return {label: by_id[user_id] for label, user_id in ids.items()}


def _untouched_data(engine: Engine) -> tuple:
    with engine.connect() as conn:
        return (
            conn.execute(text("SELECT filename, status FROM media_file ORDER BY id")).all(),
            conn.execute(text("SELECT name, display_name FROM speaker ORDER BY id")).all(),
            conn.execute(text("SELECT text, speaker_id FROM transcript_segment ORDER BY id")).all(),
            conn.execute(
                text(
                    "SELECT user_id, setting_key, setting_value FROM user_setting "
                    "WHERE setting_key <> :k ORDER BY user_id, setting_key"
                ),
                {"k": _SOURCE},
            ).all(),
        )


def _version(engine: Engine) -> str:
    with engine.connect() as conn:
        return str(conn.execute(text("SELECT version_num FROM alembic_version")).scalar())


def _detected(engine: Engine, mode: str, monkeypatch) -> str | None:
    from app.db.migrations import _detect_schema_version

    monkeypatch.setenv("DEPLOYMENT_MODE", mode)
    with engine.connect() as conn:
        return _detect_schema_version(conn, inspect(conn).get_table_names())


# --- the runs -----------------------------------------------------------------------------


def test_lite_upgrade_down_and_up_again(throwaway_pg, monkeypatch):
    pg = throwaway_pg
    db, engine = _new_database(pg, "lite")
    try:
        _alembic(pg, db, "lite", "upgrade", V434)
        ids = _seed(engine)
        data_before = _untouched_data(engine)
        before = _sources(engine, ids)
        assert before == {label: case[0] for label, case in CASES.items()}
        # Detection on the seeded-but-unmigrated lite database: stamps BELOW v436.
        assert _detected(engine, "lite", monkeypatch) == V434

        _alembic(pg, db, "lite", "upgrade", "head")
        after = _sources(engine, ids)
        assert after == {label: case[2] for label, case in CASES.items()}
        assert _untouched_data(engine) == data_before
        assert _version(engine) == V436
        assert _detected(engine, "lite", monkeypatch) == V436

        _alembic(pg, db, "lite", "downgrade", V434)
        assert _version(engine) == V434
        assert _sources(engine, ids) == after  # no-op downgrade

        _alembic(pg, db, "lite", "upgrade", "head")
        assert _sources(engine, ids) == after  # idempotent: nothing left to match
        assert _untouched_data(engine) == data_before
    finally:
        engine.dispose()


def test_the_full_image_upgrade_changes_nothing(throwaway_pg, monkeypatch):
    """Control: the same data under DEPLOYMENT_MODE=full keeps every stored choice."""
    pg = throwaway_pg
    db, engine = _new_database(pg, "full")
    try:
        _alembic(pg, db, "full", "upgrade", V434)
        ids = _seed(engine)
        data_before = _untouched_data(engine)
        before = _sources(engine, ids)

        _alembic(pg, db, "full", "upgrade", "head")

        assert _sources(engine, ids) == before
        assert _untouched_data(engine) == data_before
        assert _version(engine) == V436
        assert _detected(engine, "full", monkeypatch) == V436
    finally:
        engine.dispose()
