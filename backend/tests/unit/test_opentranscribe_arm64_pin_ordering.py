"""Issue #896's remaining gap: `arm64_deployment_preflight` reached the lite image pin
only on `start`, not on `restart`/`update`/`update-full`/`status`/`clean`/`shell`/
`health`/`logs`/`compose-files`/`stop`.

`get_compose_files()` calls `arm64_deployment_preflight` internally, but ALWAYS from
inside a `compose_files=$(get_compose_files)` command substitution — a subshell. Its
`export DEPLOYMENT_MODE=lite` dies with that subshell the instant it exits, before the
`docker compose` invocation that needs it (and before `pin_diar_native_image_for_lite`,
which reads `effective_deployment_mode()`) ever sees it. Only the `start)` arm additionally
called `arm64_deployment_preflight` as a plain statement BEFORE the pins, so it alone
worked; every other arm resolved DIAR_NATIVE_IMAGE against whatever DEPLOYMENT_MODE
happened to already be on disk.

The fix extracts a single `apply_deployment_pins()` wrapper (arm64 preflight, then the two
DIAR_NATIVE_IMAGE pins, then the gpu-split profile pin) and replaces all 12 duplicated
three/four-line pin blocks with one call each. This file is the ordering half of that fix;
`test_opentranscribe_lite_diar_native_image.py` covers `resolve_diar_native_downloader_image`
and `preflight_upgrade_env`'s parallel `effective_deployment_mode()` fix.

Runs the REAL shipped `opentranscribe.sh` through `subprocess`, with `docker`/`nvidia-smi`/
`uname` stubbed on `PATH` — same convention as `test_compose_file_selection.py`. The `docker`
stub logs the `DIAR_NATIVE_IMAGE` env var it saw on every `docker compose ...` invocation, so a
test can observe what the real command would have received in its own environment, without a
live daemon.
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
    not MANAGER.exists() or not COMMON.exists(),
    reason="opentranscribe.sh or scripts/common.sh not present in this checkout",
)

CUDA_REPO = "davidamacey/opentranscribe-backend:"
LITE_REPO = "davidamacey/opentranscribe-backend-lite:"


def _extract_function(script: Path, name: str) -> str:
    fn = subprocess.run(
        ["sed", "-n", f"/^{re.escape(name)}() {{/,/^}}/p", str(script)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert fn.strip(), f"{name}() not found in {script}"
    return fn


# --------------------------------------------------------------------------- #
# Harness: a fake curl-style install directory, driven through the REAL script
# --------------------------------------------------------------------------- #


def _make_arm64_install(
    tmp_path: Path,
    *,
    machine: str = "aarch64",
    env_lines: tuple[str, ...] = (),
) -> tuple[Path, Path]:
    """A fake install directory plus a stubbed `docker`/`nvidia-smi`/`uname` on PATH,
    so `./opentranscribe.sh <cmd>` runs as a real subprocess with no Docker daemon, no
    network, and a deterministic host architecture.

    Returns (install_dir, docker_invocations_log_file). The log records every
    `docker ...` invocation's DIAR_NATIVE_IMAGE and full argv, one line per call — so a
    test can see exactly what environment the real `docker compose` command would have
    run in, without needing a working daemon.
    """
    install = tmp_path / "install"
    install.mkdir(parents=True)
    (install / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (install / "docker-compose.prod.yml").write_text("services: {}\n", encoding="utf-8")

    scripts_dir = install / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "common.sh").write_bytes(COMMON.read_bytes())

    (install / "opentranscribe.sh").write_bytes(MANAGER.read_bytes())
    (install / "opentranscribe.sh").chmod(0o755)

    default_env = [
        "DEPLOYMENT_MODE=full",
        "OT_IMAGE_TAG=v0.5.0",
        "HUGGINGFACE_TOKEN=hf_dummy",
    ]
    (install / ".env").write_text("\n".join(default_env + list(env_lines)) + "\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_log = tmp_path / "docker-invocations.log"
    docker_log.write_text("", encoding="utf-8")

    docker_script = (
        "#!/bin/bash\n"
        'if [ "$1" = "info" ]; then echo " Runtimes: runc"; exit 0; fi\n'
        f'echo "DIAR_NATIVE_IMAGE=${{DIAR_NATIVE_IMAGE:-<unset>}} ARGV=$*" >> "{docker_log}"\n'
        "exit 0\n"
    )
    docker_bin = bin_dir / "docker"
    docker_bin.write_text(docker_script, encoding="utf-8")
    docker_bin.chmod(0o755)

    # No card at all: is_blackwell_gpu must fail closed. Not reached on these hosts
    # (nvidia runtime is reported absent above), but stubbed for safety.
    nvidia_smi = bin_dir / "nvidia-smi"
    nvidia_smi.write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
    nvidia_smi.chmod(0o755)

    uname_bin = bin_dir / "uname"
    uname_bin.write_text(
        "#!/bin/bash\n"
        f'if [ "$1" = "-m" ]; then echo "{machine}"; exit 0; fi\n'
        'exec /usr/bin/uname "$@"\n',
        encoding="utf-8",
    )
    uname_bin.chmod(0o755)

    return install, docker_log


def _run_cmd(
    tmp_path: Path,
    args: list[str],
    *,
    machine: str = "aarch64",
    env_lines: tuple[str, ...] = (),
) -> tuple[subprocess.CompletedProcess, str]:
    install, docker_log = _make_arm64_install(tmp_path, machine=machine, env_lines=env_lines)
    bin_dir = docker_log.parent / "bin"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
    }
    proc = subprocess.run(
        ["./opentranscribe.sh", *args],
        cwd=str(install),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    docker_calls = docker_log.read_text(encoding="utf-8")
    return proc, docker_calls


def _diar_native_image_for_compose_calls(docker_calls: str) -> str:
    """The DIAR_NATIVE_IMAGE seen by the first `docker compose ...` invocation.

    Deliberately skips any `docker run ...` invocation (e.g. fix_model_cache_permissions'
    permission-fix container, which some arms run BEFORE apply_deployment_pins) — only a
    `docker compose` call is what the real script's overlay/pin selection is about, and an
    unfiltered "first logged line" read would be poisoned by an earlier, unrelated `docker
    run` call that necessarily sees DIAR_NATIVE_IMAGE unset.
    """
    for line in docker_calls.splitlines():
        if line.startswith("DIAR_NATIVE_IMAGE=") and " ARGV=compose " in line:
            rest = line[len("DIAR_NATIVE_IMAGE=") :]
            return rest.split(" ARGV=", 1)[0]
    raise AssertionError(
        f"no `docker compose ...` invocation was logged; full log:\n{docker_calls!r}"
    )


# --------------------------------------------------------------------------- #
# Dynamic behaviour: real script, real subprocess, fake docker/uname/nvidia-smi
# --------------------------------------------------------------------------- #


def test_restart_on_arm64_addresses_the_lite_diar_native_image(tmp_path: Path):
    proc, docker_calls = _run_cmd(tmp_path, ["restart"], machine="aarch64")
    assert proc.returncode == 0, f"restart exited {proc.returncode}\nstderr:\n{proc.stderr}"
    image = _diar_native_image_for_compose_calls(docker_calls)
    assert image == f"{LITE_REPO}v0.5.0", (
        f"expected `restart` on an arm64 host to resolve the lite DIAR_NATIVE_IMAGE, got "
        f"{image!r}. Before the fix, `restart` never called arm64_deployment_preflight as a "
        "plain statement, so on an arm64 host that had not already run `start` the sidecar's "
        f"image pin fell through to whatever DEPLOYMENT_MODE was already on disk "
        f"(DEPLOYMENT_MODE=full here).\nstderr:\n{proc.stderr}"
    )
    assert not image.startswith(CUDA_REPO), f"arm64 resolved the CUDA repo: {image!r}"


def test_status_on_arm64_addresses_the_lite_diar_native_image(tmp_path: Path):
    proc, docker_calls = _run_cmd(tmp_path, ["status"], machine="aarch64")
    assert proc.returncode == 0, f"status exited {proc.returncode}\nstderr:\n{proc.stderr}"
    image = _diar_native_image_for_compose_calls(docker_calls)
    assert image == f"{LITE_REPO}v0.5.0", (
        f"expected `status` on an arm64 host to resolve the lite DIAR_NATIVE_IMAGE, got "
        f"{image!r}.\nstderr:\n{proc.stderr}"
    )


def test_start_on_arm64_still_addresses_the_lite_image_control(tmp_path: Path):
    """Positive control: `start` already called arm64_deployment_preflight explicitly
    before this fix, so it must stay green both before and after — this is the one arm
    the bug never affected.
    """
    proc, docker_calls = _run_cmd(tmp_path, ["start"], machine="aarch64")
    assert proc.returncode == 0, f"start exited {proc.returncode}\nstderr:\n{proc.stderr}"
    image = _diar_native_image_for_compose_calls(docker_calls)
    assert image == f"{LITE_REPO}v0.5.0", (
        f"`start` on arm64 must resolve the lite image (it always did): got {image!r}"
    )


def test_amd64_full_deployment_never_gets_the_lite_pin_control(tmp_path: Path):
    """Must-stay-clean: an amd64 host with DEPLOYMENT_MODE=full must never get the lite
    pin, on any arm — before or after this fix.
    """
    proc, docker_calls = _run_cmd(tmp_path, ["restart"], machine="x86_64")
    assert proc.returncode == 0, f"restart exited {proc.returncode}\nstderr:\n{proc.stderr}"
    image = _diar_native_image_for_compose_calls(docker_calls)
    assert image == "<unset>", (
        f"an amd64 full deployment must not resolve any DIAR_NATIVE_IMAGE pin from this "
        f"path, got {image!r}"
    )


# --------------------------------------------------------------------------- #
# Structural: every get_compose_files() consumer applies the pins first
# --------------------------------------------------------------------------- #


def _case_arms(source: str) -> dict[str, str]:
    """Split the `case "${1:-help}" in ... esac` dispatch block into {label: body}.

    Labels are 4-space-indented lines like `    start)` / `    backup|restore)`; nested
    case statements inside an arm's own body (e.g. `update)`'s argument parser) are
    indented further and so are never mistaken for a top-level label.
    """
    marker = '\ncase "${1:-help}" in\n'
    assert marker in source, (
        "the top-level case dispatch block was not found — this structural guard would "
        "scan nothing and every assertion below would be vacuous"
    )
    start = source.index(marker) + len(marker)
    end = source.index("\nesac", start)
    body = source[start:end]

    label_re = re.compile(r"^    ([A-Za-z][A-Za-z0-9_|-]*)\)$", re.MULTILINE)
    labels = list(label_re.finditer(body))
    assert labels, "no case-arm labels matched — the splitter regex is not reaching them"

    arms: dict[str, str] = {}
    for i, m in enumerate(labels):
        arm_start = m.end()
        arm_end = labels[i + 1].start() if i + 1 < len(labels) else len(body)
        arms[m.group(1)] = body[arm_start:arm_end]
    return arms


_EXEMPT_ARMS = {"backup|restore"}


def _strip_comment_lines(text: str) -> str:
    """Drop full-line comments before searching for call sites.

    Several arms mention `get_compose_files` (and, now, `apply_deployment_pins`) BY NAME
    in prose above the real call — e.g. `compose-files)`'s own header explains what
    `get_compose_files()` is for before the arm ever calls it. A raw substring search
    over the untouched arm body finds that prose mention first and reports a false
    "get_compose_files precedes apply_deployment_pins" offender. Real code in this file
    is never indented to start with `#`, so this is a safe, simple filter.
    """
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


def test_every_compose_chain_consumer_applies_the_deployment_pins_first():
    source = MANAGER.read_text(encoding="utf-8")
    arms = {label: _strip_comment_lines(body) for label, body in _case_arms(source).items()}

    consumers = [label for label, body in arms.items() if "get_compose_files" in body]
    assert len(consumers) >= 12, (
        f"expected at least 12 case arms to consume get_compose_files(), found only "
        f"{len(consumers)}: {consumers} — either the splitter is not matching correctly "
        "(making this whole test vacuous) or a consumer arm was removed"
    )

    offenders = []
    for label in consumers:
        if label in _EXEMPT_ARMS:
            continue
        body = arms[label]
        gcf_idx = body.index("get_compose_files")
        pins_idx = body.find("apply_deployment_pins")
        if pins_idx == -1 or pins_idx > gcf_idx:
            offenders.append(label)
    assert not offenders, (
        f"these case arms resolve get_compose_files() without an earlier "
        f"apply_deployment_pins call in the same arm: {offenders}"
    )

    # The exemption set must be EXACTLY {"backup|restore"} — a second silent exemption
    # (an arm that resolves compose_files with no apply_deployment_pins call ANYWHERE in
    # its body, not merely out of order) must fail loudly here rather than pass by
    # coincidence.
    fully_exempt = [label for label in consumers if "apply_deployment_pins" not in arms[label]]
    assert fully_exempt == sorted(_EXEMPT_ARMS), (
        f"expected exactly the documented exemption {_EXEMPT_ARMS}, found: {fully_exempt}"
    )


def test_apply_deployment_pins_runs_the_arm64_preflight_first():
    fn = _extract_function(MANAGER, "apply_deployment_pins")
    order = [
        "arm64_deployment_preflight",
        "pin_diar_native_image_for_blackwell",
        "pin_diar_native_image_for_lite",
        "pin_gpu_split_profile",
    ]
    indices = [fn.index(call) for call in order]
    assert indices == sorted(indices), (
        f"apply_deployment_pins must call {order} in that exact order — arm64 preflight "
        "first, since it is the only one that can change DEPLOYMENT_MODE and "
        f"pin_diar_native_image_for_lite's answer depends on having already seen that "
        f"change. Found body:\n{fn}"
    )


def test_the_preflight_export_control_dies_in_a_command_substitution():
    """Must-fire mechanism control: proves the underlying hazard this whole fix is about
    — an export made inside `$(...)` never reaches the calling shell.
    """
    out = subprocess.run(
        ["bash", "-c", 'out=$(export FOO=bar); echo "${FOO:-UNSET}"'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert out == "UNSET", "fixture is wrong: this must demonstrate the export not propagating"
