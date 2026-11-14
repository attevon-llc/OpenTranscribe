"""``capture_service_logs`` captures every service of the compose project, not a name pattern.

A hard-coded ``worker|beat|opensearch`` filter dropped ``celery-redaction`` from the load
run's evidence. The function must take its service list from the project label and write one
gzipped log per service, whatever the service is called.
"""

from __future__ import annotations

import gzip
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
LIB = REPO / "scripts/perf/load/tools/capture_logs.sh"
RUN_PROFILE = REPO / "scripts/perf/load/tools/run_profile.sh"

SERVICES = ["backend", "celery-worker", "celery-redaction", "celery-beat", "flower", "postgres"]

FAKE_DOCKER = """#!/bin/bash
if [[ "$1" == ps ]]; then
    [[ "$*" == *"label=com.docker.compose.project=otfresh-x"* ]] || exit 9
    printf '%s\\n' __SERVICES__
elif [[ "$1" == logs ]]; then
    echo "log of ${@: -1}"
fi
"""


def _run(tmp_path: Path) -> list[str]:
    fake = tmp_path / "bin"
    fake.mkdir()
    docker = fake / "docker"
    docker.write_text(FAKE_DOCKER.replace("__SERVICES__", " ".join(SERVICES + ["flower"])))
    docker.chmod(0o755)
    out = tmp_path / "out"
    out.mkdir()
    script = (
        f'st_container() {{ echo "ctr-$1"; }}; source {LIB}; capture_service_logs otfresh-x {out} T'
    )
    subprocess.run(
        ["bash", "-c", script],
        check=True,
        env={"PATH": f"{fake}:/usr/bin:/bin"},
    )
    for svc in SERVICES:
        with gzip.open(out / f"{svc}.log.gz", "rt") as fh:
            assert fh.read().strip() == f"log of ctr-{svc}"
    return sorted(p.name for p in out.iterdir())


def test_every_service_of_the_project_is_captured_once(tmp_path):
    names = _run(tmp_path)
    assert names == sorted(f"{s}.log.gz" for s in SERVICES)
    assert "celery-redaction.log.gz" in names


def test_run_profile_uses_the_function_and_no_name_filter():
    text = RUN_PROFILE.read_text()
    assert "capture_service_logs" in text
    assert "grep -E" not in text
