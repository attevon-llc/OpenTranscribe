"""``setup-opentranscribe.sh`` must carry on when ``docker compose pull`` fails.

``pull_docker_images`` prints "will use cached versions" and returns 1; the script runs under
``set -e``, so the bare call in ``main`` aborted the install right after promising to continue.
Found by the v0.6.0 rehearsal (fresh-install and lite scenarios both ended "one-liner failed")
when MinIO's quay.io repository began refusing anonymous pulls.
"""

from __future__ import annotations

import re
import subprocess  # nosec B404 - runs the repo's own script with stubbed functions
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "setup-opentranscribe.sh"


def _main_body() -> str:
    text = SCRIPT.read_text()
    match = re.search(r"^main\(\) \{\n(.*?)^\}\n", text, re.S | re.M)
    assert match, "main() not found in setup-opentranscribe.sh"
    body = match.group(1)
    # Only the step sequence; everything above it is argument and environment handling.
    assert "    # Run setup steps\n" in body
    return body.split("    # Run setup steps\n", 1)[1]


@pytest.mark.unit
def test_main_does_not_abort_on_a_failed_pull():
    body = _main_body()
    defined = set(re.findall(r"^([a-z_]+)\(\) \{", SCRIPT.read_text(), re.M))
    called = set(re.findall(r"^\s{4}([a-z_]+)(?:\s|$)", body, re.M))
    stubs = "\n".join(
        f"{name}() {{ :; }}" for name in sorted(called & defined) if name != "pull_docker_images"
    )
    driver = f"""
set -uo pipefail
set -e
pull_docker_images() {{ return 1; }}
{stubs}
main() {{
{body}
    echo REACHED_SUMMARY
}}
main
"""
    proc = subprocess.run(  # nosec B603 B607 - fixed argv, no untrusted input
        ["bash", "-c", driver], capture_output=True, text=True, timeout=30, check=False
    )

    assert "REACHED_SUMMARY" in proc.stdout, proc.stdout + proc.stderr
