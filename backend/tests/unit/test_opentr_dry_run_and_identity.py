"""opentr.sh: ``--fresh --dry-run`` writes nothing, project identity is derived, no .env litter.

* ``start dev --fresh <name> --dry-run`` used to rewrite ``.fresh/<name>.offset``, ``.aux`` and the
  overlay, clobbering a running deployment's recorded port offset; ``--nas``/``--with-watch``/
  ``--with-backup`` also created (and chowned) host directories during the preview.
* ``stop_all_containers`` and the bench volume prefix hard-coded the project ``transcribe-app``.
* The suite itself linked the main checkout's ``.env`` into the worktree it ran in.

opentr.sh runs from a scratch directory with a fake ``docker`` on PATH.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit._shell_extract import REPO_ROOT
from tests.unit._shell_extract import extract_function

pytestmark = pytest.mark.unit

OPENTR = REPO_ROOT / "opentr.sh"


def _scratch(tmp_path: Path, name: str = "proj") -> tuple[Path, dict[str, str]]:
    root = tmp_path / name
    root.mkdir()
    (root / "scripts").symlink_to(REPO_ROOT / "scripts")
    for f in REPO_ROOT.glob("docker-compose*.yml"):
        shutil.copy(f, root / f.name)
    shutil.copy(OPENTR, root / "opentr.sh")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "docker"
    fake.write_text('#!/bin/bash\necho "$@" >> "$DOCKER_LOG"\nexit 0\n')
    fake.chmod(0o755)
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "DOCKER_LOG": str(tmp_path / "docker.log"),
        "OPENTR_NO_ENV_LINK": "1",
    }
    return root, env


def _snapshot(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): p.read_text() if p.is_file() else "<dir>"
        for p in sorted(root.rglob("*"))
        if "scripts" not in p.relative_to(root).parts[:1]
    }


def test_fresh_dry_run_writes_nothing_and_keeps_recorded_state(tmp_path: Path):
    root, env = _scratch(tmp_path)
    fresh = root / ".fresh"
    fresh.mkdir()
    (fresh / "t1.offset").write_text("7\n")
    (fresh / "t1.aux").write_text("docker-compose.keycloak.yml\n")
    (fresh / "t1.yml").write_text("# live overlay\n")
    before = _snapshot(root)

    r = subprocess.run(
        [
            "bash",
            "opentr.sh",
            "start",
            "dev",
            "--fresh",
            "t1",
            "--port-offset",
            "31000",
            "--with-mock-llm",
            "--no-bindmount",
            "--dry-run",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert r.returncode == 0, r.stdout + r.stderr
    assert _snapshot(root) == before, "a --dry-run changed files in the checkout"
    assert "would write" in r.stdout and "t1.yml" in r.stdout
    assert "DRY RUN" in r.stdout


def test_fresh_dry_run_on_a_new_name_creates_no_fresh_dir(tmp_path: Path):
    root, env = _scratch(tmp_path)
    r = subprocess.run(
        [
            "bash",
            "opentr.sh",
            "start",
            "dev",
            "--fresh",
            "brandnew",
            "--port-offset",
            "31000",
            "--dry-run",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (root / ".fresh").exists()


def test_dry_run_does_not_create_watch_backup_or_nas_directories(tmp_path: Path):
    root, env = _scratch(tmp_path)
    env.update(
        WATCH_HOST_PATH=str(tmp_path / "w" / "watch"),
        BACKUP_HOST_PATH=str(tmp_path / "b" / "bk"),
        BACKUP_MIRROR_HOST_PATH=str(tmp_path / "b" / "mir"),
        MINIO_NAS_PATH=str(tmp_path / "n" / "minio"),
        POSTGRES_DATA_PATH=str(tmp_path / "n" / "pg"),
        OPENSEARCH_DATA_PATH=str(tmp_path / "n" / "os"),
    )
    r = subprocess.run(
        [
            "bash",
            "opentr.sh",
            "start",
            "dev",
            "--with-watch",
            "--with-backup",
            "--nas",
            "--dry-run",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    for d in ("w", "b", "n"):
        assert not (tmp_path / d).exists(), f"--dry-run created {d}/"


def _stop_script(root_name: str, tmp_path: Path) -> list[str]:
    root = tmp_path / root_name
    root.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "docker"
    fake.write_text('#!/bin/bash\necho "$@" >> "$DOCKER_LOG"\nexit 0\n')
    fake.chmod(0o755)
    src = "\n".join(
        extract_function(OPENTR, n)
        for n in ("ot_compose_project", "ot_drain_gpu_workers_by_container", "stop_all_containers")
    )
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "DOCKER_LOG": str(tmp_path / "d.log"),
        "OT_STOP_GRACE_GPU": "1",
    }
    subprocess.run(
        ["bash", "-c", f"{src}\nstop_all_containers down"],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return (tmp_path / "d.log").read_text().splitlines()


def test_stop_scopes_to_this_checkouts_project_not_a_hardcoded_name(tmp_path: Path):
    log = _stop_script("My_Other.Checkout", tmp_path)
    labels = {
        ln.split("project=")[1].split()[0]
        for ln in log
        if "label=com.docker.compose.project=" in ln
    }
    assert labels == {"opentranscribe", "my_othercheckout"}, labels


def test_bench_volume_prefix_comes_from_the_compose_project():
    text = OPENTR.read_text()
    assert 'BENCH_VOLUME_PREFIX="transcribe-app"' not in text
    assert 'BENCH_VOLUME_PREFIX="$(ot_compose_project)"' in text


def test_suite_exports_the_env_link_opt_out_and_opentr_honours_it(tmp_path: Path):
    assert os.environ.get("OPENTR_NO_ENV_LINK") == "1"
    main = tmp_path / "main"
    main.mkdir()

    def git(*a: str, cwd: Path = main) -> None:
        subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@e.x")
    git("config", "user.name", "t")
    (main / "f").write_text("x")
    git("add", ".")
    git("commit", "-q", "-m", "i")
    (main / ".env").write_text("FIXTURE_ONLY=1\n")
    wt = tmp_path / "wt"
    git("worktree", "add", "-q", str(wt), "-b", "b")
    shutil.copy(OPENTR, wt / "opentr.sh")
    (wt / "scripts").symlink_to(REPO_ROOT / "scripts")
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "OPENTR_NO_ENV_LINK": "1"}
    subprocess.run(["bash", "opentr.sh", "help"], cwd=wt, env=env, capture_output=True, text=True)
    assert not (wt / ".env").exists() and not (wt / ".env").is_symlink()
