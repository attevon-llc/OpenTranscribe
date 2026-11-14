"""``run-backend-tests.sh --throwaway-db`` must give any worktree an isolated, migrated, self-cleaning DB.

The mode starts a Postgres container on a developer machine that also hosts the live dev stack
and unrelated GPU services. The failure that matters is not a flaky run, it is a cleanup that
removes somebody else's container, or a database that is not the one the branch's migrations
built. So the REAL script runs inside a throwaway git repo that holds copies of it and its
libs, with stub ``docker`` / ``python3`` / venv interpreters first on PATH. That proves the
control flow (port choice, labels, exported env, teardown on every exit path, blast radius,
exit codes); it does not prove postgres starts. The real-docker run is recorded in the commit
that introduced the engine (``scripts/lib/throwaway-pg.sh``).
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import signal
import subprocess  # runs the checked-in script against stubbed docker  # nosec B404
import sys
from pathlib import Path
from types import ModuleType

import pytest
from fixtures.sigint import assert_signal_is_caught
from fixtures.sigint import with_default_sigint

REPO_ROOT = Path(__file__).resolve().parents[3]
STOCK_PORTS = (5173, 5176, 5178, 5180)
NAME_RE = re.compile(r"ot-testdb-[0-9a-f]{12}")


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


def _stub_interpreter(log: Path, root: Path) -> str:
    """A venv python/pytest stub: logs alembic and pytest calls, dumps pytest's environment."""
    return f"""#!/bin/bash
if [[ "$1" == "-c" ]]; then exit 0; fi
if [[ "$*" == *alembic* ]]; then
  echo "alembic cwd=$PWD" >> "{log}"
  [[ -n "${{STUB_ALEMBIC_FAIL:-}}" ]] && exit 99
  exit 0
fi
echo "pytest $*" >> "{log}"
env > "{root}/pytest-env.txt"
if [[ -n "${{STUB_PYTEST_HANG:-}}" ]]; then
  exec 3<>"$STUB_PYTEST_HANG"  # read-write open of a fifo never blocks; held before announcing
  echo STUB-PYTEST-RUNNING
  read -r _ <&3  # parked until the test has sent its signal and releases us
else
  echo STUB-PYTEST-RUNNING
fi
[[ -n "${{STUB_PYTEST_FAIL:-}}" ]] && exit 7
exit 0
"""


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "scripts" / "lib").mkdir(parents=True)
    for rel in (
        "scripts/run-backend-tests.sh",
        "scripts/lib/throwaway-pg.sh",
        "scripts/lib/stock-port-owners.sh",
    ):
        shutil.copy(REPO_ROOT / rel, root / rel)
    subprocess.run(
        ["git", "init", "-q", str(root)], check=True
    )  # fixed argv; git from PATH on a tmp repo
    (root / "backend").mkdir()
    log = root / "calls.log"
    for tool in ("python", "pytest"):
        _write(root / "backend" / "venv" / "bin" / tool, _stub_interpreter(log, root))
    stubs = root / "stubs"
    # ``docker run`` consumes the next line of $STUB_RUN_FAILURES as its stderr and fails;
    # once exhausted it succeeds. ``exec`` (readiness) fails while $STUB_EXEC_FAIL is set.
    _write(
        stubs / "docker",
        f"""#!/bin/bash
echo "docker $*" >> "{log}"
case "$1" in
  version) echo linux/amd64 ;;
  ps) [[ -n "${{STUB_EXISTING:-}}" ]] && echo "$STUB_EXISTING" ;;
  run)
    n=$(grep -c '^docker run ' "{log}")
    msg=$(sed -n "${{n}}p" "${{STUB_RUN_FAILURES:-/dev/null}}")
    if [[ -n "$msg" ]]; then echo "$msg" >&2; exit 125; fi
    ;;
  exec) [[ -n "${{STUB_EXEC_FAIL:-}}" ]] && exit 1 ;;
esac
exit 0
""",
    )
    _write(
        stubs / "python3",
        '#!/bin/bash\n[[ -n "${STUB_PORT:-}" ]] && { echo "$STUB_PORT"; exit 0; }\n'
        'exec /usr/bin/python3 "$@"\n',
    )
    return root


