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


def test_fast_volumes_are_created_tmpfs_before_start_and_removed_at_teardown(
    tmp_path: Path,
) -> None:
    repo = _stub_repo(tmp_path)
    log = tmp_path / "calls.log"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text('#!/bin/bash\necho "docker $*" >> "$STUB_LOG"\n')
    docker.chmod(0o755)
    env = {"STUB_LOG": str(log), "PATH": f"{bindir}:{os.environ['PATH']}"}
    _bash("fresh_stack_up rel-x 400; fresh_stack_teardown rel-x", repo, env)
    calls = log.read_text().splitlines()
    creates = [c for c in calls if c.startswith("docker volume create")]
    assert {c.split()[-1] for c in creates} == {
        "otfresh-rel-x_opensearch_data",
        "otfresh-rel-x_postgres_data",
    }
    assert all("type=tmpfs" in c for c in creates)
    start = next(i for i, c in enumerate(calls) if c.startswith("start dev --fresh"))
    assert all(calls.index(c) < start for c in creates), "volumes must exist before compose starts"
    # Stale ones are removed before creating (identical seed every run) and again at teardown.
    assert sum(c.startswith("docker volume rm") for c in calls[:start]) == 2
    assert sum(c.startswith("docker volume rm") for c in calls[start:]) == 2
    # Disabled means no volumes touched.
    log.write_text("")
    _bash("fresh_stack_up rel-x 400", repo, {**env, "RELEASE_TEST_FAST_VOLUMES": ""})
    assert "docker volume" not in log.read_text()


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


def test_dev_tests_probe_the_offset_ports_not_the_stock_ones() -> None:
    """A fresh stack lives on offset ports; a hard-coded :5174 probe reports it unreachable."""
    text = (REPO_ROOT / "scripts" / "run-dev-tests.sh").read_text(encoding="utf-8")
    probes = [ln for ln in text.splitlines() if "curl -sf http://localhost:" in ln]
    assert probes, "run-dev-tests.sh no longer probes the stack"
    for ln in probes:
        assert "${BACKEND_PORT" in ln or "${FRONTEND_PORT" in ln, f"hard-coded port in probe: {ln}"


def test_models_are_seeded_into_the_isolated_dir_before_start(tmp_path: Path) -> None:
    """An empty isolated diar-native dir makes the sidecar exit 8 and `up --wait` fail."""
    repo = _stub_repo(tmp_path)
    src = tmp_path / "export"
    src.mkdir()
    (src / "plda_lda.npy").write_bytes(b"x")
    (src / "diar-provision.json").write_text("{}")
    log = tmp_path / "calls.log"
    # The stub start records whether the seeded dir already held the files at start time.
    stub = repo / "opentr.sh"
    stub.write_text(
        stub.read_text()
        + 'if [[ "$1" == start ]]; then ls "$REPO_ROOT/.fresh/rel-x/diar-native-models" >> "$STUB_LOG"; fi\n'
    )
    env = {
        "STUB_LOG": str(log),
        "RELEASE_TEST_DIAR_MODELS_SRC": str(src),
        "RELEASE_TEST_FAST_VOLUMES": "",
    }
    _bash("fresh_stack_up rel-x 400", repo, env)
    seen = log.read_text().splitlines()
    assert "plda_lda.npy" in seen and "diar-provision.json" in seen
    # The source export is only read, never modified or mounted.
    assert sorted(p.name for p in src.iterdir()) == ["diar-provision.json", "plda_lda.npy"]
    # No source => no seed, no failure.
    log.write_text("")
    empty = tmp_path / "none"
    _bash("fresh_stack_up rel-y 400", repo, {**env, "RELEASE_TEST_DIAR_MODELS_SRC": str(empty)})
    assert not (repo / ".fresh" / "rel-y").exists()
