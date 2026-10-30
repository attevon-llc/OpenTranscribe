"""Rehearse and smoke must refuse (exit 3) when ANY compose project owns the stock ports.

THE BUG

`65-rehearse.sh` and `85-smoke.sh` decided "the live stack is stopped" by asking docker for
containers labelled `opentranscribe` / `transcribe-app` (smoke: `opentranscribe` alone). An
`otfresh-master` stack — what the release `test` stage runs on, because the gate hardcodes the
stock ports — carries neither label, so the check recorded `live-stack-stopped pass`. All three
scenarios then died in `gr_check_ports_free` with exit 1, which `release.sh` records as a
release FAIL rather than an unmet precondition (exit 3), with a message about ports instead of
the owner and the command that stops it.

APPROACH

Both halves against the REAL shipped shell, with a fake `docker` on PATH:

1. `scripts/lib/stock-port-owners.sh` is sourced and driven directly: port-range parsing,
   IPv4/IPv6 de-duplication, the stop-hint mapping, and the three-way return code (free /
   owned / could not check).
2. The `# --- BEGIN stock-port-owners-check ---` block is extracted from each of
   `65-rehearse.sh`, `85-smoke.sh` and `10-preflight.sh` and run with a stubbed `record`, so the
   control flow, the exit code and what gets recorded are the shipped ones.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
LIB_SH = REPO_ROOT / "scripts" / "lib" / "stock-port-owners.sh"
REHEARSE_SH = REPO_ROOT / "scripts" / "release" / "65-rehearse.sh"
SMOKE_SH = REPO_ROOT / "scripts" / "release" / "85-smoke.sh"
PREFLIGHT_SH = REPO_ROOT / "scripts" / "release" / "10-preflight.sh"

MARKER = "stock-port-owners-check"

# `|`-delimited, as the shipped `docker ps --format` is: a tab delimiter would collapse an
# empty compose-project label under `read` and shift every later field.
OTFRESH_PG = (
    f"otfresh-master-postgres|otfresh-master|{REPO_ROOT}|"
    "0.0.0.0:5176->5432/tcp, [::]:5176->5432/tcp"
)
UNRELATED = "cvat_db|cvat|/srv/cvat|0.0.0.0:5432->5432/tcp"


def _extract_marked_block(script: Path, name: str) -> str:
    text = script.read_text(encoding="utf-8")
    start = text.index(f"# --- BEGIN {name} ---")
    end = text.index(f"# --- END {name} ---", start)
    return text[start : text.index("\n", end) + 1]


def _fake_docker(bin_dir: Path, rows: list[str], *, fail: bool = False) -> None:
    fixture = bin_dir / "docker-ps.tsv"
    fixture.write_text("".join(f"{r}\n" for r in rows), encoding="utf-8")
    script = bin_dir / "docker"
    body = "exit 1\n" if fail else f'if [ "$1" = "ps" ]; then cat "{fixture}"; exit 0; fi\nexit 0\n'
    script.write_text("#!/bin/bash\n" + body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


def _bash(
    tmp_path: Path, snippet: str, rows: list[str], *, fail: bool = False
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _fake_docker(bin_dir, rows, fail=fail)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    return subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, env=env)


# ───────────────────────────────────────────────────────── half 1: the library ──


@pytest.mark.unit
def test_owner_listing_names_port_container_project_and_dir(tmp_path: Path) -> None:
    proc = _bash(
        tmp_path,
        f'source "{LIB_SH}"; stock_port_owners',
        [OTFRESH_PG, UNRELATED],
    )

    assert proc.returncode == 0, proc.stderr
    # IPv4 + IPv6 publishes of one port are ONE owner row; 5432 is outside the stock range.
    assert proc.stdout.splitlines() == [
        f"5176\totfresh-master-postgres\totfresh-master\t{REPO_ROOT}"
    ], proc.stdout


@pytest.mark.unit
def test_published_port_ranges_are_expanded(tmp_path: Path) -> None:
    row = "wide|p|/d|0.0.0.0:5170-5174->8000-8004/tcp"

    proc = _bash(tmp_path, f'source "{LIB_SH}"; stock_port_owners', [row])

    assert [line.split("\t")[0] for line in proc.stdout.splitlines()] == ["5173", "5174"], (
        proc.stdout
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("project", "workdir", "expected"),
    [
        ("otfresh-master", "/repo", "./opentr.sh stop --fresh master"),
        ("otfresh-a-b", "/repo", "./opentr.sh stop --fresh a-b"),
        # `.fresh/<name>.*` state lives in the checkout that created it.
        ("otfresh-master", "/x", "cd /x && ./opentr.sh stop --fresh master"),
        ("transcribe-app", "/repo", "./opentr.sh stop"),
        ("opentranscribe", "/elsewhere", "cd /elsewhere && ./opentranscribe.sh stop"),
        ("opentranscribe", "/repo", "./opentr.sh stop"),
    ],
)
def test_stop_hint_matches_the_owning_project(
    tmp_path: Path, project: str, workdir: str, expected: str
) -> None:
    proc = _bash(
        tmp_path,
        f'source "{LIB_SH}"; stock_port_stop_hint "{project}" "{workdir}" /repo',
        [],
    )

    assert proc.stdout.strip() == expected, proc.stdout


@pytest.mark.unit
def test_check_returns_free_owned_and_could_not_check(tmp_path: Path) -> None:
    snippet = f'source "{LIB_SH}"; stock_port_check; echo "RC=$?"'

    assert "RC=0" in _bash(tmp_path, snippet, [UNRELATED]).stdout
    assert "RC=1" in _bash(tmp_path, snippet, [OTFRESH_PG]).stdout
    # A docker that cannot answer is COULD NOT CHECK (2), never an all-clear.
    assert "RC=2" in _bash(tmp_path, snippet, [], fail=True).stdout


# ───────────────────────────────────────────── half 2: the three stage call sites ──


def _run_block(tmp_path: Path, script: Path, rows: list[str]) -> tuple[int, str, list[list[str]]]:
    record_log = tmp_path / "records.log"
    snippet = f"""
