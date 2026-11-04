"""``scripts/test-in-worktree.sh`` must give any worktree an isolated, migrated, self-cleaning DB.

The REAL script runs inside a throwaway git repo that holds copies of it and its libs, with stub
``docker`` / ``python3`` / venv ``python`` on PATH. That proves the control flow (port choice,
labels, exported env, teardown on every exit path, blast radius); it does not prove postgres
starts. The real-docker run is recorded in the commit that added the script.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
STOCK_PORTS = (5176, 5178, 5180)


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "scripts" / "lib").mkdir(parents=True)
    for rel in (
        "scripts/test-in-worktree.sh",
        "scripts/lib/throwaway-pg.sh",
        "scripts/lib/stock-port-owners.sh",
    ):
        shutil.copy(REPO_ROOT / rel, root / rel)
    subprocess.run(["git", "init", "-q", str(root)], check=True)  # noqa: S603, S607
    (root / "backend").mkdir()
    log = root / "calls.log"
    _write(
        root / "backend" / "venv" / "bin" / "python",
        f"""#!/bin/bash
if [[ "$*" == *alembic* ]]; then
  echo "alembic cwd=$PWD" >> "{log}"
  [[ -n "${{STUB_ALEMBIC_FAIL:-}}" ]] && exit 4
  exit 0
