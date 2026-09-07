"""End-to-end proof for the scheduled/S3 `-Fc` backup's restore path (issue #600).

``backend/app/services/backup_service.run_pg_dump`` runs ``pg_dump --format=custom`` for
the in-app scheduled/S3 backup feature. Before this fix, ``pg_restore`` — the only tool
that reads that format — appeared nowhere in this repo: ``./opentr.sh restore`` sniffed
the file's ``PGDMP`` magic bytes and printed a hint command that was itself corrupting
(fixing its missing stdin redirect reproduces #599's exact silent-corruption bug). The fix
adds a real ``pg_restore``-based replay branch to the same ``restore_database`` function,
reusing #599's confirm / safety-dump / drop-recreate / verify machinery.

**Design constraint that shapes this file**: ``pg_dump``/``pg_restore`` are not installed
on this host (measured — this repo's Python venv has no PostgreSQL client tools), so a
test cannot call ``run_pg_dump`` in-process against a published port. Two-tier design:

- **Tier 1 — the trustworthy-evidence test** (``test_the_real_run_pg_dump_artifact_...``)
  runs the ACTUAL ``backup_service.run_pg_dump`` inside a container built from
  ``opentranscribe-backend:latest``, with this checkout's ``backend/`` bind-mounted over
  the image's ``/app`` (so it runs the exact source this branch ships, using the image's
  installed ``pg_dump``/``gpg``/Python environment), against a throwaway Postgres server
  on a private docker network. This is the test the issue asks for: a real artifact from
  the real code path, actually restored, data compared exactly. Skips — naming the build
  command, never silently — if the image isn't present locally.
- **Tier 2 — the cheap tests** produce their artifact with ``docker exec <pg> pg_dump
  --format=custom --no-owner --no-acl -U <user> <dbname>`` — the SAME format flags
  ``run_pg_dump`` passes (only the connection form differs: a positional ``-U``/dbname
  pair run locally inside the container via peer trust, vs. ``run_pg_dump``'s
  ``--dbname <url>``), avoiding a container spawn per test. This is legitimate ONLY
  because ``test_backup_restore_format_contract.py``'s
  ``test_run_pg_dump_still_produces_custom_format`` and Tier 1 together pin that the two
  producers write the identical format — without that pin this would be exactly the
  "fabricated dump" shortcut issue #600 warns against.

Safety posture, matching #599's: ``uuid4``-suffixed container/network names; the Postgres
image tag parsed from ``docker-compose.yml``, never hardcoded; Tier 2's ``pg_container``
runs with ``--network none`` (cannot reach anything, including the live stack); Tier 1's
network is a dedicated bridge with NO published ports, so the throwaway backend container
can reach only the throwaway Postgres container, never a compose service name. Every
container/network is removed in a ``finally``. The live dev stack and its database are
never written to.

Run directly: ``cd backend && PYTHONPATH=. pytest -m integration
tests/integration/test_scheduled_backup_restore_roundtrip.py -v``
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.integration.test_opentr_restore_roundtrip import _DB_USER
from tests.integration.test_opentr_restore_roundtrip import _call_common_fn
from tests.integration.test_opentr_restore_roundtrip import _create_db
from tests.integration.test_opentr_restore_roundtrip import _media_filenames
from tests.integration.test_opentr_restore_roundtrip import _postgres_image_tag
from tests.integration.test_opentr_restore_roundtrip import _query
from tests.integration.test_opentr_restore_roundtrip import _run
from tests.integration.test_opentr_restore_roundtrip import _wait_ready

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available"),
]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BACKEND_IMAGE = "opentranscribe-backend:latest"
_BACKEND_IMAGE_SKIP_REASON = (
    f"{_BACKEND_IMAGE} not found locally — build it first with './opentr.sh build' or "
    "'./opentr.sh start dev --build', then re-run this test. A silent skip here would be "
    "indistinguishable from a passing round-trip, so this is a named skip, not a bare one."
)

_SEED_SQL = """
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE TABLE alembic_version (version_num varchar(32) NOT NULL PRIMARY KEY);
INSERT INTO alembic_version VALUES ('v0392_seed');
CREATE TABLE media_file (id uuid PRIMARY KEY DEFAULT uuid_generate_v4(), filename text NOT NULL);
INSERT INTO media_file (filename) VALUES ('a.wav'), ('b.wav'), ('c.wav');
"""

# Same shape #599's roundtrip test uses: a later migration (speaker_persona) plus a data
# change made after the backup — the exact drift shape issue #600 measured.
_DRIFT_SQL = """
DELETE FROM media_file WHERE filename = 'b.wav';
INSERT INTO media_file (filename) VALUES ('post_backup.wav');
CREATE TABLE speaker_persona (id uuid PRIMARY KEY DEFAULT uuid_generate_v4(), name text);
UPDATE alembic_version SET version_num = 'v0400_later';
"""


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


def _seed(container: str, dbname: str) -> None:
    _create_db(container, dbname)
    _exec_sql(container, dbname, _SEED_SQL)


def _drift(container: str, dbname: str) -> None:
    _exec_sql(container, dbname, _DRIFT_SQL)


def _backend_image_available() -> bool:
    return _run(["docker", "image", "inspect", _BACKEND_IMAGE]).returncode == 0


def _backend_image_has_gpg() -> bool:
    """False for the CURRENT opentranscribe-backend:latest — see issue #604.

    Measured while building this file: backend_service.run_pg_dump shells out to `gpg` when
    `backup.encrypt` is on, but Dockerfile.prod installs `postgresql-client` (pg_dump/pg_restore)
    and never `gnupg` — so `gpg` is not on PATH in the real production image, and a real
    encrypted scheduled backup fails with `FileNotFoundError: gpg` in production today. That is
    a genuine, separate, already-filed bug (#604), not a gap in this test: skip citing it rather
    than either faking an environment gpg wouldn't have, or silently omitting encrypted coverage.
    """
    if not _backend_image_available():
        return False
    return (
        _run(
            ["docker", "run", "--rm", "--entrypoint", "gpg", _BACKEND_IMAGE, "--version"]
        ).returncode
        == 0
    )


_GPG_SKIP_REASON = (
    f"{_BACKEND_IMAGE} does not have gpg on PATH (issue #604 — Dockerfile.prod installs "
    "postgresql-client but never gnupg, so backup.encrypt fails in production today). Rebuild "
    "the image after #604 lands, then re-run this test."
)


# ---------------------------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def pg_container() -> Iterator[str]:
    """A throwaway, network-isolated Postgres container for the Tier 2 (docker-exec-only) tests.

    Mirrors test_opentr_restore_roundtrip.py's fixture of the same name exactly (including
    the two-consecutive-readiness-check rationale) — duplicated rather than imported because
    pytest fixtures are resolved by name within the importing module's own namespace, and a
    cross-file fixture import silently does not register as a fixture here.
    """
    name = f"ot-restore600-pg-{uuid.uuid4().hex[:12]}"
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
        yield name
    finally:
        _run(["docker", "rm", "-f", name])


@pytest.fixture
def networked_pg(tmp_path: Path) -> Iterator[tuple[str, str, str]]:
    """(network, pg_container, password) for Tier 1: a Postgres reachable from a SECOND
    throwaway container over a private bridge network with no published ports — never from
    the host, and never able to resolve a live compose service name.
    """
    net_name = f"ot-restore600-net-{uuid.uuid4().hex[:12]}"
    pg_name = f"ot-restore600-netpg-{uuid.uuid4().hex[:12]}"
    image = _postgres_image_tag()
    password = uuid.uuid4().hex

    created_net = _run(["docker", "network", "create", net_name])
    assert created_net.returncode == 0, f"failed to create test network: {created_net.stderr}"
    try:
        started = _run(
            [
                "docker",
                "run",
                "-d",
                "--network",
                net_name,
                "--name",
                pg_name,
                "-e",
                f"POSTGRES_PASSWORD={password}",
                image,
            ]
        )
        assert started.returncode == 0, f"failed to start postgres: {started.stderr}"
        try:
            _wait_ready(pg_name)
            yield net_name, pg_name, password
        finally:
            _run(["docker", "rm", "-f", pg_name])
    finally:
        _run(["docker", "network", "rm", net_name])


def _pg_dump_custom_bytes(container: str, dbname: str) -> bytes:
    """Tier 2's cheap artifact producer — SAME format flags as run_pg_dump, run locally
    inside the container via peer trust instead of a --dbname URL (see module docstring).
    """
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            "docker",
            "exec",
            container,
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--no-acl",
            "-U",
            _DB_USER,
            dbname,
        ],
        capture_output=True,
    )
    assert result.returncode == 0, f"pg_dump --format=custom failed: {result.stderr!r}"
    return result.stdout


def _run_pg_dump_in_backend_container(
    network: str,
    pg_name: str,
    password: str,
    dbname: str,
    out_dir: Path,
    dump_filename: str,
    *,
    encrypt: bool = False,
    passphrase_file: str = "",
) -> None:
    """Run the REAL backup_service.run_pg_dump inside a throwaway backend-image container."""
    database_url = f"postgresql://{_DB_USER}:{password}@{pg_name}:5432/{dbname}"
    code = (
        "from pathlib import Path; "
        "from app.services import backup_service as b; "
        f"b.run_pg_dump(Path('/out/{dump_filename}'), encrypt={encrypt!r}, "
        f"passphrase_file={passphrase_file!r}, database_url={database_url!r})"
    )
    result = _run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            network,
            "-v",
            f"{_REPO_ROOT}/backend:/app",
            "-v",
            f"{out_dir}:/out",
            _BACKEND_IMAGE,
            "python",
            "-c",
            code,
        ]
    )
    assert result.returncode == 0, (
        f"run_pg_dump inside {_BACKEND_IMAGE} failed: {result.stderr}\n{result.stdout}"
    )


# ---------------------------------------------------------------------------------------------
# Tier 1 — the trustworthy-evidence test.
# ---------------------------------------------------------------------------------------------


@pytest.mark.skipif(not _backend_image_available(), reason=_BACKEND_IMAGE_SKIP_REASON)
def test_the_real_run_pg_dump_artifact_restores_exactly(
    networked_pg: tuple[str, str, str], tmp_path: Path
) -> None:
    network, pg_name, password = networked_pg
    dbname = "otrestore600_real"
    _seed(pg_name, dbname)

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    dump_filename = "real.dump"
    _run_pg_dump_in_backend_container(network, pg_name, password, dbname, out_dir, dump_filename)

    dump_path = out_dir / dump_filename
    assert dump_path.is_file(), "run_pg_dump did not produce an artifact"
    with dump_path.open("rb") as fh:
        magic = fh.read(5)
    assert magic == b"PGDMP", (
        f"the artifact from the REAL run_pg_dump does not start with PGDMP: {magic!r} — "
        "the format claim (backup_service.PG_DUMP_FORMAT == 'custom') is not what actually ran"
    )

    _drift(pg_name, dbname)
    assert set(_media_filenames(pg_name, dbname)) == {"a.wav", "c.wav", "post_backup.wav"}

    exec_prefix = f"docker exec -i {pg_name}"

    drop_result = _call_common_fn("pg_drop_and_recreate_database", exec_prefix, _DB_USER, dbname)
    assert drop_result.returncode == 0, (
        f"pg_drop_and_recreate_database failed: {drop_result.stderr}"
    )

    replay_result = _call_common_fn(
        "pg_replay_custom_dump", exec_prefix, _DB_USER, dbname, str(dump_path)
    )
    assert replay_result.returncode == 0, f"pg_replay_custom_dump failed: {replay_result.stderr}"

    verify_result = _call_common_fn(
        "pg_verify_custom_restore", exec_prefix, _DB_USER, dbname, str(dump_path)
    )
    assert verify_result.returncode == 0, (
        f"pg_verify_custom_restore reported a mismatch: {verify_result.stdout}"
    )

    assert set(_media_filenames(pg_name, dbname)) == {"a.wav", "b.wav", "c.wav"}
    version_rows = _query(pg_name, dbname, "SELECT version_num FROM alembic_version;").splitlines()
    assert version_rows == ["v0392_seed"]
    speaker_persona_exists = _query(
        pg_name,
        dbname,
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = 'speaker_persona';",
    )
    assert speaker_persona_exists == "0"


# ---------------------------------------------------------------------------------------------
# Tier 2 — the cheap tests (same argv, no per-test container spawn).
# ---------------------------------------------------------------------------------------------


def test_documented_pg_restore_into_a_populated_database_corrupts_alembic_version(
    pg_container: str, tmp_path: Path
) -> None:
    """Pin the bug as a property of pg_restore itself (the "fix the doc's missing redirect"
    shape), the way #599 pinned psql's silent-success bug. Assert on STATE, never exit code:
    pg_restore returns 1 here (unlike psql's 0), but has ALREADY committed the partial damage
    by the time it does — the nonzero exit is not the safety property it looks like (measured).
    """
    container = pg_container
    dbname = "otrestore600_docbug"
    _seed(container, dbname)

    dump_bytes = _pg_dump_custom_bytes(container, dbname)
    dump_path = tmp_path / "backup.dump"
    dump_path.write_bytes(dump_bytes)

    _drift(container, dbname)

    # The literally-documented (pre-fix) command, redirect added — backup-restore.md's shape
    # once the "could not open input file" bug is naively patched.
    with dump_path.open("rb") as dump_fh:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["docker", "exec", "-i", container, "pg_restore", "-U", _DB_USER, "-d", dbname],
            stdin=dump_fh,
            capture_output=True,
        )

    assert result.returncode != 0, (
        "this test pins pg_restore exiting nonzero on a populated target — if pg_restore's "
        "own behavior changed to exit 0, the test's premise needs re-checking, not the app"
    )

    filenames = set(_media_filenames(container, dbname))
    assert filenames == {"a.wav", "c.wav", "post_backup.wav"}, (
        f"expected the drifted (unrestored) data to SURVIVE the failed bare pg_restore, got {filenames}"
    )

    speaker_persona_exists = _query(
        container,
        dbname,
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = 'speaker_persona';",
    )
    assert speaker_persona_exists == "1", (
        "the later-migration table (created during drift) must survive too — a bare pg_restore "
        "has no --clean and only touches objects it knows about"
    )

    version_rows = _query(
        container, dbname, "SELECT version_num FROM alembic_version ORDER BY version_num;"
    )
    row_count = len([line for line in version_rows.splitlines() if line])
    assert row_count == 2, (
        "this is #600's silent-corruption half: the backup's alembic_version row does not "
        f"collide on primary key with the drifted row, so BOTH survive despite the nonzero "
        f"exit. Got {row_count} row(s): {version_rows!r}"
    )


def test_custom_format_restore_flow_replaces_the_database_exactly(
    pg_container: str, tmp_path: Path
) -> None:
    container = pg_container
    dbname = "otrestore600_fixed"
    _seed(container, dbname)

    dump_bytes = _pg_dump_custom_bytes(container, dbname)
    dump_path = tmp_path / "backup.dump"
    dump_path.write_bytes(dump_bytes)

    _drift(container, dbname)
    assert set(_media_filenames(container, dbname)) == {"a.wav", "c.wav", "post_backup.wav"}

    exec_prefix = f"docker exec -i {container}"

    drop_result = _call_common_fn("pg_drop_and_recreate_database", exec_prefix, _DB_USER, dbname)
    assert drop_result.returncode == 0, (
        f"pg_drop_and_recreate_database failed: {drop_result.stderr}"
    )

    replay_result = _call_common_fn(
        "pg_replay_custom_dump", exec_prefix, _DB_USER, dbname, str(dump_path)
    )
    assert replay_result.returncode == 0, f"pg_replay_custom_dump failed: {replay_result.stderr}"

    verify_result = _call_common_fn(
        "pg_verify_custom_restore", exec_prefix, _DB_USER, dbname, str(dump_path)
    )
    assert verify_result.returncode == 0, (
        f"pg_verify_custom_restore reported a mismatch: {verify_result.stdout}"
    )

    assert set(_media_filenames(container, dbname)) == {"a.wav", "b.wav", "c.wav"}

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
    assert version_rows == ["v0392_seed"]

    extension_present = _query(
        container, dbname, "SELECT extname FROM pg_extension WHERE extname = 'uuid-ossp';"
    )
    assert extension_present == "uuid-ossp"


def test_a_failed_custom_replay_rolls_back_whole(pg_container: str, tmp_path: Path) -> None:
    """A plausible real corruption: an interrupted S3 download leaves a truncated archive.
    Assert pg_replay_custom_dump fails AND the public table count is zero — --single-transaction
    must not leave a hybrid schema, matching #599's plain-SQL rollback guarantee.
    """
    container = pg_container
    dbname = "otrestore600_rollback"
    _seed(container, dbname)

    dump_bytes = _pg_dump_custom_bytes(container, dbname)
    dump_path = tmp_path / "backup.dump"
    dump_path.write_bytes(dump_bytes)

    truncated_path = tmp_path / "truncated.dump"
    # Cut the archive in half — a plausible interrupted-transfer shape. Long enough to pass
    # pg_restore's own header sniff and start actually replaying before it hits EOF.
    truncated_path.write_bytes(dump_bytes[: len(dump_bytes) // 2])

    exec_prefix = f"docker exec -i {container}"

    drop_result = _call_common_fn("pg_drop_and_recreate_database", exec_prefix, _DB_USER, dbname)
    assert drop_result.returncode == 0, (
        f"pg_drop_and_recreate_database failed: {drop_result.stderr}"
    )

    replay_result = _call_common_fn(
        "pg_replay_custom_dump", exec_prefix, _DB_USER, dbname, str(truncated_path)
    )
    assert replay_result.returncode != 0, (
        "replaying a truncated archive must fail, not silently succeed"
    )

    table_count = _query(
        container,
        dbname,
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';",
    )
    assert table_count == "0", (
        "a failed pg_restore replay must roll back the WHOLE transaction "
        f"(--single-transaction), leaving zero tables — got {table_count}"
    )


def test_pg_verify_custom_restore_fails_on_a_mismatched_database(
    pg_container: str, tmp_path: Path
) -> None:
    """The must-fire case for the verifier itself (issue #431's lesson): without this, a
    verifier that matches nothing reports a clean restore forever.
    """
    container = pg_container
    dbname = "otrestore600_verify_mismatch"
    _seed(container, dbname)

    dump_bytes = _pg_dump_custom_bytes(container, dbname)
    dump_path = tmp_path / "backup.dump"
    dump_path.write_bytes(dump_bytes)

    exec_prefix = f"docker exec -i {container}"

    drop_result = _call_common_fn("pg_drop_and_recreate_database", exec_prefix, _DB_USER, dbname)
    assert drop_result.returncode == 0, (
        f"pg_drop_and_recreate_database failed: {drop_result.stderr}"
    )

    replay_result = _call_common_fn(
        "pg_replay_custom_dump", exec_prefix, _DB_USER, dbname, str(dump_path)
    )
    assert replay_result.returncode == 0, f"pg_replay_custom_dump failed: {replay_result.stderr}"

    # Mutate the freshly-restored database so it no longer matches the backup it came from.
    _exec_sql(container, dbname, "DELETE FROM alembic_version;")

    verify_result = _call_common_fn(
        "pg_verify_custom_restore", exec_prefix, _DB_USER, dbname, str(dump_path)
    )
    assert verify_result.returncode != 0, (
        "pg_verify_custom_restore must FAIL when the live database no longer matches the "
        "backup's alembic head — a verifier that always passes is exactly the #431 failure mode"
    )


@pytest.mark.skipif(not _backend_image_available(), reason=_BACKEND_IMAGE_SKIP_REASON)
@pytest.mark.skipif(not _backend_image_has_gpg(), reason=_GPG_SKIP_REASON)
def test_gpg_encrypted_scheduled_artifact_round_trips(
    networked_pg: tuple[str, str, str], tmp_path: Path
) -> None:
    """The encrypted variant of the REAL feature (backup.encrypt), which nothing tested
    before this. Also asserts the plaintext .dump was unlinked (run_pg_dump:723) — a claim
    previously made only by a mocked unit test.
    """
    network, pg_name, password = networked_pg
    dbname = "otrestore600_gpg"
    _seed(pg_name, dbname)

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    # Must live UNDER out_dir — that's the only directory bind-mounted (as /out) into the
    # throwaway backend container, so a passphrase file written outside it would be invisible
    # to run_pg_dump no matter how correct the in-container path string looks.
    passphrase_file_host = out_dir / "passphrase.txt"
    passphrase_file_host.write_text("correct horse battery staple\n", encoding="utf-8")

    dump_filename = "encrypted.dump"
    _run_pg_dump_in_backend_container(
        network,
        pg_name,
        password,
        dbname,
        out_dir,
        dump_filename,
        encrypt=True,
        passphrase_file="/out/passphrase.txt",
    )

    plaintext_path = out_dir / dump_filename
    gpg_path = out_dir / f"{dump_filename}.gpg"
    assert not plaintext_path.exists(), (
        "run_pg_dump must unlink the plaintext .dump after encrypting — leaving it defeats "
        "the whole point of --encrypt"
    )
    assert gpg_path.is_file(), "expected a .dump.gpg envelope"

    # Decrypt on the host exactly as opentr.sh does (gpg IS present on the host — only
    # pg_dump/pg_restore are missing, per the module docstring).
    if shutil.which("gpg") is None:
        pytest.skip("gpg not available on this host — cannot exercise the decrypt-on-host half")

    decrypted_path = tmp_path / "decrypted.dump"
    decrypt_result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            "gpg",
            "--batch",
            "--yes",
            "--passphrase-file",
            str(passphrase_file_host),
            "--output",
            str(decrypted_path),
            "--decrypt",
            str(gpg_path),
        ],
        capture_output=True,
    )
    assert decrypt_result.returncode == 0, f"gpg decrypt failed: {decrypt_result.stderr!r}"
    with decrypted_path.open("rb") as fh:
        assert fh.read(5) == b"PGDMP"

    _drift(pg_name, dbname)

    exec_prefix = f"docker exec -i {pg_name}"
    drop_result = _call_common_fn("pg_drop_and_recreate_database", exec_prefix, _DB_USER, dbname)
    assert drop_result.returncode == 0, (
        f"pg_drop_and_recreate_database failed: {drop_result.stderr}"
    )

    replay_result = _call_common_fn(
        "pg_replay_custom_dump", exec_prefix, _DB_USER, dbname, str(decrypted_path)
    )
    assert replay_result.returncode == 0, f"pg_replay_custom_dump failed: {replay_result.stderr}"

    verify_result = _call_common_fn(
        "pg_verify_custom_restore", exec_prefix, _DB_USER, dbname, str(decrypted_path)
    )
    assert verify_result.returncode == 0, (
        f"pg_verify_custom_restore reported a mismatch: {verify_result.stdout}"
    )
    assert set(_media_filenames(pg_name, dbname)) == {"a.wav", "b.wav", "c.wav"}
