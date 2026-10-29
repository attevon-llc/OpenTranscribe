"""An overlay start under a ``--fresh`` stack must stay inside that fresh deployment.

``start_stack_or_die`` used to run a bare ``opentr.sh start dev --with-X``. Run while the stack was
an isolated ``otfresh-<name>`` project (``COMPOSE_PROJECT_NAME`` exported), that recreated its
services under the stock names with the NAS overlay auto-loaded, attached to the LIVE data
directories. The real function is extracted from the library and driven against a stub
``opentr.sh`` that records its arguments.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[3] / "scripts" / "lib" / "dev-test-overlays.sh"


def _run(tmp_path: Path, project: str) -> str:
    stub = tmp_path / "opentr.sh"
    stub.write_text('#!/usr/bin/env bash\necho "$@" >> "$OT_ARGS_LOG"\n', encoding="utf-8")
    stub.chmod(0o755)
    args_log = tmp_path / "args.log"
    script = textwrap.dedent(
        f"""
        set -uo pipefail
        eval "$(sed -n '/^start_stack_or_die()/,/^}}/p' '{LIB}')"
        compose_project_name() {{ echo '{project}'; }}
        REPO_ROOT='{tmp_path}'
        REPORT_DIR='{tmp_path}'
        start_stack_or_die "mock llm" mock-llm --with-mock-llm
        """
    )
    result = subprocess.run(  # nosec B603 B607 - fixed argv, stub opentr.sh
        ["bash", "-c", script],
        env={"PATH": "/usr/bin:/bin", "OT_ARGS_LOG": str(args_log)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return args_log.read_text(encoding="utf-8").strip()


@pytest.mark.unit
def test_a_fresh_stack_gets_its_overlay_started_inside_the_same_fresh_deployment(tmp_path):
    assert _run(tmp_path, "otfresh-master") == "start dev --fresh master --with-mock-llm"


@pytest.mark.unit
def test_the_stock_stack_is_started_without_a_fresh_flag(tmp_path):
    assert _run(tmp_path, "transcribe-app") == "start dev --with-mock-llm"


@pytest.mark.unit
def test_a_bare_fresh_prefix_with_no_name_is_not_treated_as_a_fresh_stack(tmp_path):
    assert _run(tmp_path, "otfresh-") == "start dev --with-mock-llm"
