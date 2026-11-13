"""validate-deployments.sh must not depend on operator state, and must still catch a broken chain.

`opentr.sh --dry-run` writes nothing, so the chain it prints names two generated overlays
(`.fresh/<name>.yml`, `scripts/pki/test-certs/pki-test.compose.yml`) that do not exist in a fresh
checkout, and a prod+nginx chain exits unless gitignored TLS files exist. The validator supplies
those itself; any OTHER missing chain file must still fail.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker compose is required to resolve the chain"
)


def _tracked(*pathspecs: str) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", *pathspecs],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [p for p in out.split("\0") if p]


@pytest.fixture(scope="module")
def tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A fresh-checkout stand-in: tracked launcher, compose files and scripts only."""
    root = tmp_path_factory.mktemp("validate-deployments-tree")
    wanted = _tracked(
        "opentr.sh",
        "VERSION",
        ".env.example",
        "docker-compose*.yml",
        "docker",
        "nginx",
        "scripts",
        "docs-site/docs/operations/deployment-configuration.md",
    )
    for rel in wanted:
        src = REPO_ROOT / rel
        if not src.is_file():
            continue
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst, follow_symlinks=False)
    return root


def _run(root: Path, only: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "scripts/validate-deployments.sh", "--only", only],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


@pytest.mark.parametrize("name", ["dev-fresh", "prod-nginx-pki", "prod-pki"])
def test_generated_overlays_do_not_fail_a_fresh_checkout(tree: Path, name: str) -> None:
    assert not (tree / ".fresh").exists()
    result = _run(tree, name)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAIL" not in result.stdout
    assert "PASS" in result.stdout and name in result.stdout


def test_a_genuinely_missing_chain_file_still_fails(tree: Path) -> None:
    (tree / "docker-compose.override.yml").rename(tree / "docker-compose.override.yml.gone")
    try:
        result = _run(tree, "dev-cpu")
    finally:
        (tree / "docker-compose.override.yml.gone").rename(tree / "docker-compose.override.yml")
    assert result.returncode != 0, result.stdout
    assert "chain references a missing file: docker-compose.override.yml" in result.stdout
