"""Integration proof for ``scripts/cleanup-test-users.py`` against a throwaway Postgres.

Issue #601: the script had ZERO test coverage and three real bugs (see
``backend/tests/unit/test_cleanup_test_users_safety.py`` for the full writeup — Bug A: an
orphan port variable that always silently resolved to the live dev stack; Bug B: one
blocked candidate aborted the ENTIRE bulk DELETE with an unhandled traceback; Bug C: the
dry-run report promised deletions the database could not actually perform). This file
proves those fixes against a REAL Postgres — the unit file cannot, since ``blocked_by_
foreign_keys`` and ``_delete_users`` both issue live SQL.

Safety posture, mirroring ``test_opentr_restore_roundtrip.py`` with one deliberate
divergence: that fixture used ``--network none`` because everything went through
``docker exec psql``. THIS script connects over TCP from the host with a real DSN — the
same way an operator invokes it — so the throwaway container needs a **published** port
instead; ``--network none`` would make it unreachable.

- The container publishes on ``127.0.0.1`` only, never ``0.0.0.0``.
- The allocated port is asserted to differ from 5176 (the live dev stack) and from
  whatever ``POSTGRES_PORT`` the ambient environment names, BEFORE anything touches the
  container — the one guard standing between a bug in this file and the live database.
- ``--tmpfs /var/lib/postgresql/data`` — nothing persisted to disk.
- A ``uuid4``-suffixed container name and a throwaway generated password.
- The container is removed in a ``finally`` block even if a test fails or raises.
- Every value the script under test resolves its DSN from (``POSTGRES_USER/PASSWORD/
  DB/PORT/HOST``) is passed EXPLICITLY via subprocess ``env`` — never left to fall back
  to ``.env``, which would make the test's real target ambiguous.

Deliberately **no** ``RUN_*`` env-var gate (issue #431: this repo has been burned before
by a stale gate hiding a real test). ``run-integration-tests.sh`` already globs
``tests/integration/`` under ``-m integration``, so this needs no harness change; it only
needs Docker, which the module-level ``skipif`` checks for.

Run directly: ``cd backend && PYTHONPATH=. pytest -m integration
tests/integration/test_cleanup_test_users_isolated_db.py -v``
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.engine import Engine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available"),
]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "cleanup-test-users.py"
_COMPOSE_FILE = _REPO_ROOT / "docker-compose.yml"
_DB_USER = "postgres"

# Every account the seed matrix plants, and why. Kept as one source of truth so the
# fixture and every test below read the same names.
_TAG_BLOCKED_EMAIL = "share-e2e-blocked@example.com"
_MEDIA_OWNER_EMAIL = "searchqual-x@example.invalid"
_LLM_OWNER_EMAIL = "real.person@example.com"
_NEAR_MISS_EMAIL = "testuserX@example.com"  # no literal '_' after 'testuser' — must survive

_SEED_STATEMENTS = (
    'CREATE TABLE "user" (id serial PRIMARY KEY, email text UNIQUE NOT NULL)',
    'CREATE TABLE media_file (id serial PRIMARY KEY, user_id int NOT NULL REFERENCES "user"(id))',
    'CREATE TABLE tag (id serial PRIMARY KEY, user_id int REFERENCES "user"(id))',
    (
        "CREATE TABLE user_llm_settings ("
        "id serial PRIMARY KEY, "
        'user_id int NOT NULL REFERENCES "user"(id) ON DELETE CASCADE, '
        "name text NOT NULL, base_url text, is_active boolean NOT NULL DEFAULT false)"
    ),
)


def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)  # noqa: S603


def _postgres_image_tag() -> str:
    """Parse the pinned Postgres image out of docker-compose.yml — never hardcoded."""
    compose = _COMPOSE_FILE.read_text(encoding="utf-8")
    match = re.search(r"image:\s*(postgres:\S+)", compose)
    assert match, "could not find an `image: postgres:<tag>` line in docker-compose.yml"
    return match.group(1)


def _free_port() -> int:
    """Bind ("127.0.0.1", 0), read the OS-assigned port, close it, then publish there.

    A genuine TOCTOU race is possible in principle (something else grabs the port before
    `docker run` publishes it) but is exactly what the equivalent fixtures elsewhere in
    this repo (e.g. release-test port allocation) already accept as the practical
    alternative to a fixed port list.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_ready(port: int, password: str, timeout: float = 30.0) -> None:
    """Poll ``SELECT 1`` in a bounded loop until TWO CONSECUTIVE successes.

    The official postgres image starts the server once to run initdb, stops it, then
    starts it again for real. A single successful connection can land in the brief
    window between those two starts, right before shutdown — so one "ready" reading is
    not trustworthy. Never a bare ``sleep``: this repo's audit-tests.py has a detector
    (``sleep-sync``) for exactly that anti-pattern.
    """
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
    """A throwaway, loopback-published Postgres container. Always removed after the module.

    See the module docstring for why this publishes a port instead of using
    ``--network none`` like ``test_opentr_restore_roundtrip.py``'s ``pg_container`` does.
    """
    name = f"ot-cleanup-test-{uuid.uuid4().hex[:12]}"
    image = _postgres_image_tag()
    # Fresh per run, not a credential anyone relies on: the container is removed at the
    # end of the module and published on loopback only.
    password = uuid.uuid4().hex
    port = _free_port()

    live_port = int(os.environ.get("POSTGRES_PORT", "5176"))
    # The one guard standing between a bug in this file and the live dev database: if
    # this ever resolves to the dev stack's port, refuse before `docker run` even
    # executes rather than after.
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


