"""Behavioural proof for the #619 fix: waiting for `media_file` / `system_settings` to stop
changing before test-upgrade.sh phase 06b takes its pre-upgrade snapshot/backups.

Same failure class as #617's Layer 1 (closed for the `speaker` table by
``dbs_wait_for_speaker_attributes``): the app has async post-completion writers into
``media_file`` (e.g. redaction detection's ``redaction_status``/``redaction_model_version``/
``redaction_coverage`` when redaction is enabled) that are not individually tracked, and a
startup-timer task (``app/main.py::_run_one_time_embedding_normalization``, fires ~60s after
backend startup) that writes into ``system_settings``. Both can straddle phase 06b's
snapshot/backup points and produce a false digest mismatch (``F-4``) or a false backup-content
diff.

``scripts/release-tests/lib/db-snapshot.sh`` gained a generic settle primitive,
``dbs_wait_for_stable_query``, plus two thin wrappers (``dbs_wait_for_media_file_settled``,
``dbs_wait_for_system_settings_settled``) rather than one predicate per writer — the set of
writers is not exhaustively known (and would grow every time a new one is added), so
quiescence (the table's own content digest stops changing across 3 consecutive polls) is the
signal actually being waited on.

This test drives the REAL ``dbs_wait_for_stable_query``/``dbs_wait_for_media_file_settled``,
sourced from the real ``scripts/release-tests/lib/db-snapshot.sh``, against a throwaway,
network-isolated Postgres container — never the dev stack's live database. Same pattern as
``test_opentr_restore_roundtrip.py`` (#599) and ``test_dbs_wait_for_speaker_attributes.py``
(#620 item 5): ``--network none``, a uuid4-suffixed container name, the Postgres image tag
parsed from ``docker-compose.yml``, teardown in a ``finally``.

No live rehearsal cycle here (hours-long, requires the stopped stack) — this is the
"simulate against the specific write-timing gap" verification instead: a background writer
loop stands in for the app's async post-completion tasks, proving the wait function actually
notices an ongoing write and actually clears once it stops.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available"),
]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DB_SNAPSHOT_SH = _REPO_ROOT / "scripts" / "release-tests" / "lib" / "db-snapshot.sh"
_TEST_UPGRADE_SH = _REPO_ROOT / "scripts" / "release-tests" / "test-upgrade.sh"
_COMPOSE_FILE = _REPO_ROOT / "docker-compose.yml"
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
    uuid uuid NOT NULL UNIQUE,
    redaction_status text
);
"""


def _run(cmd: list[str], *, stdin_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        cmd, capture_output=True, text=True, input=stdin_text
    )


def _postgres_image_tag() -> str:
    compose = _COMPOSE_FILE.read_text(encoding="utf-8")
    match = re.search(r"image:\s*(postgres:\S+)", compose)
    assert match, "could not find an `image: postgres:<tag>` line in docker-compose.yml"
    return match.group(1)


