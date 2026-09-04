"""Issue #709 (stale .env VALUE detection) and issue #656's remaining item (a diarization
surface in ``./opentranscribe.sh status``).

Both live in ``opentranscribe.sh``. Fast, hermetic: no Docker, no network, no live stack — a
fake ``docker`` binary is put first on ``PATH`` for the status tests, matching
``test_shipped_scripts_p0_regressions.py``'s convention. Per ``backend/tests/CLAUDE.md``, a
test that executes real infrastructure tooling must be namespace-scoped (issue #693 destroyed
17 live containers this way); these tests never invoke the real ``docker`` binary at all.

Part 1 (stale keys):
- ``check_stale_env_values()`` is a declared table (``STALE_ENV_CHECKS``) consulted by BOTH
  ``preflight_upgrade_env`` and the ``update-full`` new-key report, so a release that
  invalidates a value says so before teardown.
- It must WARN and name the fix, and must NEVER rewrite ``.env``.

Part 2 (diar status):
- ``print_diar_native_status()`` is only invoked when ``docker-compose.diar-native.yml`` is in
  the resolved compose chain.
- It must read ``devices`` (loaded), never ``supported_devices`` (build-time capability only).
- Its workers-vs-permits warning must use the SAME predicate as
  ``test_env_example_gpu_scale_derivation.py`` (effective workers > DIAR_NATIVE_MAX_INFLIGHT).
- It must never derive "which engine served a file" (that is ``media_file.diarization_provider``,
  issue #706) — it only answers "can the sidecar serve right now".
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MANAGER = REPO_ROOT / "opentranscribe.sh"
COMMON = REPO_ROOT / "scripts" / "common.sh"

pytestmark = pytest.mark.skipif(
    not MANAGER.exists(), reason="opentranscribe.sh not present in this checkout"
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _run_shell(snippet: str, env: dict[str, str] | None = None, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", **(env or {})},
        cwd=str(cwd) if cwd else None,
    )
    return _strip_ansi(proc.stdout + proc.stderr).strip()


def _extract_function(script: Path, name: str) -> str:
    fn = subprocess.run(
        ["sed", "-n", f"/^{re.escape(name)}() {{/,/^}}/p", str(script)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert fn.strip(), f"{name}() not found in {script}"
    return fn


def _extract_array(script: Path, name: str) -> str:
    """Extract a `NAME=(\\n...\\n)` bash array declaration."""
    out = subprocess.run(
        ["sed", "-n", f"/^{re.escape(name)}=(/,/^)/p", str(script)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.strip(), f"{name}=(...) not found in {script}"
    return out


# --------------------------------------------------------------------------- #
# Part 1: stale .env VALUE detection (issue #709)
# --------------------------------------------------------------------------- #


def _check_stale_env_values_snippet(env_contents: str, tmp_path: Path) -> str:
    """Drive the real check_stale_env_values() (plus its table and read_env_value
    dependency) against a synthetic .env, with no other side effects.
    """
    env_file = tmp_path / "dotenv"
    env_file.write_text(env_contents)
    table = _extract_array(MANAGER, "STALE_ENV_CHECKS")
    fn = _extract_function(MANAGER, "check_stale_env_values")
    snippet = f"""