def _release(fifo: Path) -> None:
    """Let a stub parked on ``fifo`` continue; fails (ENXIO) rather than hangs if nothing waits."""
    fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.write(fd, b"go\n")
    finally:
        os.close(fd)


def _start(root: Path, *args: str, **env: str) -> subprocess.Popen[str]:
    full = {
        **os.environ,
        "PATH": f"{root / 'stubs'}:{os.environ['PATH']}",
        "OT_TEST_OUT_DIR": str(root / "out"),
        "OT_TEST_PG_READY_TIMEOUT": "3",
        **env,
    }
    full.pop("OT_TEST_PYTHON", None)
    return subprocess.Popen(  # fixed argv; bash from PATH, script under tmp_path
        with_default_sigint(["bash", str(root / "scripts" / "run-backend-tests.sh"), *args]),
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


def _run_call(root: Path) -> str:
    return next(c for c in _docker(root) if c.startswith("run "))


def _name(root: Path) -> str:
    match = re.search(rf"--name ({NAME_RE.pattern})\b", _run_call(root))
    assert match, _run_call(root)
    return match.group(1)


def _pytest_env(root: Path) -> dict[str, str]:
    lines = (root / "pytest-env.txt").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def _published_ports(root: Path) -> list[int]:
    return [
        int(m.group(1))
        for c in _docker(root)
        if c.startswith("run ")
        for m in [re.search(r"-p 127\.0\.0\.1:(\d+):5432", c)]
        if m
    ]


# --- the container: what it is, how it is named and labelled ------------------------------------


def test_picks_a_free_nonstock_loopback_port(fake_repo: Path) -> None:
    rc, out = _run(fake_repo, "--throwaway-db", "tests/x.py")
    assert rc == 0, out
    (port,) = _published_ports(fake_repo)
    assert port > 1024
    assert port not in STOCK_PORTS


@pytest.mark.parametrize("port", STOCK_PORTS)
def test_refuses_stock_ports(fake_repo: Path, port: int) -> None:
    rc, out = _run(fake_repo, "--throwaway-db", "tests/x.py", STUB_PORT=str(port))
    assert rc == 3, out
    assert "stock port" in out
    assert not any(c.startswith("run ") for c in _docker(fake_repo))
    assert not any(c.startswith("pytest") for c in _calls(fake_repo))


def test_container_is_named_labelled_isolated_and_on_tmpfs(fake_repo: Path) -> None:
    rc, out = _run(fake_repo, "--throwaway-db", "tests/x.py")
    assert rc == 0, out
    run = _run_call(fake_repo)
    # the sweep's own test label, and the private compose-project namespace
    assert "--label ot-test-artifact=1" in run
    assert "--label org.opentranscribe.purpose=throwaway-test-db" in run
    assert re.search(
        r"--label com\.docker\.compose\.project=opentranscribe-pytest-[0-9a-f]{32}\b", run
    )
    assert re.search(r"--tmpfs /var/lib/postgresql/data:rw,size=\d+[gm]\b", run)
    assert "postgres:17.5-alpine" in run
    assert "--platform linux/amd64" in run
    assert " --rm " in run
    assert "-p 127.0.0.1:" in run


def test_name_and_password_are_random_per_run(fake_repo: Path) -> None:
    seen: set[tuple[str, str]] = set()
    for _ in range(2):
        (fake_repo / "calls.log").unlink(missing_ok=True)
        rc, out = _run(fake_repo, "--throwaway-db", "tests/x.py")
        assert rc == 0, out
        seen.add((_name(fake_repo), _pytest_env(fake_repo)["POSTGRES_PASSWORD"]))
    assert len({n for n, _ in seen}) == 2
    assert len({p for _, p in seen}) == 2
    assert all(len(p) >= 10 for _, p in seen)


def _load_sweep() -> ModuleType:
    path = REPO_ROOT / "scripts" / "lib" / "test_artifact_sweep.py"
    spec = importlib.util.spec_from_file_location("sweep_for_throwaway_db", path)
    assert spec is not None and spec.loader is not None
    sweep = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = sweep
    spec.loader.exec_module(sweep)
    return sweep


def test_the_container_is_one_the_test_data_sweep_recognises(fake_repo: Path) -> None:
    sweep = _load_sweep()
    rc, out = _run(fake_repo, "--throwaway-db", "tests/x.py")
    assert rc == 0, out
    labels = dict(re.findall(r"--label ([\w.-]+)=(\S+)", _run_call(fake_repo)))
    finding = sweep.classify_docker("container", _name(fake_repo), labels, 0, 10**12)
    assert finding.tier == sweep.TIER_B, finding


def test_the_temp_tree_is_one_the_test_data_sweep_recognises(fake_repo: Path) -> None:
    sweep = _load_sweep()
    rc, out = _run(fake_repo, "--throwaway-db", "tests/x.py")
    assert rc == 0, out
    tree = Path(_pytest_env(fake_repo)["DATA_DIR"]).parent.name
    finding = sweep.classify_tmp(tree, os.getuid(), 0, 10**12, os.getuid())
    assert finding.tier == sweep.TIER_A, finding


# --- the database: migrated by THIS checkout, environment points only at it ----------------------


def test_exports_consistent_env_and_migrates_before_pytest(fake_repo: Path) -> None:
    ambient = {
        "DATABASE_URL": "postgresql://live:pw@localhost:5176/opentranscribe",
        "MIGRATION_DATABASE_URL": "postgresql://live:pw@localhost:5176/opentranscribe",
        "POSTGRES_APP_USER": "ot_app",
        "POSTGRES_APP_PASSWORD": "app-pw",
        "POSTGRES_SSLMODE": "require",
    }
    rc, out = _run(fake_repo, "--throwaway-db", "tests/x.py", "-n0", **ambient)
    assert rc == 0, out
    env = _pytest_env(fake_repo)
    port = env["POSTGRES_PORT"]
    assert port.isdigit()
    assert int(port) == _published_ports(fake_repo)[0]
    assert int(port) not in STOCK_PORTS
    assert env["POSTGRES_HOST"] == "127.0.0.1"
    assert env["SKIP_S3"] == "True"
    assert env["SKIP_OPENSEARCH"] == "True"
    assert env["TESTING"] == "True"
    assert env["POSTGRES_USER"] == "postgres"
    assert env["POSTGRES_DB"] == "transcribe_test"
    assert len(env["POSTGRES_PASSWORD"]) >= 10
    # Settings derives the URLs from POSTGRES_*; an exported or inherited URL would leak into
    # every Settings() a unit test builds and could point the run at another database.
    leaked = {var: env[var] for var in ambient if var in env}
    assert leaked == {}, leaked
    for var in ("DATA_DIR", "MODELS_DIR", "TEMP_DIR"):
        assert "ot-throwaway-db." in env[var], (var, env[var])
    calls = [c for c in _calls(fake_repo) if not c.startswith("docker ")]
    assert calls[0] == f"alembic cwd={fake_repo / 'backend'}"
    assert calls[1].startswith("pytest ")
    assert calls[1].split()[1:4] == ["tests/x.py", "-n0", "-p"]


def test_readiness_is_probed_over_tcp_and_needs_two_successes(fake_repo: Path) -> None:
    rc, out = _run(fake_repo, "--throwaway-db", "tests/x.py")
    assert rc == 0, out
    probes = [c for c in _docker(fake_repo) if c.startswith("exec ")]
    assert len(probes) >= 2, probes
    assert all("-h 127.0.0.1" in c for c in probes), probes


def test_flags_are_consumed_not_forwarded_to_pytest(fake_repo: Path) -> None:
    rc, out = _run(fake_repo, "tests/a.py", "--keep", "-k", "foo", "--throwaway-db")
    work = Path(_pytest_env(fake_repo)["DATA_DIR"]).parent
    try:
        assert rc == 0, out
        (pytest_call,) = [c for c in _calls(fake_repo) if c.startswith("pytest ")]
        assert "--throwaway-db" not in pytest_call
        assert "--keep" not in pytest_call
        assert "tests/a.py -k foo" in pytest_call
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --- exit codes ---------------------------------------------------------------------------------


def test_pytest_failure_keeps_its_exit_code_and_still_tears_down(fake_repo: Path) -> None:
    rc, out = _run(fake_repo, "--throwaway-db", "tests/x.py", STUB_PYTEST_FAIL="1")
    assert rc == 7, out
    assert f"rm -f {_name(fake_repo)}" in _docker(fake_repo)


def test_migration_failure_exits_4_runs_no_tests_and_tears_down(fake_repo: Path) -> None:
    rc, out = _run(fake_repo, "--throwaway-db", "tests/x.py", STUB_ALEMBIC_FAIL="1")
    assert rc == 4, out
    assert not any(c.startswith("pytest") for c in _calls(fake_repo))
    assert f"rm -f {_name(fake_repo)}" in _docker(fake_repo)


def test_readiness_timeout_exits_3_and_tears_down(fake_repo: Path) -> None:
    rc, out = _run(
        fake_repo, "--throwaway-db", "tests/x.py", STUB_EXEC_FAIL="1", OT_TEST_PG_READY_TIMEOUT="1"
    )
    assert rc == 3, out
    assert "not ready" in out
    assert not any(c.startswith("pytest") for c in _calls(fake_repo))
    assert f"rm -f {_name(fake_repo)}" in _docker(fake_repo)


def test_keep_without_throwaway_db_is_misuse(fake_repo: Path) -> None:
    rc, out = _run(fake_repo, "--keep", "tests/x.py")
    assert rc == 2, out
    assert _docker(fake_repo) == []


def test_missing_venv_fails_fast_with_3_before_any_docker_call(fake_repo: Path) -> None:
    shutil.rmtree(fake_repo / "backend" / "venv")
    rc, out = _run(fake_repo, "--throwaway-db", "tests/x.py")
    assert rc == 3, out
    assert _docker(fake_repo) == []


def test_a_worktree_without_a_venv_borrows_the_main_checkouts(fake_repo: Path) -> None:
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(fake_repo)]
    subprocess.run(
        [*git, "commit", "-q", "--allow-empty", "-m", "base"], check=True
    )  # fixed argv; git from PATH on a tmp repo
    wt = fake_repo.parent / "wt"
    subprocess.run(
        [*git, "worktree", "add", "-q", str(wt), "-b", "wt-branch"], check=True
    )  # fixed argv; git from PATH on a tmp repo
    for rel in (
        "scripts/run-backend-tests.sh",
        "scripts/lib/throwaway-pg.sh",
        "scripts/lib/stock-port-owners.sh",
    ):
        (wt / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / rel, wt / rel)
    (wt / "backend").mkdir()
    shutil.copytree(fake_repo / "stubs", wt / "stubs")
    rc, out = _run(wt, "--throwaway-db", "tests/x.py")
    assert rc == 0, out
    assert "borrowing the main checkout's" in out
    # alembic ran from the WORKTREE's backend dir, with the main checkout's interpreter
    assert f"alembic cwd={wt / 'backend'}" in _calls(fake_repo)