def _wait_ready(container: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    last: subprocess.CompletedProcess[str] | None = None
    consecutive = 0
    while time.monotonic() < deadline:
        last = _run(
            [
                "docker",
                "exec",
                container,
                "psql",
                "-U",
                _DB_USER,
                "-d",
                "postgres",
                "-c",
                "SELECT 1;",
            ]
        )
        if last.returncode == 0:
            consecutive += 1
            if consecutive >= 2:
                return
        else:
            consecutive = 0
        time.sleep(0.3)
    raise RuntimeError(
        f"postgres in {container} never became ready: {last.stdout if last else 'no attempt made'}"
    )


@pytest.fixture
def pg_container() -> Iterator[str]:
    name = f"ot-dbs-settle-test-{uuid.uuid4().hex[:12]}"
    image = _postgres_image_tag()
    throwaway_password = uuid.uuid4().hex
    started = _run(
        [
            "docker",
            "run",
            "-d",
            "--network",
            "none",
            "--name",
            name,
            "-e",
            f"POSTGRES_PASSWORD={throwaway_password}",
            image,
        ]
    )
    assert started.returncode == 0, (
        f"failed to start throwaway postgres container: {started.stderr}"
    )
    try:
        _wait_ready(name)
        create = _run(
            [
                "docker",
                "exec",
                name,
                "psql",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                _DB_USER,
                "-d",
                "postgres",
                "-c",
                f'CREATE DATABASE "{_DB_NAME}" OWNER {_DB_USER};',
            ]
        )
        assert create.returncode == 0, f"CREATE DATABASE failed: {create.stderr}"
        schema = _run(
            [
                "docker",
                "exec",
                "-i",
                name,
                "psql",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                _DB_USER,
                _DB_NAME,
            ],
            stdin_text=_SCHEMA_SQL,
        )
        assert schema.returncode == 0, f"schema creation failed: {schema.stderr}"
        yield name
    finally:
        _run(["docker", "rm", "-f", name])


def _exec_sql(container: str, sql: str) -> subprocess.CompletedProcess[str]:
    return _run(
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


def _call_wait_function(function_name: str, container: str, timeout: int) -> int:
    proc = _run(
        [
            "bash",
            "-c",
            f'source "{_DB_SNAPSHOT_SH}"; '
            f"declare -F {function_name} >/dev/null || "
            f'{{ echo "FUNCTION_MISSING" >&2; exit 2; }}; '
            f'{function_name} "{container}" "{_DB_USER}" "{_DB_NAME}" "{timeout}"',
        ],
    )
    return proc.returncode


@pytest.mark.integration
def test_settles_quickly_when_nothing_is_writing(pg_container: str) -> None:
    """Static data: 3 consecutive 5s-apart polls agree immediately -> settles well inside
    a generous timeout.
    """
    _exec_sql(pg_container, "INSERT INTO media_file (uuid) VALUES (gen_random_uuid());")
    rc = _call_wait_function("dbs_wait_for_media_file_settled", pg_container, timeout=40)
    assert rc == 0, "expected dbs_wait_for_media_file_settled to settle on static data"


@pytest.mark.integration
def test_never_settles_while_a_writer_keeps_changing_the_table(pg_container: str) -> None:
    """A background writer loop stands in for the app's async post-completion tasks
    (e.g. redaction detection flipping redaction_status): as long as it keeps mutating
    media_file, the digest can never repeat 3 times in a row, so the wait must time out.
    """
    _exec_sql(pg_container, "INSERT INTO media_file (uuid) VALUES (gen_random_uuid());")

    stop = threading.Event()

    def _writer_loop() -> None:
        # stop.wait(timeout) rather than time.sleep(timeout): pacing between writes, not a
        # readiness poll, but using the interruptible form means teardown does not block
        # for up to 1.5s after the test has already gotten its answer.
        n = 0
        while not stop.wait(1.5):
            n += 1
            _exec_sql(
                pg_container,
                f"UPDATE media_file SET redaction_status = 'writer-{n}' "
                f"WHERE id = (SELECT id FROM media_file LIMIT 1);",
            )

    writer = threading.Thread(target=_writer_loop, daemon=True)
    writer.start()
    try:
        # Short timeout: with a write every 1.5s, three 5s-apart polls can never agree.
        # This only needs to prove the wait does NOT falsely settle, not exhaust the
        # full budget dbs_wait_for_media_file_settled uses in the real harness.
        rc = _call_wait_function("dbs_wait_for_media_file_settled", pg_container, timeout=13)
    finally:
        stop.set()
        writer.join(timeout=5)

    assert rc != 0, (
        "expected dbs_wait_for_media_file_settled to time out (rc!=0) while a writer was "
        "still actively changing the table"
    )


@pytest.mark.unit
def test_system_settings_wrapper_queries_the_right_table() -> None:
    """Guard against a copy-paste table-name bug between the two wrappers -- static, no
    Postgres needed.
    """
    source = _DB_SNAPSHOT_SH.read_text(encoding="utf-8")
    match = re.search(
        r"dbs_wait_for_system_settings_settled\(\)\s*\{(?P<body>.*?)\n\}",
        source,
        flags=re.DOTALL,
    )
    assert match, "dbs_wait_for_system_settings_settled not found in db-snapshot.sh"
    assert "FROM system_settings" in match.group("body")
    assert "FROM media_file" not in match.group("body")


@pytest.mark.unit
def test_media_file_wrapper_queries_the_right_table() -> None:
    source = _DB_SNAPSHOT_SH.read_text(encoding="utf-8")
    match = re.search(
        r"dbs_wait_for_media_file_settled\(\)\s*\{(?P<body>.*?)\n\}",
        source,
        flags=re.DOTALL,
    )
    assert match, "dbs_wait_for_media_file_settled not found in db-snapshot.sh"
    assert "FROM media_file" in match.group("body")
    assert "FROM system_settings" not in match.group("body")


@pytest.mark.unit
def test_phase_06b_waits_for_both_tables_before_the_first_dump() -> None:
    """Static shape check: both new waits must run BEFORE dbs_dump takes the shipped
    pg_dump snapshot -- waiting AFTER the first dump would defeat the point (issue #619).
    """
    assert _TEST_UPGRADE_SH.exists(), "scripts/release-tests/test-upgrade.sh not present"
    text = _TEST_UPGRADE_SH.read_text(encoding="utf-8")
    media_file_idx = text.find("dbs_wait_for_media_file_settled")
    system_settings_idx = text.find("dbs_wait_for_system_settings_settled")
    first_dump_idx = text.find('dbs_dump "$pg" "$db_user" "$db_name" "$shipped_dump"')
    assert media_file_idx != -1, "expected a dbs_wait_for_media_file_settled call"
    assert system_settings_idx != -1, "expected a dbs_wait_for_system_settings_settled call"
    assert first_dump_idx != -1, "expected the shipped-dump dbs_dump call"
    assert media_file_idx < first_dump_idx, (
        "dbs_wait_for_media_file_settled must run BEFORE the shipped pg_dump is taken"
    )
    assert system_settings_idx < first_dump_idx, (
        "dbs_wait_for_system_settings_settled must run BEFORE the shipped pg_dump is taken"
    )