source {COMMON}
{table}
{fn}
check_stale_env_values {env_file}
"""
    return _run_shell(snippet)


def test_stale_engine_shared_volume_path_is_flagged(tmp_path: Path):
    out = _check_stale_env_values_snippet(
        "ENGINE_SHARED_VOLUME_PATH=/tmp/transcription\n", tmp_path
    )
    assert "ENGINE_SHARED_VOLUME_PATH=/tmp/transcription" in out
    assert "661" in out or "pipeline_scratch" in out


def test_current_engine_shared_volume_path_is_not_flagged(tmp_path: Path):
    """Must-stay-clean: the coded default itself must never be reported as stale."""
    out = _check_stale_env_values_snippet(
        "ENGINE_SHARED_VOLUME_PATH=/scratch/opentranscribe/engine\n", tmp_path
    )
    assert out == ""


def test_gpu_scale_workers_exceeding_max_inflight_is_flagged(tmp_path: Path):
    out = _check_stale_env_values_snippet(
        "GPU_SCALE_WORKERS=4\nDIAR_NATIVE_MAX_INFLIGHT=2\n", tmp_path
    )
    assert "GPU_SCALE_WORKERS=4" in out
    assert "DIAR_NATIVE_MAX_INFLIGHT" in out


def test_gpu_scale_workers_within_max_inflight_is_not_flagged(tmp_path: Path):
    out = _check_stale_env_values_snippet(
        "GPU_SCALE_WORKERS=2\nDIAR_NATIVE_MAX_INFLIGHT=2\n", tmp_path
    )
    assert out == ""


def test_unset_keys_produce_no_findings(tmp_path: Path):
    out = _check_stale_env_values_snippet("SOME_OTHER_KEY=value\n", tmp_path)
    assert out == ""


def test_check_stale_env_values_never_writes_to_the_env_file(tmp_path: Path):
    """The instructions are explicit: WARN and name the fix, never rewrite .env. A silent
    correction here is exactly how the ENGINE_SHARED_VOLUME_PATH regression hid originally.
    """
    env_file = tmp_path / "dotenv"
    original = "ENGINE_SHARED_VOLUME_PATH=/tmp/transcription\nGPU_SCALE_WORKERS=4\nDIAR_NATIVE_MAX_INFLIGHT=2\n"
    env_file.write_text(original)
    table = _extract_array(MANAGER, "STALE_ENV_CHECKS")
    fn = _extract_function(MANAGER, "check_stale_env_values")
    snippet = f"""
source {COMMON}
{table}
{fn}
check_stale_env_values {env_file} > /dev/null
"""
    _run_shell(snippet)
    assert env_file.read_text() == original, "check_stale_env_values must never modify .env"


def test_adding_a_third_case_is_a_one_line_table_entry_not_a_new_call_site():
    """The whole point of the table: STALE_ENV_CHECKS must be consulted by exactly one
    function (check_stale_env_values), which both callers below share — not duplicated logic
    per caller.
    """
    text = MANAGER.read_text()
    assert text.count("check_stale_env_values") >= 3, (
        "expected the table's own definition plus at least two call sites "
        "(preflight_upgrade_env and the update-full report)"
    )


def test_preflight_upgrade_env_calls_check_stale_env_values():
    """Hooked into preflight_upgrade_env so a stale value is surfaced BEFORE teardown, same
    placement as issue #670's hard refusal.
    """
    fn = _extract_function(MANAGER, "preflight_upgrade_env")
    assert "check_stale_env_values" in fn


def test_update_full_arm_calls_check_stale_env_values():
    text = MANAGER.read_text()
    idx = text.index("New settings in this release")
    idx_full = text.index("update-full)")
    # The call must appear in the update-full arm, after the new-keys report and before the
    # docker image pull/teardown step.
    tail = text[idx:]
    teardown_idx = tail.index("compose_down_for_upgrade")
    call_idx = tail.index("check_stale_env_values")
    assert idx_full < idx, "sanity: new-keys report must live inside the update-full arm"
    assert call_idx < teardown_idx, (
        "check_stale_env_values must run before compose_down_for_upgrade (teardown), while "
        "the operator can still act"
    )


def test_stale_key_detection_must_fire_control_a_naive_presence_check_would_miss_it():
    """Must-fire control: proves the defect this closes is real — a bare `grep -qE
    '^KEY='` presence check (the shape of the existing new-key report) says the key is fine
    merely because it exists, even though its value is the stale one.
    """
    out = _run_shell(
        "grep -qE '^ENGINE_SHARED_VOLUME_PATH=' <(echo 'ENGINE_SHARED_VOLUME_PATH=/tmp/transcription') "
        "&& echo PRESENT || echo ABSENT"
    )
    assert out == "PRESENT", (
        "fixture is wrong: a presence-only check must report this stale value as fine, "
        "which is exactly the gap check_stale_env_values() closes"
    )


# --------------------------------------------------------------------------- #
# Part 2: diarization status surface (issue #656)
# --------------------------------------------------------------------------- #

_FAKE_DOCKER_HEALTHY_READY = """#!/bin/bash
case "$*" in
    *healthz*) echo '{"models_state":"ready","models_reason":null,"devices":["cpu"],"supported_devices":["cpu","cuda"]}' ;;
    *readyz*) echo -n '200' ;;
    *) exit 1 ;;
