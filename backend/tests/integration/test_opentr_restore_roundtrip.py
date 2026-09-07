"""End-to-end proof for the ``./opentr.sh restore`` fix (issue #599).

The shipped restore replayed a plain ``pg_dump`` file with a bare ``psql < backup.sql``
into a database that already had that schema+data. Every statement failed — but without
``ON_ERROR_STOP``, ``psql`` exits **0** anyway, and the restore reported success while
changing nothing. Worse: the dump's ``alembic_version`` row does not collide on primary
key with a drifted row already present, so it inserts successfully while every
data-table ``COPY`` fails — leaving TWO rows in ``alembic_version``, which Alembic can no
longer migrate from. That is silent *corruption*, not a no-op.

This test drives the REAL ``scripts/common.sh`` helpers (``pg_drop_and_recreate_database``,
``pg_replay_dump``, ``pg_verify_restore``) the shipped ``opentr.sh restore`` uses, against a
throwaway, network-isolated Postgres container — never the dev stack's live database.

Safety posture:
- ``--network none`` — the container cannot reach anything, including the live stack.
- A ``uuid4``-suffixed container name — cannot collide with, or be mistaken for, a real
  deployment's container.
- The image tag is **parsed from ``docker-compose.yml``**, not hardcoded, so this test tracks
  the pinned Postgres version rather than silently drifting from it.
- The container is removed in a ``finally`` block (via the ``pg_container`` fixture) even if
  a test fails or raises.

Deliberately **no** ``RUN_*`` env-var gate — this repo has been burned before by a stale gate
hiding a real test (issue #431: 240 security tests gated off behind stale env vars).
``scripts/run-integration-tests.sh`` already globs ``tests/integration/``, so this needs no
harness change; it only needs Docker, which the module-level ``skipif`` checks for.

Run directly: ``cd backend && PYTHONPATH=. pytest -m integration
tests/integration/test_opentr_restore_roundtrip.py -v``
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tests.integration import throwaway_pg

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available"),
]

# This module no longer starts its own container -- `pg_container` below now takes the shared
# one -- but `test_scheduled_backup_restore_roundtrip.py` imports these two names FROM HERE
# (along with `_DB_USER`, `_run`, `_create_db`, `_query`, `_media_filenames`,
# `_call_common_fn`), because it still provisions containers of its own. They are aliases of
# the canonical implementations in `throwaway_pg`, not second copies.
#
# ⚠️ That cross-test-module import is a wart worth removing: the importer should depend on
# `throwaway_pg` directly. Left as-is here only to keep this change to files in one lane.
_postgres_image_tag = throwaway_pg.postgres_image_tag
_wait_ready = throwaway_pg.wait_ready

_REPO_ROOT = Path(__file__).resolve().parents[3]
_COMMON_SH = _REPO_ROOT / "scripts" / "common.sh"
_DB_USER = "postgres"

_SEED_SQL = """
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE TABLE alembic_version (version_num varchar(32) NOT NULL PRIMARY KEY);
INSERT INTO alembic_version VALUES ('v0392_seed');
CREATE TABLE media_file (id uuid PRIMARY KEY DEFAULT uuid_generate_v4(), filename text NOT NULL);
INSERT INTO media_file (filename) VALUES ('a.wav'), ('b.wav'), ('c.wav');
"""

# A later migration (speaker_persona) plus a data change made after the backup was taken —
# the exact shape the original issue measured (a table the dump's schema knows nothing about).
_DRIFT_SQL = """
DELETE FROM media_file WHERE filename = 'b.wav';
INSERT INTO media_file (filename) VALUES ('post_backup.wav');
CREATE TABLE speaker_persona (id uuid PRIMARY KEY DEFAULT uuid_generate_v4(), name text);
UPDATE alembic_version SET version_num = 'v0400_later';
"""


def _run(
    cmd: list[str], *, stdin_text: str | None = None, cwd: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        cmd, capture_output=True, text=True, input=stdin_text, cwd=cwd
    )


@pytest.fixture
def pg_container(isolated_pg: str) -> str:
    """The session's throwaway, network-isolated Postgres container.

    Was a private ``docker run`` per test (~124 s of setup each, 6 tests). It is now the
    shared container from ``tests/integration/conftest.py``, which drops every database this
    test creates once it finishes — see that file for the measurements and for why the
    isolation guarantee is unchanged.

    Nothing in this module touches cluster-level state: each test names its own database
    (``otrestore_bug``, ``otrestore_fixed``, ``otrestore_rollback``, ``otrestore_force``,
    ``otrestore_head_{plain,custom}``) and every operation it performs — ``CREATE``/``DROP
    DATABASE``, ``pg_replay_dump``, corrupting ``alembic_version`` — is scoped to that
    database. Adding a test that reuses one of those names will fail loudly on
    ``_create_db``'s assert rather than silently sharing state.
    """
    return isolated_pg


def _create_db(container: str, dbname: str) -> None:
    result = _run(
        [
            "docker",
            "exec",
            container,
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            _DB_USER,
            "-d",
            "postgres",
            "-c",
            f'CREATE DATABASE "{dbname}" OWNER {_DB_USER};',
        ]
    )
    assert result.returncode == 0, f"CREATE DATABASE failed: {result.stderr}"


def _exec_sql(container: str, dbname: str, sql: str) -> None:
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
            dbname,
        ],
        stdin_text=sql,
    )
    assert result.returncode == 0, f"seed/drift SQL failed: {result.stderr}"


def _dump(container: str, dbname: str, dest: Path) -> None:
    result = _run(["docker", "exec", container, "pg_dump", "-U", _DB_USER, dbname])
    assert result.returncode == 0, f"pg_dump failed: {result.stderr}"
    dest.write_text(result.stdout, encoding="utf-8")


def _dump_custom(container: str, dbname: str, dest: Path) -> None:
    """Custom-format (``-Fc``) dump — binary, so captured/written as bytes, not text."""
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["docker", "exec", container, "pg_dump", "-U", _DB_USER, "-Fc", dbname],
        capture_output=True,
    )
    assert result.returncode == 0, f"pg_dump -Fc failed: {result.stderr!r}"
    dest.write_bytes(result.stdout)


def _awk_extract_head(dump_path: Path) -> str:
    """The exact awk expression ``opentr.sh`` uses to read a plain-SQL dump's alembic head."""
    result = _run(
        ["awk", r"/^COPY public\.alembic_version /{getline; print; exit}", str(dump_path)]
    )
    assert result.returncode == 0, f"awk head extraction failed: {result.stderr}"
    return result.stdout.strip()