# --- cleanup on every exit path -----------------------------------------------------------------


@pytest.mark.parametrize(("sig", "code"), [(signal.SIGINT, 130), (signal.SIGTERM, 143)])
def test_interrupt_tears_down_exactly_once(fake_repo: Path, sig: int, code: int) -> None:
    hold = fake_repo / "hold.fifo"
    os.mkfifo(hold)
    proc = _start(fake_repo, "--throwaway-db", "tests/x.py", STUB_PYTEST_HANG=str(hold))
    try:
        assert proc.stdout is not None
        seen = []
        for line in proc.stdout:
            seen.append(line)
            if "STUB-PYTEST-RUNNING" in line:
                break
        else:
            pytest.fail("stub pytest never started:\n" + "".join(seen))
        assert_signal_is_caught(proc.pid, sig)
        proc.send_signal(sig)
        # bash runs its trap only once the foreground stub returns, so the stub is released
        # AFTER the signal is pending: the interrupt cannot lose a race with a finished stub.
        _release(hold)
        rest, _ = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=30)
    assert proc.returncode == code, "".join(seen) + rest
    assert _docker(fake_repo).count(f"rm -f {_name(fake_repo)}") == 1


def test_temp_dir_is_removed_by_default(fake_repo: Path) -> None:
    rc, out = _run(fake_repo, "--throwaway-db", "tests/x.py")
    assert rc == 0, out
    assert not Path(_pytest_env(fake_repo)["DATA_DIR"]).exists()
    assert f"rm -f {_name(fake_repo)}" in _docker(fake_repo)


