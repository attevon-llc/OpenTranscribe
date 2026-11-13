"""scripts/lite-smoke.sh resolves its stack by compose label, never by container-name substring.

Executes the real script against a fake `docker` that lists ANOTHER project's containers first.
Not covered: a real stack, or nvidia-smi reporting real device memory (a fake one reports none).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "lite-smoke.sh"

FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, sys
cs = json.load(open(os.environ["FAKE_DOCKER_MODEL"]))
a = sys.argv[1:]
if a[0] == "ps":
    proj = [x.split("=", 2)[2] for x in a if x.startswith("label=com.docker.compose.project=")]
    rows = [c for c in cs if not proj or c["project"] == proj[0]]
    fmt = a[a.index("--format") + 1] if "--format" in a else ""
    for c in rows:
        if "-q" in a:
            print(c["name"])
        elif "\t" in fmt:
            print("\t".join([c["name"], c["project"], c["service"], "running", "/w", ""]))
        else:
            print(c["service"], c["name"])
elif a[0] == "inspect" and "--format" in a:
    print("healthy")
elif a[0] == "inspect":
    print("[{}]")
"""


def _run(tmp_path: Path, containers: list[dict], *args: str) -> subprocess.CompletedProcess[str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(FAKE_DOCKER)
    (bindir / "nvidia-smi").write_text("#!/bin/bash\nexit 0\n")
    for f in bindir.iterdir():
        f.chmod(0o755)
    model = tmp_path / "model.json"
    model.write_text(json.dumps(containers))
    env = {k: v for k, v in os.environ.items() if k != "COMPOSE_PROJECT_NAME"}
    env.update(PATH=f"{bindir}:{env['PATH']}", FAKE_DOCKER_MODEL=str(model))
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=60
    )


def c(name: str, project: str, service: str) -> dict:
    return {"name": name, "project": project, "service": service}


def test_a_gpu_worker_in_another_project_does_not_fail_the_checked_one(tmp_path):
    stacks = [
        c("opentranscribe-celery-worker-gpu", "transcribe-app", "celery-worker-gpu"),
        c("opentranscribe-backend", "transcribe-app", "backend"),
        c("ot-lite-backend", "ot-reltest-lite", "backend"),
    ]
    proc = _run(tmp_path, stacks, "--project", "ot-reltest-lite")
    assert proc.returncode == 0, proc.stderr


def test_a_gpu_worker_in_the_checked_project_fails_it(tmp_path):
    stacks = [
        c("opentranscribe-backend", "transcribe-app", "backend"),
        c("ot-lite-backend", "ot-reltest-lite", "backend"),
        c("ot-lite-celery-worker-gpu-scaled", "ot-reltest-lite", "celery-worker-gpu-scaled"),
    ]
    proc = _run(tmp_path, stacks, "--project", "ot-reltest-lite")
    assert proc.returncode == 1 and "ot-lite-celery-worker-gpu-scaled" in proc.stderr


def test_an_unresolvable_project_is_not_measured_never_a_pass(tmp_path):
    proc = _run(tmp_path, [c("x-backend", "other", "backend")], "--project", "ot-reltest-lite")
    assert proc.returncode == 4, (proc.returncode, proc.stderr)
