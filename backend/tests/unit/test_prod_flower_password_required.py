"""Production compose chains require a Flower password; development keeps its default.

``docker-compose.prod.yml`` interpolates ``FLOWER_PASSWORD`` with ``:?``, so ``docker
compose`` refuses to render (and therefore to start) any production chain while it is unset
or empty. The base file's ``flower`` fallback stays for development only.

Rendered with the real ``docker compose config`` from a temp copy of the compose files and
an explicit env file, so an operator's ``.env`` or shell can neither leak in nor change the
answer.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not present"),
]

REPO_ROOT = Path(__file__).resolve().parents[3]

PROD_CHAINS: dict[str, tuple[str, ...]] = {
    "prod": ("docker-compose.yml", "docker-compose.prod.yml"),
    "prod+nginx": ("docker-compose.yml", "docker-compose.prod.yml", "docker-compose.nginx.yml"),
    "prod+lite": ("docker-compose.yml", "docker-compose.prod.yml", "docker-compose.lite.yml"),
}

#: Test value only; never a credential of any deployment.
SUPPLIED = "rendered-flower-test-value"


def _config(tmp_path: Path, chain: tuple[str, ...], env: str) -> subprocess.CompletedProcess:
    for name in chain:
        shutil.copy(REPO_ROOT / name, tmp_path / name)
    (tmp_path / "test.env").write_text(env, encoding="utf-8")
    args = ["docker", "compose", "--env-file", "test.env"]
    for name in chain:
        args += ["-f", name]
    return subprocess.run(
        [*args, "config", "--format", "json"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize("label", sorted(PROD_CHAINS))
@pytest.mark.parametrize("env", ["", "FLOWER_PASSWORD=\n"], ids=["unset", "empty"])
def test_prod_chain_refuses_to_render_without_a_flower_password(tmp_path, label, env):
    result = _config(tmp_path, PROD_CHAINS[label], env)

    assert result.returncode != 0, f"{label} rendered without FLOWER_PASSWORD"
    assert "FLOWER_PASSWORD must be set" in result.stderr, result.stderr


@pytest.mark.parametrize("label", sorted(PROD_CHAINS))
def test_prod_chain_uses_the_supplied_flower_password(tmp_path, label):
    result = _config(tmp_path, PROD_CHAINS[label], f"FLOWER_PASSWORD={SUPPLIED}\n")

    assert result.returncode == 0, result.stderr
    flower = json.loads(result.stdout)["services"]["flower"]
    assert flower["environment"]["FLOWER_PASSWORD"] == SUPPLIED
    assert f"--basic_auth=admin:{SUPPLIED}" in flower["command"]


def test_development_chain_keeps_its_default(tmp_path):
    result = _config(tmp_path, ("docker-compose.yml", "docker-compose.override.yml"), "")

    assert result.returncode == 0, result.stderr
    flower = json.loads(result.stdout)["services"]["flower"]
    assert flower["environment"]["FLOWER_PASSWORD"] == "flower"