def test_keep_leaves_container_and_temp_dir_and_says_how_to_remove(fake_repo: Path) -> None:
    rc, out = _run(fake_repo, "--throwaway-db", "--keep", "tests/x.py")
    work = Path(_pytest_env(fake_repo)["DATA_DIR"]).parent
    try:
        assert rc == 0, out
        assert work.is_dir()
        assert f"docker rm -f {_name(fake_repo)}" in out
        assert not any(c.startswith("rm -f") for c in _docker(fake_repo))
    finally:
        shutil.rmtree(work, ignore_errors=True)


@pytest.mark.parametrize("mode", ["--summary", "--failures", "--log"])
def test_report_modes_start_no_container(fake_repo: Path, mode: str) -> None:
    _run(fake_repo, "--throwaway-db", mode)
    assert _docker(fake_repo) == []


# --- blast radius -------------------------------------------------------------------------------


def test_only_ever_touches_its_own_container(fake_repo: Path) -> None:
    rc, out = _run(fake_repo, "--throwaway-db", "tests/x.py", STUB_PYTEST_FAIL="1")
    assert rc == 7, out
    name = _name(fake_repo)
    for call in _docker(fake_repo):
        verb = call.split()[0]
        assert verb in {"version", "ps", "container", "run", "exec", "rm", "logs"}, call
        if verb == "version":
            continue
        if verb == "container":
            # stale-reaper listing: read-only, scoped to this lib's two labels
            assert call.startswith("container ls -a --filter label=ot-test-artifact=1"), call
            assert "label=org.opentranscribe.purpose=throwaway-test-db" in call, call
            continue
        if verb == "ps":
            # the pre-existing-container check: anchored, so a prefix cannot select others
            assert f"name=^{name}$" in call, call
            continue
        assert re.search(rf"(^|\s){name}(\s|$)", call), call
        if verb == "rm":
            assert call == f"rm -f {name}", call


