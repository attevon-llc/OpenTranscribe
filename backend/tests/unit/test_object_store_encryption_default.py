"""Bundled-MinIO encryption at rest is on unless ``.env`` turns it off.

``docker-compose.yml`` defaults ``MINIO_KMS_AUTO_ENCRYPTION`` to ``on`` and the backend's
startup check reads an unset value the same way, so the two can never disagree about
whether objects are being encrypted. The only way to store objects unencrypted is the
explicit pair ``MINIO_KMS_AUTO_ENCRYPTION=off`` + ``ALLOW_UNENCRYPTED_OBJECT_STORE=true``.

Chains are rendered with the real ``docker compose config`` from a temp copy and an explicit
env file, so an operator's ``.env`` or shell cannot change the answer.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.core.startup_validation import ServiceCredentialSettings

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]

CHAINS: dict[str, tuple[str, ...]] = {
    "dev": ("docker-compose.yml", "docker-compose.override.yml"),
    "prod": ("docker-compose.yml", "docker-compose.prod.yml"),
    "offline": ("docker-compose.yml", "docker-compose.offline.yml"),
}

#: The prod overlay refuses to render without it (test_prod_flower_password_required.py).
BASE_ENV = "FLOWER_PASSWORD=rendered-test-value\n"

needs_docker = pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not present")


def _minio_env(tmp_path: Path, chain: tuple[str, ...], env: str) -> dict:
    for name in chain:
        shutil.copy(REPO_ROOT / name, tmp_path / name)
    (tmp_path / "test.env").write_text(BASE_ENV + env, encoding="utf-8")
    args = ["docker", "compose", "--env-file", "test.env"]
    for name in chain:
        args += ["-f", name]
    result = subprocess.run(
        [*args, "config", "--format", "json"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    environment: dict = json.loads(result.stdout)["services"]["minio"]["environment"]
    return environment


@needs_docker
@pytest.mark.parametrize("label", sorted(CHAINS))
@pytest.mark.parametrize("env", ["", "MINIO_KMS_AUTO_ENCRYPTION=\n"], ids=["unset", "empty"])
def test_minio_encrypts_by_default(tmp_path, label, env):
    assert _minio_env(tmp_path, CHAINS[label], env)["MINIO_KMS_AUTO_ENCRYPTION"] == "on"


@needs_docker
@pytest.mark.parametrize("label", sorted(CHAINS))
def test_explicit_off_is_honoured(tmp_path, label):
    environment = _minio_env(tmp_path, CHAINS[label], "MINIO_KMS_AUTO_ENCRYPTION=off\n")
    assert environment["MINIO_KMS_AUTO_ENCRYPTION"] == "off"


@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
def test_backend_reads_an_unset_switch_as_on(monkeypatch, value):
    """The startup check must see what MinIO does: unset (or empty) is compose's ``on``."""
    if value is None:
        monkeypatch.delenv("MINIO_KMS_AUTO_ENCRYPTION", raising=False)
    else:
        monkeypatch.setenv("MINIO_KMS_AUTO_ENCRYPTION", value)

    assert ServiceCredentialSettings().MINIO_KMS_AUTO_ENCRYPTION == "on"