def _query(container: str, dbname: str, sql: str) -> str:
    result = _run(["docker", "exec", container, "psql", "-tA", "-U", _DB_USER, dbname, "-c", sql])
    assert result.returncode == 0, f"query failed: {result.stderr}"
    return result.stdout.strip()


def _call_common_fn(name: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Invoke a real ``scripts/common.sh`` function — the exact code opentr.sh ships."""
    return _run(
        ["bash", "-c", f'source "{_COMMON_SH}"; {name} "$@"', "--", *args], cwd=str(_REPO_ROOT)
    )


def _seed(container: str, dbname: str) -> None:
    _create_db(container, dbname)
    _exec_sql(container, dbname, _SEED_SQL)


def _drift(container: str, dbname: str) -> None:
    _exec_sql(container, dbname, _DRIFT_SQL)


def _media_filenames(container: str, dbname: str) -> list[str]:
    out = _query(container, dbname, "SELECT filename FROM media_file ORDER BY filename;")
    return [line for line in out.splitlines() if line]


# ---------------------------------------------------------------------------------------------
# 1. Pin the bug as a property of `psql` itself (the old shape), so it can never rot.
# ---------------------------------------------------------------------------------------------


def test_restoring_into_a_populated_database_reproduces_the_shipped_silent_failure(
    pg_container: str, tmp_path: Path
) -> None:
    container = pg_container
    dbname = "otrestore_bug"
    _seed(container, dbname)

    dump_path = tmp_path / "backup.sql"
    _dump(container, dbname, dump_path)

    _drift(container, dbname)

    # The OLD shipped shape: no ON_ERROR_STOP, no --single-transaction, no drop-first.
    with dump_path.open("rb") as dump_fh:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["docker", "exec", "-i", container, "psql", "-U", _DB_USER, dbname],
            stdin=dump_fh,
            capture_output=True,
            text=True,
        )

    assert result.returncode == 0, (
        "this is the bug being pinned: the OLD restore shape reports success (exit 0) even "
        f"though every statement failed. Got exit {result.returncode} instead — if psql's own "
        "behavior changed, this test's premise needs re-checking, not the app code."
    )

    filenames = set(_media_filenames(container, dbname))
    assert filenames == {"a.wav", "c.wav", "post_backup.wav"}, (
        f"expected the drifted (unrestored) data to survive the no-op 'restore', got {filenames}"
    )

    version_rows = _query(
        container, dbname, "SELECT version_num FROM alembic_version ORDER BY version_num;"
    )
    row_count = len([line for line in version_rows.splitlines() if line])
    assert row_count == 2, (
        "this is the silent-corruption half of the bug: the backup's alembic_version row does "
        f"not collide with the drifted row's primary key, so BOTH survive. Got {row_count} row(s): "
        f"{version_rows!r}"
    )


# ---------------------------------------------------------------------------------------------
# 2. The fix's full sequence reproduces the backup EXACTLY.
# ---------------------------------------------------------------------------------------------


def test_restore_flow_replaces_the_database_exactly(pg_container: str, tmp_path: Path) -> None:
    container = pg_container
    dbname = "otrestore_fixed"
    _seed(container, dbname)

    dump_path = tmp_path / "backup.sql"
    _dump(container, dbname, dump_path)

    _drift(container, dbname)
    # Sanity: drift actually happened, or the rest of this test proves nothing.
    assert set(_media_filenames(container, dbname)) == {"a.wav", "c.wav", "post_backup.wav"}

    exec_prefix = f"docker exec -i {container}"

    drop_result = _call_common_fn("pg_drop_and_recreate_database", exec_prefix, _DB_USER, dbname)
    assert drop_result.returncode == 0, (
        f"pg_drop_and_recreate_database failed: {drop_result.stderr}"
    )

    replay_result = _call_common_fn("pg_replay_dump", exec_prefix, _DB_USER, dbname, str(dump_path))
    assert replay_result.returncode == 0, f"pg_replay_dump failed: {replay_result.stderr}"

    verify_result = _call_common_fn(
        "pg_verify_restore", exec_prefix, _DB_USER, dbname, str(dump_path)
    )
    assert verify_result.returncode == 0, (
        f"pg_verify_restore reported a mismatch: {verify_result.stdout}"
    )

    assert set(_media_filenames(container, dbname)) == {"a.wav", "b.wav", "c.wav"}, (
        "post_backup.wav should be gone (it postdates the backup) and b.wav should be back "
        "(it was deleted after the backup)"
    )

    speaker_persona_exists = _query(
        container,
        dbname,
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = 'speaker_persona';",
    )
    assert speaker_persona_exists == "0", (
        "speaker_persona (the later-migration table) must not survive an exact restore"
    )

    version_rows = _query(
        container, dbname, "SELECT version_num FROM alembic_version;"
    ).splitlines()
    assert version_rows == ["v0392_seed"], (
        f"expected exactly one row 'v0392_seed', got {version_rows}"
    )

    extension_present = _query(
        container, dbname, "SELECT extname FROM pg_extension WHERE extname = 'uuid-ossp';"
    )
    assert extension_present == "uuid-ossp", (
        "uuid-ossp extension should be recreated by the dump's CREATE EXTENSION"
    )


# ---------------------------------------------------------------------------------------------
# 3. A failed replay rolls back to nothing, and the pre-restore safety dump recovers it.
# ---------------------------------------------------------------------------------------------


def test_a_failed_restore_rolls_back_whole_and_the_safety_dump_recovers(
    pg_container: str, tmp_path: Path
) -> None:
    container = pg_container
    dbname = "otrestore_rollback"
    _seed(container, dbname)

    backup_path = tmp_path / "backup.sql"
    _dump(container, dbname, backup_path)

    _drift(container, dbname)

    # Simulate opentr.sh's mandatory pre-restore safety dump of the CURRENT (drifted) state,
    # taken immediately before the destructive drop.
    safety_dump_path = tmp_path / "pre-restore-safety.sql"
    _dump(container, dbname, safety_dump_path)

    exec_prefix = f"docker exec -i {container}"

    drop_result = _call_common_fn("pg_drop_and_recreate_database", exec_prefix, _DB_USER, dbname)
    assert drop_result.returncode == 0, (
        f"pg_drop_and_recreate_database failed: {drop_result.stderr}"
    )

    corrupted_path = tmp_path / "corrupted.sql"
    corrupted_path.write_text(
        backup_path.read_text(encoding="utf-8") + "\nTHIS IS NOT VALID SQL AT ALL;\n",
        encoding="utf-8",
    )

    replay_result = _call_common_fn(
        "pg_replay_dump", exec_prefix, _DB_USER, dbname, str(corrupted_path)
    )
    assert replay_result.returncode != 0, (
        "replaying a corrupted dump must fail, not silently succeed"
    )

    table_count = _query(
        container,
        dbname,
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';",
    )
    assert table_count == "0", (
        "a failed replay must roll back the WHOLE transaction (--single-transaction), leaving zero "
        f"tables — a nonzero count means a hybrid schema survived, got {table_count}"
    )

    # Recover: drop the (empty) database again and replay the safety dump of the drifted state.
    recover_drop = _call_common_fn("pg_drop_and_recreate_database", exec_prefix, _DB_USER, dbname)
    assert recover_drop.returncode == 0, (
        f"recovery pg_drop_and_recreate_database failed: {recover_drop.stderr}"
    )

    recover_replay = _call_common_fn(
        "pg_replay_dump", exec_prefix, _DB_USER, dbname, str(safety_dump_path)
    )
    assert recover_replay.returncode == 0, (
        f"recovery pg_replay_dump failed: {recover_replay.stderr}"
    )

    assert set(_media_filenames(container, dbname)) == {"a.wav", "c.wav", "post_backup.wav"}, (
        "the safety dump must recover the exact drifted state that existed before the failed restore"
    )
    version_rows = _query(
        container, dbname, "SELECT version_num FROM alembic_version;"
    ).splitlines()
    assert version_rows == ["v0400_later"], (
        f"expected the drifted head 'v0400_later' back, got {version_rows}"
    )
    speaker_persona_exists = _query(
        container,
        dbname,
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = 'speaker_persona';",
    )
    assert speaker_persona_exists == "1", (
        "speaker_persona (created during drift) should be back after recovery"
    )


# ---------------------------------------------------------------------------------------------
# 4. DROP DATABASE ... WITH (FORCE) terminates an open connection instead of failing.
# ---------------------------------------------------------------------------------------------


def test_drop_database_with_force_terminates_an_open_connection(pg_container: str) -> None:
    container = pg_container
    dbname = "otrestore_force"
    _create_db(container, dbname)

    holder = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [
            "docker",
            "exec",
            "-i",
            container,
            "psql",
            "-U",
            _DB_USER,
            dbname,
            "-c",
            "SELECT pg_sleep(30);",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Poll pg_stat_activity in a loop (never a bare sleep) until the holder session appears.
        deadline = time.monotonic() + 10.0
        activity_count = "0"
        while time.monotonic() < deadline:
            activity_count = _query(
                container,
                "postgres",
                f"SELECT count(*) FROM pg_stat_activity WHERE datname = '{dbname}' AND state = 'active';",
            )
            if activity_count != "0":
                break
            time.sleep(0.2)
        assert activity_count != "0", (
            "the pg_sleep holder session never appeared in pg_stat_activity"
        )

        exec_prefix = f"docker exec -i {container}"
        drop_result = _call_common_fn(
            "pg_drop_and_recreate_database", exec_prefix, _DB_USER, dbname
        )
        assert drop_result.returncode == 0, (
            "DROP DATABASE ... WITH (FORCE) should terminate the open connection and succeed "
            f"rather than blocking/erroring on it: {drop_result.stderr}"
        )
    finally:
        holder.kill()
        holder.wait(timeout=5)


# ---------------------------------------------------------------------------------------------
# 5. Issue #610: the real head-extraction functions feed the real restart decision correctly,
#    for BOTH dump formats — closing the gap between "the decision function is right"
#    (unit/test_restore_restart_decision.py) and "the values fed into it are right".
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("dump_format", ["plain", "custom"])
def test_head_extraction_feeds_the_restart_decision(
    pg_container: str, tmp_path: Path, dump_format: str
) -> None:
    container = pg_container
    dbname = f"otrestore_head_{dump_format}"
    _create_db(container, dbname)
    _exec_sql(
        container,
        dbname,
        "CREATE TABLE alembic_version (version_num varchar(32) NOT NULL PRIMARY KEY);\n"
        "INSERT INTO alembic_version VALUES ('vOLD_seed');\n"
        "CREATE TABLE media_file (id serial PRIMARY KEY, filename text NOT NULL);\n",
    )

    exec_prefix = f"docker exec -i {container}"

    def _extract(dest_stem: str) -> str:
        if dump_format == "plain":
            dump_path = tmp_path / f"{dest_stem}.sql"
            _dump(container, dbname, dump_path)
            return _awk_extract_head(dump_path)
        dump_path = tmp_path / f"{dest_stem}.dump"
        _dump_custom(container, dbname, dump_path)
        result = _call_common_fn(
            "pg_custom_dump_expected_head", exec_prefix, _DB_USER, str(dump_path)
        )
        assert result.returncode == 0, f"pg_custom_dump_expected_head failed: {result.stderr}"
        return result.stdout.strip()

    # 1. Extract the backup's own head, taken while the DB is still at the seeded version.
    dump_head = _extract("backup")
    assert dump_head == "vOLD_seed", f"expected the dump's own head 'vOLD_seed', got {dump_head!r}"

    # 2. Drift the LIVE database forward after the backup was taken — the exact shape of a
    #    rollback scenario: a newer image has already migrated the schema since the backup.
    _exec_sql(container, dbname, "UPDATE alembic_version SET version_num = 'vNEW_later';")
    live_head = _query(container, dbname, "SELECT version_num FROM alembic_version;")
    assert live_head == "vNEW_later", (
        f"expected the drifted live head 'vNEW_later', got {live_head!r}"
    )

    # 3. Feed the REAL extracted values into the REAL shared decision function (issue #610).
    mismatch = _call_common_fn(
        "pg_restore_restart_decision", dump_head, live_head, "false", "false"
    )
    assert mismatch.returncode == 0, f"pg_restore_restart_decision failed: {mismatch.stderr}"
    assert mismatch.stdout.strip() == "hold:schema-mismatch", (
        f"[{dump_format}] expected hold:schema-mismatch (backup={dump_head!r} vs "
        f"live={live_head!r}), got {mismatch.stdout!r}"
    )

    # 4. Control: dump the now-DRIFTED database and extract ITS head too. Same head as the
    #    live DB it came from -> restart. Proves the mismatch above is about the head
    #    values actually differing, not an artifact of always holding.
    control_head = _extract("control")
    assert control_head == "vNEW_later", (
        f"expected the control dump's head 'vNEW_later', got {control_head!r}"
    )

    control_decision = _call_common_fn(
        "pg_restore_restart_decision", control_head, live_head, "false", "false"
    )
    assert control_decision.returncode == 0, (
        f"pg_restore_restart_decision failed: {control_decision.stderr}"
    )
    assert control_decision.stdout.strip() == "restart", (
        f"[{dump_format}] expected restart for the matching-head control, got "
        f"{control_decision.stdout!r}"
    )