def test_an_existing_container_of_that_name_is_refused_and_never_removed(fake_repo: Path) -> None:
    rc, out = _run(fake_repo, "--throwaway-db", "tests/x.py", STUB_EXISTING="somebody-elses")
    assert rc == 3, out
    assert "already exists" in out
    assert not any(c.startswith(("run ", "rm ")) for c in _docker(fake_repo))


def test_a_name_conflict_on_run_is_final_and_does_not_remove_the_other_container(
    fake_repo: Path,
) -> None:
    failures = fake_repo / "failures"
    failures.write_text('Conflict. The container name "/x" is already in use by container "abc"\n')
    rc, out = _run(fake_repo, "--throwaway-db", "tests/x.py", STUB_RUN_FAILURES=str(failures))
    assert rc == 3, out
    assert not any(c.startswith("rm ") for c in _docker(fake_repo)), _docker(fake_repo)


# --- retries ------------------------------------------------------------------------------------


def test_a_lost_port_race_is_retried_on_another_port(fake_repo: Path) -> None:
    failures = fake_repo / "failures"
    failures.write_text("Bind for 127.0.0.1:55432 failed: port is already allocated\n")
    rc, out = _run(fake_repo, "--throwaway-db", "tests/x.py", STUB_RUN_FAILURES=str(failures))
    assert rc == 0, out
    assert len(_published_ports(fake_repo)) == 2
    assert _pytest_env(fake_repo)["POSTGRES_PORT"] == str(_published_ports(fake_repo)[-1])


def test_a_bind_failure_is_retried_a_bounded_number_of_times(fake_repo: Path) -> None:
    failures = fake_repo / "failures"
    failures.write_text("listen tcp4: bind: address already in use\n" * 50)
    rc, out = _run(fake_repo, "--throwaway-db", "tests/x.py", STUB_RUN_FAILURES=str(failures))
    assert rc == 3, out
    assert len(_published_ports(fake_repo)) == 5
    assert "address already in use" in out


def test_any_other_docker_run_failure_is_not_retried(fake_repo: Path) -> None:
    failures = fake_repo / "failures"
    failures.write_text("no such image: postgres\n")
    rc, out = _run(fake_repo, "--throwaway-db", "tests/x.py", STUB_RUN_FAILURES=str(failures))
    assert rc == 3, out
    assert len(_published_ports(fake_repo)) == 1
    assert "no such image" in out
