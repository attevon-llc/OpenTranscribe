"""``--cpu`` / ``OPENTRANSCRIBE_FORCE_CPU``: the CPU-only opt-out on a host that has a GPU.

The opt-out exists for hosts where Docker advertises the NVIDIA runtime but GPU passthrough
is broken (typically WSL2 without a WSL-capable Windows driver): the GPU overlay makes
every container fail at start. Three scripts carry it, and each piece is driven for real
here rather than grepped:

* the installer (``setup-opentranscribe.sh``) accepts the flag and the env var, skips all
  GPU probing, and documents both in ``--help``;
* the management script (``opentranscribe.sh``) honours a one-off env var as well as the
  persisted ``FORCE_CPU_MODE`` in ``.env`` (the ``.env`` letter-case table lives in
  ``test_force_cpu_mode_case_insensitive.py``);
* the dev script (``opentr.sh``) accepts ``--cpu`` on ``start`` and ``reset`` and leaves
  the GPU overlay out even when the host reports the NVIDIA runtime.

``reset`` deletes data, so it is only checked statically; ``start`` runs as a dry run in
a sandbox checkout with stubbed ``docker`` / ``nvidia-smi``.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.unit._shell_extract import COMMON
from tests.unit._shell_extract import INSTALLER
from tests.unit._shell_extract import MANAGER
from tests.unit._shell_extract import REPO_ROOT
from tests.unit._shell_extract import extract_function

OPENTR = REPO_ROOT / "opentr.sh"

COLOURS = "YELLOW=''; GREEN=''; RED=''; BLUE=''; CYAN=''; NC=''\n"

_SCRUBBED_ENV = {
    "FORCE_CPU",
    "FORCE_CPU_MODE",
    "OPENTRANSCRIBE_FORCE_CPU",
    "OPENTRANSCRIBE_LITE",
    "OPENTRANSCRIBE_UNATTENDED",
    "OPENTRANSCRIBE_VERSION",
    "OPENTRANSCRIBE_BRANCH",
}


def _env(extra: dict[str, str] | None = None, path_prefix: Path | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED_ENV}
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}:{env.get('PATH', '/usr/bin:/bin')}"
    env.update(extra or {})
    return env


def _bash(
    body: str, cwd: Path, env: dict[str, str], args: list[str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", body, "bash", *(args or [])],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def _recording_stubs(tmp_path: Path) -> tuple[Path, Path]:
    """``nvidia-smi`` and ``docker`` stubs that log every call to one file."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    calls = tmp_path / "calls.log"
    for tool in ("nvidia-smi", "docker"):
        stub = stubs / tool
        stub.write_text(f'#!/bin/bash\necho "{tool} $*" >> "{calls}"\nexit 1\n', encoding="utf-8")
        stub.chmod(0o755)
    return stubs, calls


# --------------------------------------------------------------------------- #
# Installer: setup-opentranscribe.sh
# --------------------------------------------------------------------------- #


def _installer_without_main() -> str:
    """The installer's source with its trailing ``main "$@"`` invocation removed."""
    text = INSTALLER.read_text(encoding="utf-8")
    stripped, count = re.subn(r'^main "\$@"\s*$', "", text, flags=re.MULTILINE)
    assert count == 1, 'expected exactly one top-level `main "$@"` in the installer'
    return stripped


