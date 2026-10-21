"""``opentr.sh fresh-destroy`` must be usable without a terminal, and say so when it is not (#1238).

The prompt treated EOF as "cancelled" and exited 0, so a backgrounded or piped caller was told
success for a destroy that never happened. These tests run the REAL ``fresh_destroy`` function
(extracted from ``opentr.sh``) and the REAL dispatch arm, with the surrounding helpers stubbed
and a fake ``docker`` that records whether ``compose ... down`` -- the destructive call -- ran.
"""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENTR_SH = REPO_ROOT / "opentr.sh"

FAKE_DOCKER = """\
#!/bin/bash
echo "$*" >> "${FAKE_DOCKER_LOG:?}"
exit 0
"""


def _function_source() -> str:
    source = OPENTR_SH.read_text(encoding="utf-8")
    match = re.search(r"^fresh_destroy\(\) \{.*?^\}\n", source, re.MULTILINE | re.DOTALL)
    assert match, "no fresh_destroy() in opentr.sh -- was it renamed?"
    return match.group(0)


def _dispatch_arm() -> str:
    source = OPENTR_SH.read_text(encoding="utf-8")
    match = re.search(r"^  fresh-destroy\)\n(.*?)^    ;;\n", source, re.MULTILINE | re.DOTALL)
    assert match, "no fresh-destroy) arm in opentr.sh's command dispatch"
    return match.group(1)


@pytest.fixture
def fake_env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    log = tmp_path / "docker.log"
    log.write_text("", encoding="utf-8")
    return {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(log),
        "OVERLAY_DIR": str(tmp_path / "overlays"),
    }


def run_destroy(
    args: str, fake_env: dict[str, str], *, stdin: str | None
) -> subprocess.CompletedProcess[str]:
    """Run the real dispatch arm, which calls the real fresh_destroy, with helpers stubbed."""
    script = textwrap.dedent(f"""
        set -uo pipefail
        fresh_sanitize_name() {{ echo "$1"; }}
        fresh_project_name() {{ echo "otfresh-$1"; }}
        fresh_compose_chain() {{ echo ""; }}
        fresh_diar_native_models_dir() {{ echo "$OVERLAY_DIR/none"; }}
        ot_drain_gpu_workers() {{ :; }}
        FRESH_OVERLAY_DIR="$OVERLAY_DIR"
        {_function_source()}
        set -- fresh-destroy {args}
        {_dispatch_arm()}
        echo "RC=$?"
    """)
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, **fake_env},
        input=stdin if stdin is not None else "",
        timeout=60,
    )


def destructive_call_ran(fake_env: dict[str, str]) -> bool:
    log = Path(fake_env["FAKE_DOCKER_LOG"]).read_text(encoding="utf-8")
    return any(" down " in f" {line} " for line in log.splitlines())


def rc_of(proc: subprocess.CompletedProcess[str]) -> int:
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("RC="):
            return int(line.removeprefix("RC="))
    raise AssertionError(f"harness crashed\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")


def test_yes_destroys_without_reading_a_prompt(fake_env: dict[str, str]):
    proc = run_destroy("visual --yes", fake_env, stdin=None)

    assert rc_of(proc) == 0, proc.stderr
    assert destructive_call_ran(fake_env)
    assert "proceeding without confirmation" in proc.stdout


def test_yes_is_accepted_before_the_name_too(fake_env: dict[str, str]):
    proc = run_destroy("--yes visual", fake_env, stdin=None)

    assert rc_of(proc) == 0, proc.stderr
    assert destructive_call_ran(fake_env)
    assert "project otfresh-visual" in proc.stdout


def test_no_answer_at_the_prompt_is_a_failure_not_a_quiet_success(fake_env: dict[str, str]):
    """EOF used to read as 'cancelled' with exit 0 -- success for a destroy that did not happen."""
    proc = run_destroy("visual", fake_env, stdin="")

    assert rc_of(proc) == 1, proc.stdout
    assert not destructive_call_ran(fake_env)
    assert "NOTHING was destroyed" in proc.stderr
    assert "--yes" in proc.stderr


def test_an_explicit_no_still_cancels_cleanly(fake_env: dict[str, str]):
    proc = run_destroy("visual", fake_env, stdin="n\n")

    assert rc_of(proc) == 0, proc.stderr
    assert not destructive_call_ran(fake_env)
    assert "Destroy cancelled" in proc.stdout


def test_an_explicit_yes_at_the_prompt_destroys(fake_env: dict[str, str]):
    proc = run_destroy("visual", fake_env, stdin="y\n")

    assert rc_of(proc) == 0, proc.stderr
    assert destructive_call_ran(fake_env)