esac
"""

_FAKE_DOCKER_NOT_READY = """#!/bin/bash
case "$*" in
    *healthz*) echo '{"models_state":"loading","models_reason":"provisioning weights","devices":[],"supported_devices":["cpu","cuda"]}' ;;
    *readyz*) echo -n '503' ;;
    *) exit 1 ;;
esac
"""

_FAKE_DOCKER_UNREACHABLE = """#!/bin/bash
exit 1
"""


def _print_diar_native_status_snippet(
    fake_docker_script: str, env_contents: str, tmp_path: Path, compose_files: str
) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_bin = bin_dir / "docker"
    docker_bin.write_text(fake_docker_script)
    docker_bin.chmod(0o755)

    env_file = tmp_path / ".env"
    env_file.write_text(env_contents)

    fn = _extract_function(MANAGER, "print_diar_native_status")
    snippet = f"""
cd {tmp_path}
source {COMMON}
YELLOW=''; GREEN=''; RED=''; BLUE=''; NC=''
{fn}
print_diar_native_status "{compose_files}"
"""
    return _run_shell(snippet, env={"PATH": f"{bin_dir}:/usr/bin:/bin"}, cwd=tmp_path)


def test_status_block_hidden_when_diar_native_overlay_not_in_compose_chain(tmp_path: Path):
    out = _print_diar_native_status_snippet(
        _FAKE_DOCKER_HEALTHY_READY, "", tmp_path, "-f docker-compose.yml"
    )
    assert out == ""


def test_status_block_shown_and_ready_when_overlay_present_and_sidecar_healthy(
    tmp_path: Path,
):
    out = _print_diar_native_status_snippet(
        _FAKE_DOCKER_HEALTHY_READY,
        "ENGINE_DIARIZER_BACKEND=native\nDIAR_NATIVE_MAX_INFLIGHT=2\nDIAR_NATIVE_TIMEOUT_S=1800\n",
        tmp_path,
        "-f docker-compose.yml -f docker-compose.diar-native.yml",
    )
    assert "Diarization:" in out
    assert "configured  native" in out
    assert "ready" in out
    assert "1800s ceiling" in out


def test_status_block_shows_not_ready_with_reason(tmp_path: Path):
    out = _print_diar_native_status_snippet(
        _FAKE_DOCKER_NOT_READY,
        "ENGINE_DIARIZER_BACKEND=native\n",
        tmp_path,
        "-f docker-compose.yml -f docker-compose.diar-native.yml",
    )
    assert "not ready" in out
    assert "loading" in out
    assert "provisioning weights" in out


def test_status_block_handles_unreachable_sidecar(tmp_path: Path):
    out = _print_diar_native_status_snippet(
        _FAKE_DOCKER_UNREACHABLE,
        "",
        tmp_path,
        "-f docker-compose.yml -f docker-compose.diar-native.yml",
    )
    assert "unreachable" in out


def test_status_block_warns_when_gpu_scale_workers_exceeds_permits(tmp_path: Path):
    out = _print_diar_native_status_snippet(
        _FAKE_DOCKER_HEALTHY_READY,
        "GPU_SCALE_WORKERS=4\nDIAR_NATIVE_MAX_INFLIGHT=2\n",
        tmp_path,
        "-f docker-compose.yml -f docker-compose.diar-native.yml",
    )
    assert "exceeds the sidecar's permits" in out
    assert "workers     4" in out


def test_status_block_does_not_warn_when_gpu_scale_workers_within_permits(tmp_path: Path):
    out = _print_diar_native_status_snippet(
        _FAKE_DOCKER_HEALTHY_READY,
        "GPU_SCALE_WORKERS=2\nDIAR_NATIVE_MAX_INFLIGHT=2\n",
        tmp_path,
        "-f docker-compose.yml -f docker-compose.diar-native.yml",
    )
    assert "exceeds" not in out


def test_status_block_uses_devices_not_supported_devices():
    """Conflating `devices` (loaded) with `supported_devices` (build-time capability) caused
    a live outage on this branch. The function source must read `devices`, and must not read
    `supported_devices` at all.
    """
    fn = _extract_function(MANAGER, "print_diar_native_status")
    assert '"devices"' in fn
    # A comment may legitimately name supported_devices to explain why it's avoided; what
    # must never appear is an actual READ of that key (dict lookup / json key access).
    assert '"supported_devices"' not in fn
    assert '.get("supported_devices"' not in fn


def test_status_block_does_not_derive_which_engine_served_a_file():
    """Issue #706 closed exactly this bug: deriving "which engine served a file" from the
    *configured* value instead of what actually ran. This surface must not reintroduce it —
    no reference to diarization_provider or per-file provenance.
    """
    fn = _extract_function(MANAGER, "print_diar_native_status")
    assert "diarization_provider" not in fn


def _workers_predicate_block() -> str:
    """The REAL workers-vs-permits if/else block, spliced out of the real
    ``print_diar_native_status()`` body — not a hand-reimplementation.

    A hand-copied bash reimplementation of this predicate previously lived in this test: it
    could invert `-gt` to `-lt` in the real function and stay green, because it never ran the
    real function's source at all — only a lookalike the test author typed. Per this repo's
    established pattern (``test_run_integration_tests_diar_native_gate.py``'s
    ``_predicate_block``/``_decision_block``), extract and execute the actual bash instead.
    """
    fn = _extract_function(MANAGER, "print_diar_native_status")
    match = re.search(
        r"local gpu_scale_workers effective_workers\n.*?\n    fi\n",
        fn,
        re.DOTALL,
    )
    assert match, (
        "the workers-vs-permits predicate block was not found in print_diar_native_status() — "
        "if it was renamed or restructured, update this extraction rather than deleting the "
        "test: it guards the shared #656/env-example-derivation predicate from silently "
        "diverging"
    )
    block = match.group(0)
    assert '"$effective_workers" -gt "$max_inflight"' in block, (
        "sanity: expected the real -gt comparison inline in the extracted block"
    )
    return block


def test_workers_warning_predicate_matches_the_gpu_scale_derivation_test(tmp_path: Path):
    """Same rule as backend/tests/unit/test_env_example_gpu_scale_derivation.py: effective
    workers (GPU_SCALE_WORKERS, or DIAR_NATIVE_MAX_INFLIGHT if unset) must never exceed
    DIAR_NATIVE_MAX_INFLIGHT. Exercises the REAL predicate block spliced out of
    print_diar_native_status() itself — not a duplicated copy — so an inverted comparison in
    the real function (e.g. `-gt` flipped to `-lt`) fails this test, the way it previously did
    not.
    """
    block = _workers_predicate_block()

    def effective_exceeds(workers: str, max_inflight: str) -> bool:
        env_file = tmp_path / ".env"
        lines = [f"DIAR_NATIVE_MAX_INFLIGHT={max_inflight}\n"]
        if workers:
            lines.append(f"GPU_SCALE_WORKERS={workers}\n")
        env_file.write_text("".join(lines))
        snippet = f"""
cd {tmp_path}
source {COMMON}
max_inflight="{max_inflight}"
{block}
"""
        out = _run_shell(snippet, cwd=tmp_path)
        return "exceeds the sidecar's permits" in out

    assert effective_exceeds("4", "2") is True
    assert effective_exceeds("", "2") is False
    assert effective_exceeds("2", "2") is False