@pytest.fixture
def seeded_db(throwaway_pg: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """A fresh database, on the shared throwaway container, seeded per the issue #601 matrix."""
    # Guard the DDL below against drift: it is meant to MIRROR these two live FK rules.
    # If a future migration changes either, test_user_deletion_fk_coverage's own test
    # goes red AND this precondition goes red together — they check each other.
    from tests.unit.test_user_deletion_fk_coverage import NO_ACTION
    from tests.unit.test_user_deletion_fk_coverage import REGISTRY

    assert REGISTRY["media_file.user_id"].rule == NO_ACTION
    assert REGISTRY["tag.user_id"].rule == NO_ACTION

    port = throwaway_pg["port"]
    password = throwaway_pg["password"]
    dbname = f"otcleanup_{uuid.uuid4().hex[:8]}"  # deliberately NOT "opentranscribe"

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
        test_uuid_email = f"test-{uuid.uuid4().hex[:8]}@example.com"
        expected_delete = {
            "reg-e2e-abc123@example.com",
            test_uuid_email,
            "mfa-e2e-deadbeef@example.com",
        }
        expected_blocked = {_TAG_BLOCKED_EMAIL}
        expected_survivors = {
            _MEDIA_OWNER_EMAIL,
            "admin@example.com",
            "ldap-user@example.com",
            _LLM_OWNER_EMAIL,
            _NEAR_MISS_EMAIL,
        }

        with engine.begin() as conn:
            for stmt in _SEED_STATEMENTS:
                conn.execute(text(stmt))

            user_ids: dict[str, int] = {}
            for email in sorted(expected_delete | expected_blocked | expected_survivors):
                uid = conn.execute(
                    text('INSERT INTO "user" (email) VALUES (:email) RETURNING id'),
                    {"email": email},
                ).scalar_one()
                user_ids[email] = uid

            conn.execute(
                text("INSERT INTO tag (user_id) VALUES (:uid)"),
                {"uid": user_ids[_TAG_BLOCKED_EMAIL]},
            )
            for _ in range(2):
                conn.execute(
                    text("INSERT INTO media_file (user_id) VALUES (:uid)"),
                    {"uid": user_ids[_MEDIA_OWNER_EMAIL]},
                )
            conn.execute(
                text(
                    "INSERT INTO user_llm_settings (user_id, name, base_url, is_active) "
                    "VALUES (:uid, 'Mock LLM', 'http://mock-llm:5199/v1', true)"
                ),
                {"uid": user_ids[_LLM_OWNER_EMAIL]},
            )
            conn.execute(
                text(
                    "INSERT INTO user_llm_settings (user_id, name, base_url, is_active) "
                    "VALUES (:uid, 'OpenAI', 'https://api.openai.com/v1', false)"
                ),
                {"uid": user_ids[_LLM_OWNER_EMAIL]},
            )

        env = {
            **os.environ,
            "POSTGRES_USER": _DB_USER,
            "POSTGRES_PASSWORD": password,
            "POSTGRES_DB": dbname,
            "POSTGRES_PORT": str(port),
            "POSTGRES_HOST": "127.0.0.1",
        }

        yield {
            "engine": engine,
            "env": env,
            "dbname": dbname,
            "port": port,
            "user_ids": user_ids,
            "test_uuid_email": test_uuid_email,
            "expected_delete": expected_delete,
            "expected_blocked": expected_blocked,
            "expected_survivors": expected_survivors,
        }
    finally:
        engine.dispose()


def _run_script(env: dict[str, str], *flags: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(_SCRIPT), *flags],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _query_user_emails(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return set(conn.execute(text('SELECT email FROM "user"')).scalars().all())


#: Matches ONLY the user-sweep's "  WOULD DELETE  <email>" lines, never the LLM-config
#: sweep's "  WOULD DELETE  #<id> '<name>' -> <url> [...]" lines — both sections print
#: the identical verb, and the LLM line's last whitespace token is not an email at all
#: (a naive `rsplit(None, 1)` grabbed "failures]" off the "[ACTIVE ... failures]" suffix
#: the first time this was written).
_WOULD_DELETE_USER_RE = re.compile(r"^\s*WOULD DELETE\s+(\S+@\S+)\s*$")


def _would_delete_user_emails(stdout: str) -> set[str]:
    return {m.group(1) for line in stdout.splitlines() if (m := _WOULD_DELETE_USER_RE.match(line))}


def _snapshot(engine: Engine) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of every seeded table, as plain tuples (never SQLAlchemy ``Row``
    objects — mypy does not consider ``Row`` a ``sortable`` tuple, and a plain tuple
    is exactly what a row-for-row equality check needs anyway)."""
    with engine.connect() as conn:
        return {
            "user": sorted(
                tuple(row)
                for row in conn.execute(text('SELECT id, email FROM "user" ORDER BY id')).all()
            ),
            "media_file": sorted(
                tuple(row)
                for row in conn.execute(
                    text("SELECT id, user_id FROM media_file ORDER BY id")
                ).all()
            ),
            "tag": sorted(
                tuple(row)
                for row in conn.execute(text("SELECT id, user_id FROM tag ORDER BY id")).all()
            ),
            "user_llm_settings": sorted(
                tuple(row)
                for row in conn.execute(
                    text("SELECT id, user_id, base_url FROM user_llm_settings ORDER BY id")
                ).all()
            ),
        }


# ---------------------------------------------------------------------------------------------
# 1. Dry run mutates nothing — the single most safety-critical assertion in this file.
# ---------------------------------------------------------------------------------------------


def test_dry_run_deletes_nothing(seeded_db: dict[str, Any]) -> None:
    before = _snapshot(seeded_db["engine"])
    result = _run_script(seeded_db["env"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WOULD DELETE" in result.stdout
    after = _snapshot(seeded_db["engine"])
    assert after == before, "a dry run must not change a single row, in ANY table"


# ---------------------------------------------------------------------------------------------
# 2. The dry-run report names exactly the rows --execute removes (Bug C)
# ---------------------------------------------------------------------------------------------


def test_dry_run_report_names_exactly_the_rows_execute_removes(
    seeded_db: dict[str, Any],
) -> None:
    """RED before Bug C's fix: the batch aborted entirely on the blocked candidate, so
    --execute promised 4 removals in the dry run and delivered 0.
    """
    dry = _run_script(seeded_db["env"])
    assert dry.returncode == 0, dry.stdout + dry.stderr
    would_delete = _would_delete_user_emails(dry.stdout)
    assert would_delete, "dry run reported no WOULD DELETE <email> lines at all"

    before_emails = _query_user_emails(seeded_db["engine"])
    execute = _run_script(seeded_db["env"], "--execute")
    after_emails = _query_user_emails(seeded_db["engine"])
    removed_emails = before_emails - after_emails

    assert removed_emails == would_delete, (
        f"execute (exit {execute.returncode}) removed {removed_emails}, dry run promised "
        f"{would_delete}\n--- execute stdout ---\n{execute.stdout}"
    )


# ---------------------------------------------------------------------------------------------
# 3. --execute removes exactly the matched orphans — nothing more, nothing less
# ---------------------------------------------------------------------------------------------


def test_execute_removes_only_the_matched_orphans(seeded_db: dict[str, Any]) -> None:
    _run_script(seeded_db["env"], "--execute")
    survivors = _query_user_emails(seeded_db["engine"])
    expected = seeded_db["expected_survivors"] | seeded_db["expected_blocked"]
    assert survivors == expected


# ---------------------------------------------------------------------------------------------
# 4. A media-file owner is never deleted
# ---------------------------------------------------------------------------------------------


def test_a_user_who_owns_media_files_survives_execute(seeded_db: dict[str, Any]) -> None:
    result = _run_script(seeded_db["env"], "--execute")
    survivors = _query_user_emails(seeded_db["engine"])
    assert _MEDIA_OWNER_EMAIL in survivors

    with seeded_db["engine"].connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM media_file WHERE user_id = :uid"),
            {"uid": seeded_db["user_ids"][_MEDIA_OWNER_EMAIL]},
        ).scalar_one()
    assert count == 2, "the owned media_file rows must survive untouched too"

    assert re.search(
        rf"SKIP\s+{re.escape(_MEDIA_OWNER_EMAIL)} \(owns 2 media files", result.stdout
    ), result.stdout


# ---------------------------------------------------------------------------------------------
# 5. A blocked user is reported, does not abort the batch, and costs exit code 1 (Bug B)
# ---------------------------------------------------------------------------------------------


def test_a_blocked_user_is_reported_and_does_not_abort_the_batch(
    seeded_db: dict[str, Any],
) -> None:
    """RED before Bug B's fix: the single-statement bulk DELETE raised IntegrityError on
    the tag-blocked candidate and removed ZERO of the four legitimate orphans, crashing
    before the LLM-config sweep ran at all.
    """
    result = _run_script(seeded_db["env"], "--execute")

    assert result.returncode == 1, (
        f"a partial sweep must not read as success:\n{result.stdout}\n{result.stderr}"
    )
    assert _TAG_BLOCKED_EMAIL in result.stdout
    assert "tag.user_id" in result.stdout, "the blocking constraint must be named"

    survivors = _query_user_emails(seeded_db["engine"])
    assert _TAG_BLOCKED_EMAIL in survivors, "the blocked user must survive, not be attempted"
    assert (
        seeded_db["expected_delete"]
        <= (seeded_db["expected_delete"] | seeded_db["expected_blocked"]) - survivors
    ), "the other legitimate orphans must still be removed despite the block"


# ---------------------------------------------------------------------------------------------
# 6. Leaked LLM configs: reported in dry run (never mutated), deleted only with --execute
# ---------------------------------------------------------------------------------------------


def test_leaked_llm_configs_are_reported_in_dry_run_and_deleted_only_with_execute(
    seeded_db: dict[str, Any],
) -> None:
    dry = _run_script(seeded_db["env"])
    assert "mock-llm:5199" in dry.stdout
    assert "api.openai.com" not in dry.stdout, "only the test-infrastructure row is reported"

    with seeded_db["engine"].connect() as conn:
        urls_after_dry_run = set(
            conn.execute(text("SELECT base_url FROM user_llm_settings")).scalars().all()
        )
    assert "http://mock-llm:5199/v1" in urls_after_dry_run, "dry run must never mutate"
    assert "https://api.openai.com/v1" in urls_after_dry_run

    _run_script(seeded_db["env"], "--execute")
    with seeded_db["engine"].connect() as conn:
        urls_after_execute = set(
            conn.execute(text("SELECT base_url FROM user_llm_settings")).scalars().all()
        )
    assert "http://mock-llm:5199/v1" not in urls_after_execute
    assert "https://api.openai.com/v1" in urls_after_execute, (
        "the LLM owner is a non-candidate account, so this row's survival cannot be "
        "confounded by a cascade off the user row"
    )


# ---------------------------------------------------------------------------------------------
# 7. The script names the database it targets (Bug A)
# ---------------------------------------------------------------------------------------------


def test_the_script_names_the_database_it_targets(seeded_db: dict[str, Any]) -> None:
    """RED before Bug A's fix: no such line existed at all, and the DSN silently resolved
    to port 5176 (the live dev stack) regardless of what was passed. This asserts on the
    printed STRING only — it must never attempt a connection to port 5176 itself.
    """
    result = _run_script(seeded_db["env"])
    first_line = result.stdout.splitlines()[0] if result.stdout else "<no output>"
    expected = f"Target: {_DB_USER}@127.0.0.1:{seeded_db['port']}/{seeded_db['dbname']}"
    assert first_line == expected, f"got {result.stdout!r}"
