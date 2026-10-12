"""``scripts/pki/run-pki-e2e-leg.sh`` must stop the PKI prod stack on EVERY exit path (#1222).

The stop used to be the script's last statement, so Ctrl-C/SIGTERM between ``opentr.sh start``
and that line left a prod+nginx+PKI stack bound to the stock ports. A real PKI run needs a
~10 minute prod build, so these tests drive the REAL script inside a throwaway copy of the repo
layout it expects, with stub ``opentr.sh`` / ``setup-test-pki.sh`` / python / ``docker`` / ``curl``.
That proves the trap logic; it does NOT prove anything about the real stack (see
``docs/design/v060_test_hygiene_audit.md``).
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
LEG = REPO_ROOT / "scripts" / "pki" / "run-pki-e2e-leg.sh"


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    dest = root / "scripts" / "pki" / LEG.name
    dest.parent.mkdir(parents=True)
    shutil.copy(LEG, dest)
    log = root / "calls.log"
    _write(
        root / "opentr.sh",
        f'#!/bin/bash\necho "opentr $*" >> "{log}"\n'
        '[[ "$1" == start && -n "${STUB_START_FAIL:-}" ]] && exit 1\nexit 0\n',
    )
    _write(
        root / "scripts" / "pki" / "setup-test-pki.sh",
        f'#!/bin/bash\necho setup >> "{log}"\n[[ -n "${{STUB_SETUP_FAIL:-}}" ]] && exit 1\nexit 0\n',
    )
    _write(
        root / "backend" / "venv" / "bin" / "python",
        f'#!/bin/bash\necho pytest >> "{log}"\necho STUB-PYTEST-RUNNING\n'
        '[[ -n "${STUB_PYTEST_HANG:-}" ]] && sleep 2\n'
        '[[ -n "${STUB_PYTEST_FAIL:-}" ]] && exit 1\nexit 0\n',
    )
    stubs = root / "stubs"
    _write(stubs / "docker", "#!/bin/bash\nexit 0\n")
    _write(stubs / "curl", "#!/bin/bash\nexit 0\n")
    return root


def _run(root: Path, *args: str, **env: str) -> subprocess.Popen[str]:
    full = {**os.environ, "PATH": f"{root / 'stubs'}:{os.environ['PATH']}", **env}
    return subprocess.Popen(
        ["bash", str(root / "scripts" / "pki" / LEG.name), "--yes", *args],
        env=full,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _calls(root: Path) -> list[str]:
    log = root / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


def test_passing_run_stops_the_stack_once(fake_repo: Path) -> None:
    proc = _run(fake_repo)
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 0, out
    assert _calls(fake_repo).count("opentr stop") == 1, _calls(fake_repo)


def test_failing_pytest_still_stops_the_stack_and_keeps_the_gate_code(fake_repo: Path) -> None:
    proc = _run(fake_repo, STUB_PYTEST_FAIL="1")
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 1, out
    assert "opentr stop" in _calls(fake_repo)


def test_failed_start_stops_the_half_started_stack(fake_repo: Path) -> None:
    proc = _run(fake_repo, STUB_START_FAIL="1")
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 1, out
    assert "opentr stop" in _calls(fake_repo)
    assert "pytest" not in _calls(fake_repo)


@pytest.mark.parametrize(("sig", "code"), [(signal.SIGINT, 130), (signal.SIGTERM, 143)])
def test_interrupt_during_pytest_stops_the_stack(fake_repo: Path, sig: int, code: int) -> None:
    proc = _run(fake_repo, STUB_PYTEST_HANG="1")
    try:
        assert proc.stdout is not None
        seen = []
        for line in proc.stdout:  # blocks until the stub announces itself; no polling
            seen.append(line)
            if "STUB-PYTEST-RUNNING" in line:
                break
        else:
            pytest.fail("stub pytest never started:\n" + "".join(seen))
        proc.send_signal(sig)
        rest, _ = proc.communicate(timeout=60)
        out = "".join(seen) + rest
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=30)
    assert proc.returncode == code, out
    assert _calls(fake_repo).count("opentr stop") == 1, _calls(fake_repo)


def test_keep_stack_leaves_it_up(fake_repo: Path) -> None:
    proc = _run(fake_repo, "--keep-stack")
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 0, out
    assert "opentr stop" not in _calls(fake_repo)


def test_setup_failure_never_touches_the_stack(fake_repo: Path) -> None:
    proc = _run(fake_repo, STUB_SETUP_FAIL="1")
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 1, out
    assert not any(c.startswith("opentr") for c in _calls(fake_repo))
