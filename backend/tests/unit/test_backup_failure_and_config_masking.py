"""Two operator-facing failure/secret behaviours of the shipped shell scripts.

* ``backup_database`` redirected ``pg_dump`` into ``./backups/<file>.sql``; when pg_dump failed
  it reported "Backup failed" but left the truncated file in place, where it looks like a
  restore point. The encrypted branch already removed its partial output.
* ``opentranscribe.sh config`` printed the first 20 assignments of ``.env`` verbatim, which in
  a generated install are the database, Redis and MinIO passwords.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.unit._shell_extract import COMMON
from tests.unit._shell_extract import MANAGER
from tests.unit._shell_extract import extract_function

pytestmark = pytest.mark.unit


def _bash(script: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script], cwd=cwd, capture_output=True, text=True, check=False
    )


def test_failed_plain_backup_leaves_no_partial_dump(tmp_path):
    script = "\n".join(
        [
            # Emits partial output, then fails, like a pg_dump killed mid-stream.
            'docker() { echo "-- partial dump"; return 1; }',
            extract_function(COMMON, "backup_database"),
            'backup_database "-f x.yml" "./opentranscribe.sh"',
        ]
    )
    result = _bash(script, tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Backup failed" in result.stdout
    assert list((tmp_path / "backups").iterdir()) == []


def test_config_command_masks_credentials_but_shows_the_rest(tmp_path):
    (tmp_path / ".env").write_text(
        "POSTGRES_PASSWORD=hunter2hunter2\n"
        "JWT_SECRET_KEY=abcdef\n"
        "HUGGINGFACE_TOKEN=hf_realtoken\n"
        "MINIO_KMS_SECRET_KEY=opentranscribe-key:AAAA\n"
        "OPENSEARCH_ADMIN_PASSWORD=\n"
        "BACKEND_PORT=5174\n"
    )
    script = "\n".join(
        [
            "BLUE=; NC=",
            "check_environment() { :; }",
            "docker() { return 0; }",
            "apply_deployment_pins() { :; }",
            "get_compose_files() { echo '-f docker-compose.yml'; }",
            extract_function(MANAGER, "cmd_config"),
            "cmd_config",
        ]
    )
    out = _bash(script, tmp_path).stdout
    for secret in ("hunter2hunter2", "abcdef", "hf_realtoken", "AAAA"):
        assert secret not in out
    assert "POSTGRES_PASSWORD=********" in out
    assert "BACKEND_PORT=5174" in out
    assert "OPENSEARCH_ADMIN_PASSWORD=\n" in out