def _run_installer_parser(
    tmp_path: Path, args: list[str], extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the real installer through argument parsing, then stop before any side effect.

    ``resolve_install_ref`` (network) is neutralised and ``detect_platform`` (the first
    setup step after the post-parse notices) reports the parsed state and exits.
    """
    body = (
        _installer_without_main()
        + "\nresolve_install_ref() { :; }\n"
        + 'detect_platform() { echo "PARSED FORCE_CPU=$FORCE_CPU LITE_MODE=$LITE_MODE"; exit 0; }\n'
        + 'main "$@"\n'
    )
    return _bash(body, tmp_path, _env(extra_env), args)


def _parsed_force_cpu(proc: subprocess.CompletedProcess[str]) -> str:
    match = re.search(r"PARSED FORCE_CPU=(\S*)", proc.stdout)
    assert match, f"parser never reached setup:\n{proc.stdout}\n{proc.stderr}"
    return match.group(1)


def test_installer_help_lists_the_cpu_flag_and_env_var_and_exits_zero(tmp_path: Path):
    proc = _bash(INSTALLER.read_text(encoding="utf-8"), tmp_path, _env(), ["--help"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "--cpu" in proc.stdout
    assert "OPENTRANSCRIBE_FORCE_CPU" in proc.stdout
    for documented in (
        "--lite",
        "--version",
        "--branch",
        "OPENTRANSCRIBE_LITE",
        "OPENTRANSCRIBE_UNATTENDED",
        "OPENTRANSCRIBE_VERSION",
        "OPENTRANSCRIBE_BRANCH",
    ):
        assert documented in proc.stdout, f"--help does not mention {documented}"


def test_installer_rejects_an_unknown_argument_with_exit_2(tmp_path: Path):
    proc = _bash(INSTALLER.read_text(encoding="utf-8"), tmp_path, _env(), ["--no-such-flag"])
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "--no-such-flag" in proc.stderr


def test_installer_cpu_flag_sets_force_cpu(tmp_path: Path):
    proc = _run_installer_parser(tmp_path, ["--cpu"])
    assert _parsed_force_cpu(proc) == "true"
    assert "CPU-only install mode" in proc.stdout


@pytest.mark.parametrize("value", ["1", "yes", "anything", "false"])
def test_installer_any_non_empty_env_value_sets_force_cpu(tmp_path: Path, value: str):
    proc = _run_installer_parser(tmp_path, [], {"OPENTRANSCRIBE_FORCE_CPU": value})
    assert _parsed_force_cpu(proc) == "true"


def test_installer_empty_or_absent_env_var_leaves_force_cpu_false(tmp_path: Path):
    unset = _run_installer_parser(tmp_path, [])
    empty = _run_installer_parser(tmp_path, [], {"OPENTRANSCRIBE_FORCE_CPU": ""})
    assert _parsed_force_cpu(unset) == "false"
    assert _parsed_force_cpu(empty) == "false"
    assert "CPU-only install mode" not in unset.stdout


def test_installer_lite_does_not_print_the_cpu_only_notice(tmp_path: Path):
    """Lite implies CPU-only but has its own notice; the CPU one must not double up."""
    proc = _run_installer_parser(tmp_path, ["--lite"])
    assert _parsed_force_cpu(proc) == "true"
    assert "CPU-only install mode" not in proc.stdout


def _detect_hardware(tmp_path: Path, force_cpu: str) -> tuple[str, str]:
    stubs, calls = _recording_stubs(tmp_path)
    body = (
        COLOURS
        + 'DETECTED_PLATFORM="linux"; ARCH="x86_64"\n'
        + f'FORCE_CPU="{force_cpu}"\n'
        + extract_function(INSTALLER, "detect_hardware_acceleration")
        + "detect_hardware_acceleration\n"
        + 'echo "RESULT $DETECTED_DEVICE $COMPUTE_TYPE $BATCH_SIZE $USE_GPU_RUNTIME"\n'
    )
    proc = _bash(body, tmp_path, _env(path_prefix=stubs))
    recorded = calls.read_text(encoding="utf-8") if calls.exists() else ""
    return proc.stdout + proc.stderr, recorded


def test_forced_cpu_hardware_detection_probes_nothing_gpu_related(tmp_path: Path):
    output, recorded = _detect_hardware(tmp_path, "true")
    assert recorded == "", f"forced CPU still probed: {recorded!r}"
    assert "RESULT cpu int8 4 false" in output, output
    assert re.search(r"Detected \d+ CPU cores", output), output


def test_unforced_hardware_detection_does_reach_the_stubs(tmp_path: Path):
    """Control for the test above: the recording stubs are on PATH and do get called."""
    _, recorded = _detect_hardware(tmp_path, "false")
    assert "nvidia-smi" in recorded


# --------------------------------------------------------------------------- #
# Management script: opentranscribe.sh force_cpu_mode_requested
# --------------------------------------------------------------------------- #


def _manager_forces_cpu(tmp_path: Path, dotenv: str | None, env_var: str | None) -> bool:
    if dotenv is not None:
        (tmp_path / ".env").write_text(dotenv, encoding="utf-8")
    extra = {} if env_var is None else {"OPENTRANSCRIBE_FORCE_CPU": env_var}
    body = (
        "set -uo pipefail\n"
        + extract_function(COMMON, "read_env_value")
        + extract_function(MANAGER, "force_cpu_mode_requested")
        + "force_cpu_mode_requested && echo CPU || echo GPU\n"
    )
    proc = _bash(body, tmp_path, _env(extra))
    verdict = proc.stdout.strip()
    assert verdict in {"CPU", "GPU"}, proc.stdout + proc.stderr
    return verdict == "CPU"


def test_manager_env_var_forces_cpu_without_any_dotenv(tmp_path: Path):
    assert _manager_forces_cpu(tmp_path, None, "1") is True


def test_manager_env_var_beats_force_cpu_mode_false_in_dotenv(tmp_path: Path):
    assert _manager_forces_cpu(tmp_path, "FORCE_CPU_MODE=false\n", "1") is True


def test_manager_without_dotenv_or_env_var_keeps_the_gpu(tmp_path: Path):
    assert _manager_forces_cpu(tmp_path, None, None) is False


def test_manager_empty_env_var_does_not_force_cpu(tmp_path: Path):
    assert _manager_forces_cpu(tmp_path, None, "") is False


def test_manager_dotenv_without_the_key_keeps_the_gpu(tmp_path: Path):
    assert _manager_forces_cpu(tmp_path, "OTHER_SETTING=true\n", None) is False


# --------------------------------------------------------------------------- #
# Dev script: opentr.sh start/reset --cpu
# --------------------------------------------------------------------------- #


def _opentr_function(name: str) -> str:
    text = OPENTR.read_text(encoding="utf-8")
    start = text.index(f"\n{name}() {{")
    end = text.index("\n}\n", start)
    return text[start:end]


@pytest.mark.parametrize("function", ["start_app", "reset_and_init"])
def test_opentr_parser_has_a_cpu_arm_that_sets_cpu_flag(function: str):
    body = _opentr_function(function)
    assert re.search(r'^\s*CPU_FLAG=""\s*$', body, re.MULTILINE), (
        f"{function}() does not default CPU_FLAG (set -u would abort)"
    )
    arm = re.search(r"^\s*--cpu\)\s*\n(.*?);;", body, re.MULTILINE | re.DOTALL)
    assert arm, f"{function}() has no --cpu) case arm"
    assert re.search(r'CPU_FLAG="--cpu"', arm.group(1)), arm.group(0)


def _opentr_sandbox(tmp_path: Path) -> tuple[Path, Path]:
    """A minimal checkout on a host that reports a working NVIDIA runtime."""
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "opentr.sh").write_text(OPENTR.read_text(encoding="utf-8"), encoding="utf-8")
    (checkout / "scripts" / "common.sh").write_text(
        COMMON.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (checkout / "VERSION").write_text("0.0.0-test\n", encoding="utf-8")
    for overlay in ("docker-compose.yml", "docker-compose.override.yml", "docker-compose.gpu.yml"):
        (checkout / overlay).write_text("services: {}\n", encoding="utf-8")
    (checkout / ".env").write_text("GPU_DEVICE_ID=0\n", encoding="utf-8")

    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for tool, body in (
        ("nvidia-smi", 'if [ "$1" = "-L" ]; then echo "GPU 0: Stub (UUID: GPU-0)"; fi\nexit 0'),
        ("docker", "echo nvidia\nexit 0"),
    ):
        stub = stubs / tool
        stub.write_text(f"#!/bin/bash\n{body}\n", encoding="utf-8")
        stub.chmod(0o755)
    return checkout, stubs


def _dry_run_start(tmp_path: Path, *flags: str) -> tuple[list[str], str]:
    checkout, stubs = _opentr_sandbox(tmp_path)
    proc = subprocess.run(
        ["bash", "opentr.sh", "start", "dev", "--no-nas", "--dry-run", *flags],
        cwd=checkout,
        capture_output=True,
        text=True,
        env=_env({"HOME": str(checkout)}, path_prefix=stubs),
        timeout=120,
        check=False,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    block = output.split("Compose files:", 1)
    assert len(block) == 2, f"dry run printed no compose chain:\n{output}"
    chain = re.findall(r"^\s+- (\S+\.yml)\s*$", block[1], re.MULTILINE)
    assert chain, f"no compose files parsed from:\n{output}"
    return chain, output


def test_opentr_start_without_cpu_adds_the_gpu_overlay_on_an_nvidia_host(tmp_path: Path):
    """Control: the sandbox really is an NVIDIA host, so the next test can fail."""
    chain, _ = _dry_run_start(tmp_path)
    assert "docker-compose.gpu.yml" in chain


def test_opentr_start_cpu_skips_the_gpu_overlay_and_says_so(tmp_path: Path):
    chain, output = _dry_run_start(tmp_path, "--cpu")
    assert "docker-compose.gpu.yml" not in chain, chain
    assert "docker-compose.blackwell.yml" not in chain, chain
    assert "CPU-only mode enabled" in output, output
    assert "Detecting hardware configuration" not in output, (
        "--cpu must skip detect_and_configure_hardware entirely"
    )