fi
echo "pytest $*" >> "{log}"
env > "{root}/pytest-env.txt"
echo STUB-PYTEST-RUNNING
[[ -n "${{STUB_PYTEST_HANG:-}}" ]] && sleep 2
[[ -n "${{STUB_PYTEST_FAIL:-}}" ]] && exit 7
exit 0
""",
    )
    stubs = root / "stubs"
    _write(stubs / "docker", f'#!/bin/bash\necho "docker $*" >> "{log}"\nexit 0\n')
    _write(
        stubs / "python3",
        '#!/bin/bash\n[[ -n "${STUB_PORT:-}" ]] && { echo "$STUB_PORT"; exit 0; }\n'
        'exec /usr/bin/python3 "$@"\n',
    )
    return root


def _start(root: Path, *args: str, **env: str) -> subprocess.Popen[str]:
    full = {**os.environ, "PATH": f"{root / 'stubs'}:{os.environ['PATH']}", **env}
    return subprocess.Popen(
        ["bash", str(root / "scripts" / "test-in-worktree.sh"), *args],
        env=full,
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _run(root: Path, *args: str, **env: str) -> tuple[int, str]:
    proc = _start(root, *args, **env)
    out, _ = proc.communicate(timeout=60)
    return proc.returncode, out


def _calls(root: Path) -> list[str]:
    log = root / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


def _docker(root: Path) -> list[str]:
    return [c[len("docker ") :] for c in _calls(root) if c.startswith("docker ")]


def _name(root: Path) -> str:
    run = next(c for c in _docker(root) if c.startswith("run "))
    match = re.search(r"--name (ot-wt-test-[0-9a-f]{12})\b", run)
    assert match, run
    return match.group(1)


def _pytest_env(root: Path) -> dict[str, str]:
    lines = (root / "pytest-env.txt").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def test_picks_a_free_nonstock_loopback_port(fake_repo: Path) -> None:
    rc, out = _run(fake_repo, "--", "tests/x.py")
    assert rc == 0, out
    run = next(c for c in _docker(fake_repo) if c.startswith("run "))
    match = re.search(r"-p 127\.0\.0\.1:(\d+):5432", run)
    assert match, run
    port = int(match.group(1))
    assert port > 1024
    assert port not in STOCK_PORTS


def test_container_is_named_labelled_and_on_tmpfs(fake_repo: Path) -> None:
    rc, out = _run(fake_repo, "--", "tests/x.py")
    assert rc == 0, out
    run = next(c for c in _docker(fake_repo) if c.startswith("run "))
    assert "--label ot-test-artifact=1" in run
    assert _name(fake_repo).startswith("ot-wt-test-")
    assert re.search(r"--tmpfs /var/lib/postgresql/data:rw,size=\d+[gm]\b", run)
    assert "postgres:17.5-alpine" in run


def test_exports_consistent_env_and_migrates_before_pytest(fake_repo: Path) -> None:
    rc, out = _run(fake_repo, "--", "tests/x.py", "-n0")
    assert rc == 0, out
    env = _pytest_env(fake_repo)
    port = env["POSTGRES_PORT"]
    assert port.isdigit()
    assert int(port) not in STOCK_PORTS
    assert env["POSTGRES_HOST"] == "127.0.0.1"
    assert env["SKIP_S3"] == "True"
    assert env["SKIP_OPENSEARCH"] == "True"
    assert env["MIGRATION_DATABASE_URL"] == (
        f"postgresql://{env['POSTGRES_USER']}:{env['POSTGRES_PASSWORD']}@127.0.0.1:{port}/{env['POSTGRES_DB']}"
    )
    for var in ("DATA_DIR", "MODELS_DIR", "TEMP_DIR"):
        assert "ot-wt-test." in env[var], (var, env[var])
    calls = [c for c in _calls(fake_repo) if not c.startswith("docker ")]
    assert calls[0] == f"alembic cwd={fake_repo / 'backend'}"
    assert calls[1].startswith("pytest ")
    assert calls[1].endswith("tests/x.py -n0")


@pytest.mark.parametrize(
    ("env", "expected_rc"),
    [({"STUB_PYTEST_FAIL": "1"}, 7), ({"STUB_ALEMBIC_FAIL": "1"}, 4)],
)
def test_failure_still_tears_down_and_keeps_the_exit_code(
    fake_repo: Path, env: dict[str, str], expected_rc: int
) -> None:
    rc, out = _run(fake_repo, "--", "tests/x.py", **env)
    assert rc == expected_rc, out
    assert f"rm -f {_name(fake_repo)}" in _docker(fake_repo)


@pytest.mark.parametrize(("sig", "code"), [(signal.SIGINT, 130), (signal.SIGTERM, 143)])
def test_interrupt_tears_down(fake_repo: Path, sig: int, code: int) -> None:
    proc = _start(fake_repo, "--", "tests/x.py", STUB_PYTEST_HANG="1")
    try:
        assert proc.stdout is not None
        seen = []
        for line in proc.stdout:
            seen.append(line)
            if "STUB-PYTEST-RUNNING" in line:
                break
        else:
            pytest.fail("stub pytest never started:\n" + "".join(seen))
        proc.send_signal(sig)
        rest, _ = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=30)
    assert proc.returncode == code, "".join(seen) + rest
    assert _docker(fake_repo).count(f"rm -f {_name(fake_repo)}") == 1


def test_temp_dir_is_removed_by_default(fake_repo: Path) -> None:
    rc, out = _run(fake_repo, "--", "tests/x.py")
    assert rc == 0, out
    assert not Path(_pytest_env(fake_repo)["DATA_DIR"]).exists()


def test_keep_leaves_container_and_temp_dir_and_says_how_to_remove(fake_repo: Path) -> None:
    rc, out = _run(fake_repo, "--keep", "--", "tests/x.py")
    work = Path(_pytest_env(fake_repo)["DATA_DIR"]).parent
    try:
        assert rc == 0, out
        assert work.is_dir()
        assert f"docker rm -f {_name(fake_repo)}" in out
        assert not any(c.startswith("rm ") for c in _docker(fake_repo))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_only_ever_touches_its_own_container(fake_repo: Path) -> None:
    rc, out = _run(fake_repo, "--", "tests/x.py", STUB_PYTEST_FAIL="1")
    assert rc == 7, out
    name = _name(fake_repo)
    docker_calls = _docker(fake_repo)
    assert docker_calls
    for call in docker_calls:
        assert call.split()[0] in {"run", "exec", "rm", "logs"}, call
        assert re.search(rf"(^|\s){name}(\s|$)", call), call


@pytest.mark.parametrize("port", STOCK_PORTS)
def test_refuses_stock_ports(fake_repo: Path, port: int) -> None:
    rc, out = _run(fake_repo, "--", "tests/x.py", STUB_PORT=str(port))
    assert rc == 3, out
    assert "stock port" in out
    assert not any(c.startswith("run ") for c in _docker(fake_repo))
    assert not any(c.startswith("pytest") for c in _calls(fake_repo))


def test_missing_venv_fails_fast_before_any_docker_call(fake_repo: Path) -> None:
    (fake_repo / "backend" / "venv" / "bin" / "python").unlink()
    rc, out = _run(fake_repo, "--", "tests/x.py")
    assert rc == 3, out
    assert "no backend venv" in out
    assert _docker(fake_repo) == []


@pytest.mark.parametrize("svc", ["minio", "opensearch", "redis", "bogus"])
def test_unprovisioned_services_are_refused(fake_repo: Path, svc: str) -> None:
    rc, out = _run(fake_repo, "--services", svc, "--", "tests/x.py")
    assert rc == 2, out
    assert _docker(fake_repo) == []
