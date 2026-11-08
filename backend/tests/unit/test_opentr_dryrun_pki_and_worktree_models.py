"""Two `opentr.sh` host-side contracts (finish-list A13).

* `--with-pki --dry-run` must write nothing: `add_pki_overlay` used to run
  `scripts/pki/generate-test-env.sh` (which writes gitignored files) even for a preview.
* A relative `MODEL_CACHE_DIR` (`./models`, the `.env.example` default) resolves against
  the project dir, so in a git worktree it bind-mounted an empty directory. opentr.sh now
  resolves it with `scripts/lib/model-cache-dir.sh` (the documented contract: relative
  values resolve against the MAIN checkout) right after sourcing `.env`.

Both drive the REAL shell source via subprocess bash, not a model of it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENTR = REPO_ROOT / "opentr.sh"
MODEL_LIB = REPO_ROOT / "scripts" / "lib" / "model-cache-dir.sh"
GIT_ENV = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": "/nonexistent"}


def _function_body(text: str, name: str) -> str:
    start = text.index(f"\n{name}() {{")
    return text[start : text.index("\n}\n", start) + 3]


def _run(script: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script], cwd=cwd, env=env, capture_output=True, text=True, check=False
    )


def _pki_run(tmp_path: Path, dry_run: str) -> Path:
    scripts = tmp_path / "scripts" / "pki"
    scripts.mkdir(parents=True)
    marker = tmp_path / "generated"
    gen = scripts / "generate-test-env.sh"
    gen.write_text(
        f"#!/bin/bash\ntouch {marker}\nmkdir -p {tmp_path}/scripts/pki/test-certs\n"
        f": > {tmp_path}/scripts/pki/test-certs/pki-test.env\n"
    )
    gen.chmod(0o755)
    (tmp_path / "docker-compose.pki-dev.yml").write_text("services: {}\n")
    body = _function_body(OPENTR.read_text(encoding="utf-8"), "add_pki_overlay")
    script = (
        f'WITH_PKI_FLAG=--with-pki ENVIRONMENT=dev COMPOSE_FILES="" DRY_RUN_FLAG="{dry_run}"\n'
        f'{body}\nadd_pki_overlay\necho "$COMPOSE_FILES"\n'
    )
    result = _run(script, tmp_path, {"PATH": "/usr/bin:/bin"})
    assert result.returncode == 0, result.stderr
    assert "docker-compose.pki-dev.yml" in result.stdout, "the preview lost the PKI overlay"
    return marker


def test_pki_dry_run_does_not_generate(tmp_path: Path) -> None:
    assert not _pki_run(tmp_path, "--dry-run").exists()


def test_pki_real_run_still_generates(tmp_path: Path) -> None:
    assert _pki_run(tmp_path, "").exists()


def _worktree_pair(tmp_path: Path) -> tuple[Path, Path]:
    main, wt = tmp_path / "main", tmp_path / "wt"
    main.mkdir()
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    for cmd in (
        [*git, "init", "-q", "-b", "m"],
        [*git, "commit", "-q", "--allow-empty", "-m", "i"],
        [*git, "worktree", "add", "-q", str(wt), "-b", "w"],
    ):
        subprocess.run(cmd, cwd=main, env=GIT_ENV, check=True, capture_output=True)
    return main, wt


def _opentr_env_block() -> str:
    """The REAL lines of opentr.sh that source .env and then resolve MODEL_CACHE_DIR."""
    text = OPENTR.read_text(encoding="utf-8")
    start = text.index("# Load environment variables from .env if present")
    return text[start : text.index("\nfi\n", start) + 4]


def _resolve(cwd: Path, env_value: str) -> str:
    (cwd / ".env").write_text(f"MODEL_CACHE_DIR={env_value}\n")
    script = f'source {MODEL_LIB}\n{_opentr_env_block()}\necho "$MODEL_CACHE_DIR"\n'
    result = _run(script, cwd, dict(GIT_ENV))
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_worktree_resolves_relative_models_against_main_checkout(tmp_path: Path) -> None:
    main, wt = _worktree_pair(tmp_path)
    assert _resolve(wt, "./models") == str(main.resolve() / "models")


def test_absolute_path_is_never_overridden(tmp_path: Path) -> None:
    _, wt = _worktree_pair(tmp_path)
    assert _resolve(wt, "/data/models") == "/data/models"


def test_main_checkout_resolves_to_its_own_models(tmp_path: Path) -> None:
    main, _ = _worktree_pair(tmp_path)
    assert _resolve(main, "./models") == str(main.resolve() / "models")
