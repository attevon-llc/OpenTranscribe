"""A container-start budget must come from ``tests/docker_ops.py``, not from each caller.

``backend/tests/CLAUDE.md`` already says it:

    give the constant a **name**, put the **measurement** beside it, and never copy a
    budget into a fifth file.

and the repo had already paid to learn the number — ``docker run alpine`` at 60 s was
raised to 120 s **plus one retry**. But that constant was private to the module that
learned it, so the two throwaway-Redis fixtures kept their own ``timeout=30``.

Measured 2026-09-08 on a ``run-dev-tests.sh --full``: ``docker run -d --rm -p
127.0.0.1:<port>:6379 redis:7-alpine`` timed out at 30 s and took **19 tests** with it —
all 18 of ``test_migration_progress_service`` (one ``xdist_group``, so the whole module
died in fixture setup) and ``test_backup_tasks``'s real-Redis lock test. The gate reported
a red backend phase; nothing was wrong with the product. The same command takes seconds on
an idle host — **the gate itself is the load**, which is the entire point of that rule.

⚠️ This is NOT a style check. A fixture timeout reports as an ERROR against every test in
its group, so one contended second deletes a module's worth of coverage and looks like a
product failure while doing it.
"""

from __future__ import annotations

import ast
import re
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TESTS_ROOT = REPO_ROOT / "backend" / "tests"

# `timeout=<int>` on a subprocess call whose command starts a container.
_HARDCODED = re.compile(r"timeout\s*=\s*(\d+)")

#: Anything at or below this is too tight to survive a loaded daemon — that is the measured
#: failure. Callers wanting a *longer* explicit budget are not the hazard.
_TOO_TIGHT_S = 60


def _files_that_start_a_container() -> list[Path]:
    out: list[Path] = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        if path.name == "docker_ops.py":
            continue
        text = path.read_text(encoding="utf-8")
        if '"docker",' in text and '"run",' in text:
            out.append(path)
    return out


def test_no_test_starts_a_container_on_its_own_tight_budget():
    offenders: list[str] = []
    for path in _files_that_start_a_container():
        text = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name != "run":
                continue
            # Only calls whose command list literally starts a container.
            src = ast.get_source_segment(text, node) or ""
            if '"docker"' not in src or '"run"' not in src:
                continue
            m = _HARDCODED.search(src)
            if m and int(m.group(1)) <= _TOO_TIGHT_S:
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{node.lineno}: timeout={m.group(1)}s"
                )

    assert not offenders, (
        "these start a container on a hardcoded budget of "
        f"{_TOO_TIGHT_S}s or less. On a loaded daemon that is a FIXTURE failure, which "
        "reports as an ERROR against every test in the group — 19 tests died this way on "
        "2026-09-08. Use tests/docker_ops.run_container, which carries the measured "
        "budget and a retry:\n  " + "\n  ".join(offenders)
    )


def test_the_scanner_can_fire(tmp_path: Path):
    """Guard the guard: a detector matching nothing reads exactly like a clean tree."""
    victim = tmp_path / "bad.py"
    victim.write_text(
        "import subprocess\n"
        'subprocess.run(["docker", "run", "-d", "redis:7-alpine"], timeout=30)\n',
        encoding="utf-8",
    )
    text = victim.read_text(encoding="utf-8")
    found = []
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Call):
            src = ast.get_source_segment(text, node) or ""
            if '"docker"' in src and '"run"' in src:
                m = _HARDCODED.search(src)
                if m and int(m.group(1)) <= _TOO_TIGHT_S:
                    found.append(m.group(1))
    assert found == ["30"], "the scanner no longer matches the exact shape that killed 19 tests"


def test_the_two_redis_fixtures_go_through_the_shared_helper():
    """Named explicitly, because these are the two that regressed."""
    for rel in (
        "backend/tests/unit/test_migration_progress_service.py",
        "backend/tests/unit/test_backup_tasks.py",
    ):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "run_container(" in text, f"{rel} no longer uses the shared container starter"
        assert "stop_container(" in text, f"{rel} no longer uses the shared teardown"


# --------------------------------------------------------- the retry must actually retry


def test_run_container_retries_before_giving_up(monkeypatch: pytest.MonkeyPatch):
    """The retry is the load-bearing half, not belt-and-braces.

    A single longer timeout still fails if the daemon is saturated at the one moment you
    ask. Asking again a moment later is what recovers. A test that only checked the
    constant would pass against an implementation that never retried.
    """
    from tests import docker_ops

    calls: list[float] = []

    def fake_run(cmd, **kwargs):
        calls.append(time.monotonic())
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    monkeypatch.setattr(docker_ops.subprocess, "run", fake_run)
    monkeypatch.setattr(docker_ops, "DOCKER_RETRY_PAUSE_S", 0.0)

    result = docker_ops.run_container(["run", "-d", "redis:7-alpine"])
    assert len(calls) == 2, f"expected one retry after a timeout, got {len(calls)} attempt(s)"
    assert result.stdout == "ok"


def test_run_container_raises_with_every_attempt_diagnosed(monkeypatch: pytest.MonkeyPatch):
    """When it genuinely cannot start, the error must be actionable.

    "it timed out" without the budget or the stderr is what made the original failure cost
    a full gate run to understand.
    """
    from tests import docker_ops

    def always_timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))

    monkeypatch.setattr(docker_ops.subprocess, "run", always_timeout)
    monkeypatch.setattr(docker_ops, "DOCKER_RETRY_PAUSE_S", 0.0)

    with pytest.raises(docker_ops.DockerOpError) as excinfo:
        docker_ops.run_container(["run", "-d", "redis:7-alpine"])

    message = str(excinfo.value)
    assert "attempt 1" in message and "attempt 2" in message, (
        f"the error does not account for every attempt: {message!r}"
    )
    assert str(docker_ops.DOCKER_OP_TIMEOUT) in message, (
        f"the error does not name the budget that was exceeded: {message!r}"
    )


def test_the_shared_budget_is_not_below_what_was_measured():
    from tests import docker_ops

    assert docker_ops.DOCKER_OP_TIMEOUT >= 120, (
        "the shared budget dropped below the 120s that test_opentr_stop_container_scoping "
        "measured this host to need. Lowering it re-creates the 19-test failure."
    )
