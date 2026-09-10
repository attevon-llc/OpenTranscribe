"""Behavioural proof for ``dbs_wait_for_speaker_attributes``'s extended settle predicates
(issue #620 item 5, the #617 follow-up).

``scripts/release-tests/lib/db-snapshot.sh``'s ``dbs_wait_for_speaker_attributes`` used to
poll only ``speaker.attributes_predicted_at IS NOT NULL`` — but LLM speaker-suggestion writes
(``backend/app/tasks/speaker_identification_task.py``) dispatch AFTER gender-attribute
detection and can still be in flight once that predicate alone is satisfied. The function now
also waits for (a) no ``task`` row of ``task_type='speaker_identification'`` in
``pending``/``in_progress`` for the given files, and (b) the speaker table's content digest to
repeat across 3 consecutive polls ~5s apart.

This test drives the REAL function, sourced from the real ``scripts/release-tests/lib/
db-snapshot.sh``, against a throwaway, network-isolated Postgres container — never the dev
stack's live database. Same safety posture and container-lifecycle pattern as
``test_opentr_restore_roundtrip.py`` (issue #599): ``--network none``, a uuid4-suffixed
container name, the Postgres image tag parsed from ``docker-compose.yml`` rather than
hardcoded, and teardown in a ``finally``.

Deliberately no ``RUN_*`` gate, matching ``test_opentr_restore_roundtrip.py`` — only Docker is
required, checked by the module-level ``skipif``.

Run directly: ``cd backend && PYTHONPATH=. pytest -m integration
tests/integration/test_dbs_wait_for_speaker_attributes.py -v``
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from tests.integration import throwaway_pg

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available"),
]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DB_SNAPSHOT_SH = _REPO_ROOT / "scripts" / "release-tests" / "lib" / "db-snapshot.sh"
_DB_USER = "postgres"
_DB_NAME = "opentranscribe_test"

pytestmark.append(
    pytest.mark.skipif(
        not _DB_SNAPSHOT_SH.exists(), reason="scripts/release-tests/lib/db-snapshot.sh not present"
    )
)

_SCHEMA_SQL = """
CREATE TABLE media_file (
    id serial PRIMARY KEY,
    uuid uuid NOT NULL UNIQUE
);
CREATE TABLE speaker (
    id serial PRIMARY KEY,
    media_file_id int NOT NULL REFERENCES media_file(id),
    name text,
    attributes_predicted_at timestamptz
);
CREATE TABLE task (
    id text PRIMARY KEY,
    media_file_id int REFERENCES media_file(id),
    task_type text NOT NULL,
    status text NOT NULL
);
"""


def _run(cmd: list[str], *, stdin_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        cmd, capture_output=True, text=True, input=stdin_text
    )


@pytest.fixture
def pg_container(isolated_pg: str) -> str:
    """The session's throwaway, network-isolated Postgres container, with this module's
    schema created in a fresh ``_DB_NAME`` database.

    Was a private ``docker run`` per test (~124 s of setup each, 3 tests). It is now the
    shared container from ``tests/integration/conftest.py``: ``isolated_pg`` drops every
    database created during the test, so each test still gets a virgin ``_DB_NAME`` -- which
    matters here, because all three tests seed ``media_file (id=1)`` and would collide if the
    database persisted between them.

    Nothing in this module touches cluster-level state; ``dbs_wait_for_speaker_attributes``
    is handed the database name explicitly and only reads from it.
    """
    create = throwaway_pg.psql(
        isolated_pg,
        "postgres",
        # One `docker exec` rather than two: CREATE DATABASE cannot run inside the target
        # database, so psql's \connect switches to it before the schema DDL. Cheap either
        # way (an exec is ~0.1 s) but it keeps the failure to a single reported step.
        f'CREATE DATABASE "{_DB_NAME}" OWNER {throwaway_pg.DB_USER};\n'
        f"\\connect {_DB_NAME}\n"
        f"{_SCHEMA_SQL}",
    )
    assert create.returncode == 0, f"database/schema creation failed: {create.stderr}"
    return isolated_pg


def _exec_sql(container: str, sql: str) -> None:
    result = _run(
        [
            "docker",
            "exec",
            "-i",
            container,
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            _DB_USER,
            _DB_NAME,
        ],
        stdin_text=sql,
    )
    assert result.returncode == 0, f"seed SQL failed: {result.stderr}\nSQL: {sql}"


def _call_dbs_wait(container: str, timeout: int, file_uuid: str) -> int:
    """Run the REAL dbs_wait_for_speaker_attributes, sourced from the real db-snapshot.sh,
    and return its exit code.
    """
    proc = _run(
        [
            "bash",
            "-c",
            f'source "{_DB_SNAPSHOT_SH}"; '
            f"declare -F dbs_wait_for_speaker_attributes >/dev/null || "
            f'{{ echo "FUNCTION_MISSING" >&2; exit 2; }}; '
            f'dbs_wait_for_speaker_attributes "{container}" "{_DB_USER}" "{_DB_NAME}" '
            f'"{timeout}" "{file_uuid}"',
        ],
    )
    return proc.returncode


@pytest.mark.integration
def test_settles_when_attributes_done_and_no_pending_llm_task(pg_container: str) -> None:
    """Clean case: attributes predicted, no speaker_identification task at all -> settles."""
    fid = str(uuid.uuid4())
    _exec_sql(
        pg_container,
        f"""
        INSERT INTO media_file (id, uuid) VALUES (1, '{fid}');
        INSERT INTO speaker (media_file_id, name, attributes_predicted_at)
            VALUES (1, 'Speaker 1', now());
        """,
    )
    # Needs to survive 3 consecutive 5s-apart digest polls with nothing changing --
    # give it real headroom above that ~15s floor.
    rc = _call_dbs_wait(pg_container, timeout=40, file_uuid=fid)
    assert rc == 0, "expected dbs_wait_for_speaker_attributes to settle (rc=0) on stable, done data"


@pytest.mark.integration
def test_returns_nonzero_when_llm_task_stuck_in_progress(pg_container: str) -> None:
    """A speaker_identification Task row stuck in_progress must never let this settle --
    that is exactly the write-still-in-flight case issue #620 item 5 exists to catch.
    """
    fid = str(uuid.uuid4())
    _exec_sql(
        pg_container,
        f"""
        INSERT INTO media_file (id, uuid) VALUES (1, '{fid}');
        INSERT INTO speaker (media_file_id, name, attributes_predicted_at)
            VALUES (1, 'Speaker 1', now());
        INSERT INTO task (id, media_file_id, task_type, status)
            VALUES ('stuck-task-1', 1, 'speaker_identification', 'in_progress');
        """,
    )
    # Short timeout: the function sleeps 5s per poll and never exits early on this
    # predicate, so a bounded timeout here just means "give up sooner", not "check less".
    rc = _call_dbs_wait(pg_container, timeout=8, file_uuid=fid)
    assert rc != 0, (
        "expected dbs_wait_for_speaker_attributes to time out (rc!=0) with a "
        "speaker_identification task stuck in_progress"
    )


@pytest.mark.integration
def test_zero_speaker_rows_is_trivially_settled(pg_container: str) -> None:
    """A file with no speaker rows (single-speaker content, diarization off) must not
    block forever waiting on rows that will never exist.
    """
    fid = str(uuid.uuid4())
    _exec_sql(pg_container, f"INSERT INTO media_file (id, uuid) VALUES (1, '{fid}');")
    rc = _call_dbs_wait(pg_container, timeout=40, file_uuid=fid)
    assert rc == 0, "expected a file with zero speaker rows to settle trivially"
