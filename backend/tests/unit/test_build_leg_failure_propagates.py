"""A failed ``docker buildx build`` for a backend leg must fail the build, not report success.

``legs+=("$(build_one_leg ...)")`` exits 0 whatever the substitution did, so the v0.6.0 lite
arm64 leg (pip could not resolve ``torchcodec==0.11.1+cpu`` on aarch64) printed "Lite backend
image built" and ``release.sh build`` passed with no arm64 image; only ``scan`` noticed.
"""

from __future__ import annotations

import subprocess  # nosec B404 - runs the repo's own script with a stubbed leg builder
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "docker-build-push.sh"

_DRIVER = """
set -e
source "{script}"
build_announce() {{ return 0; }}
build_backend_identity_args() {{ :; }}
build_platform_list() {{ echo "linux/amd64"; echo "linux/arm64"; }}
assemble_capability_index() {{ echo ASSEMBLED; }}
docker() {{ if [ "$1" = "buildx" ] && [[ " $* " == *" linux/arm64 "* ]]; then return 1; fi; return 0; }}
# Called under `||`, as main() calls it: errexit is then ignored inside, which is exactly why the
# unchecked `legs+=("$(...)")` swallowed the failure.
if {fn}; then echo "FN_OK"; else echo "FN_FAILED"; fi
"""


def _run(fn: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # nosec B603 B607 - fixed argv
        ["bash", "-c", _DRIVER.format(script=SCRIPT, fn=fn)],
        capture_output=True,
        cwd=SCRIPT.parents[1],
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.unit
@pytest.mark.parametrize("fn", ["build_backend_lite", "build_backend"])
def test_a_failed_leg_stops_the_build_before_it_claims_success(fn):
    proc = _run(fn)

    assert "FN_FAILED" in proc.stdout, proc.stdout + proc.stderr
    assert "FN_OK" not in proc.stdout
    assert "ASSEMBLED" not in proc.stdout
    assert "built (" not in proc.stdout