set -uo pipefail
REPO_ROOT="{REPO_ROOT}"
RED=''; GREEN=''; YELLOW=''; BLUE=''; NC=''
JSON_OUT=false
live_stack_names=""
VERSION=v9.9.9
record() {{ printf '%s\\t%s\\t%s\\t%s\\n' "$1" "$2" "${{3:-}}" "${{4:-}}" >> "{record_log}"; }}
criteria_json() {{ :; }}
{_extract_marked_block(script, MARKER)}
echo "FELL_THROUGH"
"""
    proc = _bash(tmp_path, snippet, rows)
    records = (
        [line.split("\t") for line in record_log.read_text(encoding="utf-8").splitlines()]
        if record_log.exists()
        else []
    )
    return proc.returncode, proc.stdout + proc.stderr, records


@pytest.mark.unit
@pytest.mark.parametrize("script", [REHEARSE_SH, SMOKE_SH])
def test_stage_refuses_with_exit_3_when_a_fresh_stack_owns_the_stock_ports(
    tmp_path: Path, script: Path
) -> None:
    rc, out, records = _run_block(tmp_path, script, [OTFRESH_PG])

    assert rc == 3, f"a precondition, not a scenario failure:\n{out}"
    assert "FELL_THROUGH" not in out
    assert records and records[0][:2] == ["live-stack-stopped", "fail"], records
    assert "otfresh-master" in records[0][2], records
    assert records[0][3] == "./opentr.sh stop --fresh master", records
    assert "./opentr.sh stop --fresh master" in out


@pytest.mark.unit
@pytest.mark.parametrize("script", [REHEARSE_SH, SMOKE_SH])
def test_stage_falls_through_when_nothing_owns_the_stock_ports(
    tmp_path: Path, script: Path
) -> None:
    rc, out, records = _run_block(tmp_path, script, [UNRELATED])

    assert rc == 0 and "FELL_THROUGH" in out, out
    assert records == [], f"the stage records its own pass after the block: {records}"


@pytest.mark.unit
def test_preflight_warns_naming_the_owner(tmp_path: Path) -> None:
    rc, out, records = _run_block(tmp_path, PREFLIGHT_SH, [OTFRESH_PG])

    assert rc == 0 and "FELL_THROUGH" in out, f"preflight only warns:\n{out}"
    assert records and records[0][:2] == ["live-stack", "fail"], records
    assert "otfresh-master" in records[0][2], records
    assert records[0][3] == "./opentr.sh stop --fresh master", records


@pytest.mark.unit
def test_preflight_passes_when_the_ports_are_free(tmp_path: Path) -> None:
    rc, out, records = _run_block(tmp_path, PREFLIGHT_SH, [UNRELATED])

    assert rc == 0, out
    assert records and records[0][:2] == ["live-stack", "pass"], records
