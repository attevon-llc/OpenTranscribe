"""`scan` records `images-carry-no-dev-artifacts` from check-image-hygiene.sh, per leg.

The REAL ``50-scan.sh``, ``criteria-lib.sh`` and ``release-criteria.yaml`` run in a scratch
repo; only the scanner, the platform table, the hygiene check and ``docker`` are stubs. So the
criterion's declaration and recording are exercised together (criteria-lib exits 2 if they
disagree), and the outcome mapping is the shipped code.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
STAGE = REPO_ROOT / "scripts" / "release" / "50-scan.sh"

pytestmark = pytest.mark.skipif(not STAGE.exists(), reason="release scripts not in this checkout")

VERSION = "v9.9.9"
LEGS = [
    "test/ot-backend:v9.9.9-cuda-amd64",
    "test/ot-docs:v9.9.9-multiarch-amd64",
    "test/ot-docs:v9.9.9-multiarch-arm64",
]


def _exe(path: Path, body: str) -> None:
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _world(tmp_path: Path, hygiene: dict[str, int]) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "repo"
    (repo / "scripts" / "release").mkdir(parents=True)
    for name in ("50-scan.sh", "criteria-lib.sh", "release-criteria.yaml"):
        shutil.copy(REPO_ROOT / "scripts" / "release" / name, repo / "scripts" / "release" / name)
    s = repo / "scripts"
    _exe(
        s / "security-scan.sh",
        'if [[ "$1" == list-repos ]]; then\n'
        "  printf 'backend\\ttest/ot-backend\\nblackwell\\ttest/ot-backend\\ndocs\\ttest/ot-docs\\n'\n"
        "  exit 0\nfi\n"
        'mkdir -p "$OUTPUT_DIR"\n'
        'case "$1" in backend) arches=amd64; repo=test/ot-backend ;;'
        " docs) arches='amd64 arm64'; repo=test/ot-docs ;; esac\n"
        "for a in $arches; do\n"
        '  printf \'{"ArtifactName":"%s:%s-scanleg-%s"}\' "$repo" "$IMAGE_TAG" "$a"'
        ' > "$OUTPUT_DIR/$1-$a-trivy.json"\n'
        "done\n",
    )
    _exe(
        s / "docker-build-push.sh",
        "printf 'backend\\tcuda\\tlinux/amd64\\nblackwell\\tblackwell\\tlinux/arm64\\n"
        "docs\\tmultiarch\\tlinux/amd64,linux/arm64\\n'\n",
    )
    log = tmp_path / "hygiene.log"
    cases = "".join(
        f'  "{img}") echo "  /app/mutants" >&2; exit {code} ;;\n'
        if code == 1
        else f'  "{img}") exit {code} ;;\n'
        for img, code in hygiene.items()
    )
    _exe(
        s / "check-image-hygiene.sh",
        f'echo "$1" >> {log}\ncase "$1" in\n{cases}  *) exit 0 ;;\nesac\n',
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _exe(bindir / "docker", "exit 0\n")
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "OT_SCAN_DIR": str(tmp_path / "reports"),
        "JSON_OUT": "true",
    }
    return repo, env


def _run(tmp_path: Path, hygiene: dict[str, int]) -> tuple[int, dict, list[str]]:
    repo, env = _world(tmp_path, hygiene)
    proc = subprocess.run(
        ["bash", "scripts/release/50-scan.sh", VERSION],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    assert lines, f"no --json payload (rc={proc.returncode}):\n{proc.stderr[-3000:]}"
    payload = json.loads(lines[-1])
    crit = {c["id"]: c for c in payload["criteria"]}["images-carry-no-dev-artifacts"]
    log = tmp_path / "hygiene.log"
    checked = log.read_text().splitlines() if log.exists() else []
    return proc.returncode, crit, checked


def test_clean_legs_pass_and_every_leg_tag_is_checked(tmp_path: Path):
    rc, crit, checked = _run(tmp_path, {})
    assert (rc, crit["outcome"], crit["status"]) == (0, "pass", "pass")
    assert sorted(checked) == sorted(LEGS), "must check each per-leg tag, never :latest"


def test_dirty_leg_fails_the_stage_naming_the_path(tmp_path: Path):
    rc, crit, _ = _run(tmp_path, {LEGS[2]: 1})
    assert rc == 1
    assert (crit["outcome"], crit["status"]) == ("fail", "fail")
    assert LEGS[2] in crit["detail"] and "/app/mutants" in crit["detail"]


def test_could_not_inspect_is_not_measured_and_blocks(tmp_path: Path):
    rc, crit, _ = _run(tmp_path, {LEGS[0]: 2})
    assert rc == 1
    assert (crit["outcome"], crit["status"]) == ("not-measured", "fail")
    assert LEGS[0] in crit["detail"]
