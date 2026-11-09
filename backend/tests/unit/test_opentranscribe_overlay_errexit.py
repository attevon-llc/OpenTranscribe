"""A failing command inside a compose_overlay_* step must abort the script (set -e).

The steps run inside ``$(...)``, where bash does not inherit errexit (and macOS bash 3.2 has no
``inherit_errexit``); without an explicit ``set -e`` a failure would be swallowed and the chain
would silently omit an overlay.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests.unit._shell_extract import MANAGER
from tests.unit._shell_extract import extract_function

_STEPS = [
    "compose_overlay_lite",
    "compose_overlay_gpu",
    "compose_overlay_db_roles",
    "compose_overlay_nginx",
    "compose_overlay_backup",
    "compose_overlay_diar_native",
    "compose_overlay_gpu_split",
    "compose_overlay_demo",
]


def _run(failing_read_env: bool, cwd: Path) -> subprocess.CompletedProcess[str]:
    read_env = (
        "read_env_value() { return 1; }" if failing_read_env else "read_env_value() { echo ''; }"
    )
    snippet = "\n".join(
        [
            "set -e",
            "BLUE=''; YELLOW=''; RED=''; NC=''",
            read_env,
            "effective_deployment_mode() { echo full; }",
            "detect_nvidia_runtime() { echo default; }",
            "force_cpu_mode_requested() { return 1; }",
            "nginx_overlay_ready() { return 1; }",
            "gpu_split_active() { return 1; }",
            "resolve_diar_native_models_dir() { echo /nonexistent; }",
            *(extract_function(MANAGER, n) for n in _STEPS),
            'out="$(compose_overlay_db_roles)"',
            'echo "REACHED:$out"',
        ]
    )
    return subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, cwd=cwd)


def test_a_failing_command_inside_an_overlay_step_aborts_the_script(tmp_path: Path):
    proc = _run(failing_read_env=True, cwd=tmp_path)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "REACHED" not in proc.stdout


def test_control_the_same_step_succeeds_when_nothing_fails(tmp_path: Path):
    proc = _run(failing_read_env=False, cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "REACHED:" in proc.stdout
