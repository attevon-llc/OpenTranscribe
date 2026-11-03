"""The release `test` stage runs on its own isolated --fresh stack, repeatably.

Run against the long-lived dev stack the gate's verdict depended on whatever that database
held: the seeded admin's password aged past the 60-day expiry, ``must_change_password`` was
set, and ~180 authenticated tests 403'd ``password_change_required`` -- nothing to do with the
release. These tests drive ``scripts/release/fresh-stack-lib.sh`` against a STUB ``opentr.sh``
that records its argv, so what is pinned is behaviour (what gets invoked, with which env), not
string literals; plus two checks against the real ``opentr.sh``.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
LIB = REPO_ROOT / "scripts" / "release" / "fresh-stack-lib.sh"
STAGE = REPO_ROOT / "scripts" / "release" / "60-test.sh"
REAL_OPENTR = REPO_ROOT / "opentr.sh"


def _bash(script: str, repo: Path, env: dict[str, str] | None = None) -> str:
    base = {k: v for k, v in os.environ.items() if not k.startswith("RELEASE_TEST_")}
    full = {**base, "REPO_ROOT": str(repo), **(env or {})}
    return subprocess.run(
        ["bash", "-c", f"source {LIB}\n{script}"],
        capture_output=True,
        text=True,
        check=True,
        env=full,
    ).stdout


def _stub_repo(tmp_path: Path) -> Path:
    """A repo root whose opentr.sh logs argv + the env the gate depends on, and succeeds."""
    repo = tmp_path / "repo"
    repo.mkdir()
    real = REAL_OPENTR.read_text(encoding="utf-8")
    block = re.search(r"^FRESH_PORT_VARS=\(.*?^\)", real, re.S | re.M)
    assert block, "opentr.sh no longer defines FRESH_PORT_VARS"
    stub = repo / "opentr.sh"
    stub.write_text(
        "#!/bin/bash\n"
        + block.group(0)
        + '\necho "$* | project=${COMPOSE_PROJECT_NAME:-} pg=${POSTGRES_PORT:-}" >> "$STUB_LOG"\n'
    )
    stub.chmod(0o755)
    return repo


def test_name_is_derived_from_the_version_and_overridable() -> None:
    assert _bash("fresh_stack_name v0.6.1", REPO_ROOT).strip() == "rel-0-6-1"
    assert _bash("fresh_stack_name 0.6.10", REPO_ROOT).strip() == "rel-0-6-10"
    assert (
        _bash("fresh_stack_name 0.6.1", REPO_ROOT).strip()
        != _bash("fresh_stack_name 0.61.0", REPO_ROOT).strip()
    )
    assert (
        _bash("fresh_stack_name 0.6.1", REPO_ROOT, {"RELEASE_TEST_FRESH_NAME": "mine"}).strip()
        == "mine"
    )


def test_ports_come_from_opentr_and_move_with_the_offset() -> None:
    real = REAL_OPENTR.read_text(encoding="utf-8")
    block = re.search(r"^FRESH_PORT_VARS=\((.*?)^\)", real, re.S | re.M).group(1)  # type: ignore[union-attr]
    stock = dict(re.findall(r'"([A-Z_]+)=(\d+)"', block))
    assert "POSTGRES_PORT" in stock and len(stock) >= 8
    for offset in (0, 400, 1234):
        got = dict(
            line.split("=")
            for line in _bash(f"fresh_stack_port_exports {offset}", REPO_ROOT).split()
        )
        assert got == {k: str(int(v) + offset) for k, v in stock.items()}


def test_up_destroys_stale_state_first_and_is_isolated(tmp_path: Path) -> None:
    repo = _stub_repo(tmp_path)
    log = tmp_path / "calls.log"
    env = {"STUB_LOG": str(log)}
    _bash("fresh_stack_up rel-x 400", repo, env)
    calls = log.read_text().splitlines()
    assert calls[0].startswith("fresh-destroy rel-x --yes"), "stale deployment not cleared first"
    assert calls[1].startswith("start dev --fresh rel-x --port-offset 400")
    assert "project=otfresh-rel-x" in calls[1] and "pg=5576" in calls[1]
    # Dev stack untouched, NAS never requested, nothing signalled.
    joined = "\n".join(calls)
    assert "--nas" not in joined
    assert not re.search(r"^(stop|restart|rebuild)", joined, re.M)
    assert all("--fresh" in c or c.startswith("fresh-destroy") for c in calls)


def test_rerun_behaves_identically(tmp_path: Path) -> None:
    repo = _stub_repo(tmp_path)
    log = tmp_path / "calls.log"
    env = {"STUB_LOG": str(log)}
    _bash("fresh_stack_up rel-x 400", repo, env)
    first = log.read_text()
    log.write_text("")
    _bash("fresh_stack_up rel-x 400", repo, env)
    assert log.read_text() == first


def test_real_opentr_fresh_start_never_loads_the_nas_overlay() -> None:
    out = subprocess.run(
        [
            str(REAL_OPENTR),
            "start",
            "dev",
            "--fresh",
            "rel-pytest-dry",
            "--port-offset",
            "400",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=60,
    )
    try:
        assert "docker-compose.nas.yml" not in out.stdout
        assert "NAS overlay omitted by design" in out.stdout
    finally:
        subprocess.run(
            [str(REAL_OPENTR), "fresh-destroy", "rel-pytest-dry", "--yes"],
            capture_output=True,
            cwd=REPO_ROOT,
            timeout=60,
        )


def test_stage_uses_the_lib_and_never_probes_the_dev_stack() -> None:
    text = STAGE.read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "fresh-stack-lib.sh" in code and "fresh_stack_up" in code
    assert "trap teardown_fresh EXIT" in code, "the stack must be torn down at exit"
    assert "5176" not in code, "the stage must not probe the dev stack's stock Postgres port"
    assert "opentr.sh start dev" not in code.replace("./opentr.sh start dev --fresh", "")
