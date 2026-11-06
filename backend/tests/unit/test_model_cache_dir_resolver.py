"""The GPU diarization runner must resolve MODEL_CACHE_DIR against the MAIN checkout.

`docker-compose.benchmark.yml` bind-mounts `${MODEL_CACHE_DIR:-./models}`. Compose resolves a
relative source against the project dir, which from a git worktree is the worktree, so docker
created empty root-owned `models/huggingface` there and every GPU test failed with
PermissionError on the HF cache (observed 2026-11-05).

These tests build a throwaway git repo + real `git worktree`, copy the real script and lib into
it, and put a stub `docker` first on PATH that records the MODEL_CACHE_DIR compose would see.
No docker daemon, no image, no GPU.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "run-diarization-gpu-tests.sh"
LIBS = ("model-cache-dir.sh", "env_reader.py")

pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason="runner script not present")

_DOCKER_STUB = """#!/usr/bin/env bash
# `docker compose ... run` records the env the real compose would interpolate.
echo "$*" >> "$STUB_LOG"
case "$*" in
  *" run "*) echo "MODEL_CACHE_DIR=${MODEL_CACHE_DIR:-<unset>}" >> "$STUB_LOG" ;;
esac
exit 0
"""


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def layout(tmp_path):
    """A main checkout (with the script, fixtures, models/) and a linked worktree of it."""
    main = tmp_path / "main"
    (main / "scripts" / "lib").mkdir(parents=True)
    shutil.copy(SCRIPT, main / "scripts" / SCRIPT.name)
    for name in LIBS:
        shutil.copy(REPO_ROOT / "scripts" / "lib" / name, main / "scripts" / "lib" / name)
    (main / "docker-compose.benchmark.yml").write_text("services: {}\n")
    (main / "benchmark" / "test_audio").mkdir(parents=True)
    (main / "benchmark" / "test_audio" / "0.5h_1899s.wav").write_bytes(b"x")
    (main / "models" / "huggingface" / "hub").mkdir(parents=True)
    _git(tmp_path, "init", "-q", str(main))
    _git(main, "add", "-A", "-f")
    _git(main, "commit", "-q", "-m", "init")
    worktree = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", str(worktree))

    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(_DOCKER_STUB)
    (bindir / "docker").chmod(0o755)
    log = tmp_path / "docker.log"
    log.write_text("")
    return {"main": main, "wt": worktree, "bin": bindir, "log": log}


def _run(layout, cwd_key="wt", env_extra=None, path_prefix=None):
    env = {k: v for k, v in os.environ.items() if k != "MODEL_CACHE_DIR"}
    env["PATH"] = f"{path_prefix or layout['bin']}{os.pathsep}{env['PATH']}"
    env["STUB_LOG"] = str(layout["log"])
    env.update(env_extra or {})
    root = layout[cwd_key]
    return subprocess.run(
        ["bash", str(root / "scripts" / SCRIPT.name), "tests/x.py"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )


def _worktree_has_script(layout):
    """The worktree is a checkout of the committed tree, so it carries the script too."""
    return (layout["wt"] / "scripts" / SCRIPT.name).exists()


def test_worktree_default_resolves_to_main_checkout_models(layout):
    # benchmark/test_audio is committed above only so the worktree sees it; models/ is NOT in
    # git history of the worktree (it is untracked in main), exactly the real-world shape.
    assert _worktree_has_script(layout)
    assert not (layout["wt"] / "models").exists()
    r = _run(layout)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"MODEL_CACHE_DIR={layout['main'] / 'models'}" in layout["log"].read_text()
    assert not (layout["wt"] / "models").exists()


def test_relative_value_is_resolved_against_main_not_worktree(layout):
    shutil.copytree(layout["main"] / "models", layout["main"] / "cache2")
    r = _run(layout, env_extra={"MODEL_CACHE_DIR": "./cache2"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"MODEL_CACHE_DIR={layout['main'] / 'cache2'}" in layout["log"].read_text()


def test_env_file_value_of_main_checkout_is_honoured(layout):
    probe = subprocess.run(["python3", "-c", "import dotenv"], capture_output=True)
    if probe.returncode != 0:
        pytest.skip("system python3 has no python-dotenv")
    shutil.copytree(layout["main"] / "models", layout["main"] / "fromenv")
    (layout["main"] / ".env").write_text("MODEL_CACHE_DIR=./fromenv  # comment\n")
    r = _run(layout)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"MODEL_CACHE_DIR={layout['main'] / 'fromenv'}" in layout["log"].read_text()


def test_missing_dir_is_refused_with_exit_3_and_no_compose_call(layout):
    r = _run(layout, env_extra={"MODEL_CACHE_DIR": str(layout["main"] / "nope")})
    assert r.returncode == 3, r.stdout + r.stderr
    assert "does not exist" in r.stderr
    assert " run " not in layout["log"].read_text()


def test_dir_without_model_caches_is_refused(layout):
    empty = layout["main"] / "emptymodels"
    (empty / "huggingface").mkdir(parents=True)  # the docker-created stub shape
    r = _run(layout, env_extra={"MODEL_CACHE_DIR": str(empty)})
    assert r.returncode == 3, r.stdout + r.stderr
    assert "no model cache" in r.stderr
    assert " run " not in layout["log"].read_text()


def test_dir_owned_by_another_user_is_refused(layout, tmp_path):
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    # Reports every path as owned by root, as docker-created stubs are.
    (fakebin / "stat").write_text("#!/usr/bin/env bash\necho 0\n")
    (fakebin / "stat").chmod(0o755)
    if os.getuid() == 0:
        pytest.skip("running as root: uid 0 is the invoking user")
    r = _run(layout, path_prefix=f"{fakebin}{os.pathsep}{layout['bin']}")
    assert r.returncode == 3, r.stdout + r.stderr
    assert "owned by uid 0" in r.stderr
    assert " run " not in layout["log"].read_text()
