"""`./opentr.sh --gpu-device N` must move EVERY GPU this stack reserves.

Before this flag there was no way to retarget a GPU from the CLI at all.
`docker-compose.gpu.yml` interpolates ``device_ids: ['${GPU_DEVICE_ID:-0}']``, and
``opentr.sh`` opens with ``set -a; source ./.env``, so a pre-exported
``GPU_DEVICE_ID=2 ./opentr.sh start dev`` is overwritten by `.env` before any
compose file is read. The only workaround left was editing `.env` — which is
shared with the live stack, and in a git worktree is a copy of (or a symlink to)
the very same file. That is not hypothetical: a worktree `.env` edit here
silently moved the LIVE stack's transcription worker onto another card.

The flag therefore has one hard requirement beyond "it parses": it must move
**all** of the device ids compose interpolates for our own AI workers. A flag
that repoints the default worker and leaves the redaction / scaled / split
workers on the old card is worse than no flag — it looks like it worked, and
then two stacks fight over one GPU. That is what
:func:`test_every_worker_device_id_compose_interpolates_is_moved` enforces, and
it derives the expected set from **the compose files**, not from ``opentr.sh`` —
otherwise the test would agree with the script by construction and a sixth
worker overlay added later would sail through.

Two device ids are deliberately excluded, and the exclusions are asserted to be
documented rather than merely present:

* ``LLM_TEST_GPU_DEVICE_ID`` — ``--with-llm-test`` pins a multi-GB LLM to a
  *different* card on purpose so it never contends with transcription. Moving it
  with the flag would cause the exact OOM that separation exists to prevent.
* ``GPU_CLUSTERING_DEVICE`` and the container-side copy of ``GPU_DEVICE_ID`` —
  read inside the container from ``env_file: .env``, never interpolated by
  compose, so no shell export can reach them at all.

The static tests encode the contract. :func:`test_the_flag_beats_a_dotenv_value`
and its control reproduce the original defect end to end in a sandbox checkout
with a real `.env`, and
:func:`test_the_override_reaches_the_process_that_runs_compose` asserts the
exported value is actually inherited by the child process — being set in
``opentr.sh``'s own shell would prove nothing about what compose interpolates.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENTR = REPO_ROOT / "opentr.sh"
COMMON = REPO_ROOT / "scripts" / "common.sh"

# Device ids that exist for something other than an OpenTranscribe AI worker, and
# so must NOT follow --gpu-device. Each maps to the reason the flag documents.
EXCLUDED_DEVICE_VARS = {
    "LLM_TEST_GPU_DEVICE_ID": "--with-llm-test keeps its own card on purpose",
}

pytestmark = pytest.mark.skipif(
    not OPENTR.exists() or not COMMON.exists(),
    reason="opentr.sh / scripts/common.sh not present in this checkout",
)


def _script() -> str:
    return OPENTR.read_text(encoding="utf-8")


def _function_body(text: str, name: str) -> str:
    """Source of one top-level ``name() { ... }`` block, closing brace included."""
    start = text.index(f"\n{name}() {{")
    end = text.index("\n}\n", start)
    return text[start:end]


def _show_help() -> str:
    return _function_body(_script(), "show_help")


def _declared_device_vars() -> list[str]:
    """The GPU_DEVICE_VARS array `--gpu-device` iterates over."""
    match = re.search(r"^GPU_DEVICE_VARS=\((.*?)^\)", _script(), re.S | re.M)
    assert match, "opentr.sh no longer declares a GPU_DEVICE_VARS array"
    return re.findall(r"^\s*([A-Z][A-Z0-9_]*)", match.group(1), re.M)


def _composed_overlays() -> set[Path]:
    """Compose files opentr.sh can put into a start/reset chain.

    Scoping matters: `docker-compose.benchmark.yml` also reserves a GPU
    (`DIARIZATION_PROBE_GPU`), but it is a one-shot probe run by hand and never
    appears in a `COMPOSE_FILES=` assignment, so `--gpu-device` has nothing to say
    about it. Only *which files* comes from opentr.sh — which device ids those
    files reserve, the part that actually drifts, still comes from the files.
    """
    names: set[str] = set()
    for assignment in re.findall(r'COMPOSE_FILES="[^"]*"', _script()):
        names.update(re.findall(r"-f\s+(docker-compose[\w.-]*\.yml)", assignment))
    return {REPO_ROOT / name for name in names if (REPO_ROOT / name).exists()}


def _interpolated_device_vars() -> set[str]:
    """Every variable those compose files interpolate into a `device_ids:` entry.

    The compose files are the authority on which cards this stack reserves —
    reading the list out of opentr.sh instead would make the test agree with the
    script by construction.
    """
    found: set[str] = set()
    for compose in sorted(_composed_overlays()):
        for line in compose.read_text(encoding="utf-8").splitlines():
            if "device_ids" not in line or line.lstrip().startswith("#"):
                continue
            found.update(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", line))
    return found


def test_the_compose_scan_finds_the_device_ids_at_all():
    """Guard the guard: an empty scan would make the contract test vacuous."""
    overlays = {path.name for path in _composed_overlays()}
    assert "docker-compose.gpu.yml" in overlays, overlays
    assert "docker-compose.benchmark.yml" not in overlays, (
        "the hand-run benchmark probe is not part of a start/reset chain"
    )
    interpolated = _interpolated_device_vars()
    assert "GPU_DEVICE_ID" in interpolated, interpolated
    assert len(interpolated) >= 5, interpolated


def test_every_worker_device_id_compose_interpolates_is_moved():
    """--gpu-device moves all of them, or it is a trap.

    Fails both ways on purpose: a new worker overlay whose device id is not in
    GPU_DEVICE_VARS (the "moves one worker, leaves five behind" bug), and a
    variable listed in GPU_DEVICE_VARS that no compose file actually reads (dead
    configuration that reads as coverage).
    """
    expected = _interpolated_device_vars() - set(EXCLUDED_DEVICE_VARS)
    declared = set(_declared_device_vars())

    assert declared == expected, (
        "the set of GPU device ids --gpu-device moves has drifted from what the "
        "compose files interpolate.\n"
        f"  reserved by compose but NOT moved: {sorted(expected - declared)}\n"
        f"  moved but read by no compose file: {sorted(declared - expected)}\n"
        "Add it to GPU_DEVICE_VARS in opentr.sh, or — if it must stay put — add it "
        "to EXCLUDED_DEVICE_VARS here WITH the reason, and say so in show_help()."
    )


def test_the_deliberate_exclusions_are_documented_not_just_omitted():
    """An undocumented omission is indistinguishable from an oversight."""
    help_text = _show_help()
    for name in EXCLUDED_DEVICE_VARS:
        assert name in help_text, (
            f"{name} is excluded from --gpu-device but show_help() never mentions it; "
            "a user cannot tell that from a bug."
        )
    # The two vars no shell export can reach are the other half of the promise.
    assert "GPU_CLUSTERING_DEVICE" in help_text
    assert "env_file" in help_text


def test_show_help_documents_the_flag_and_the_vars_it_moves():
    help_text = _show_help()
    assert "--gpu-device" in help_text, "show_help() does not document --gpu-device"
    for name in _declared_device_vars():
        assert name in help_text, f"--gpu-device moves {name} without documenting it"


@pytest.mark.parametrize("function_name", ["start_app", "reset_and_init"])
def test_both_start_and_reset_accept_the_flag(function_name):
    """`reset` documents itself as taking the same options as `start`.

    An arm present in one and missing from the other means `reset --gpu-device 2`
    falls through to the catch-all, prints "Unknown flag", and then resets the
    stack on whatever card `.env` names — silently, having been asked not to.
    """
    body = _function_body(_script(), function_name)
    assert "--gpu-device)" in body, f"{function_name} does not parse --gpu-device"
    assert "apply_gpu_device_override" in body, (
        f"{function_name} parses --gpu-device but never applies it"
    )


# ---------------------------------------------------------------------------
# Behavioural: a sandbox checkout with its own .env, stub docker/nvidia-smi.
# ---------------------------------------------------------------------------

DOTENV_DEVICE_ID = "7"  # what the sandbox .env pins, i.e. the value to beat


@dataclass(frozen=True)
class Sandbox:
    """A throwaway checkout plus the stub binaries opentr.sh will find on PATH."""

    checkout: Path
    stubs: Path
    docker_env: Path


def _write_stub(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/bash\n{body}\n")
    path.chmod(0o755)


@pytest.fixture
def sandbox(tmp_path):
    """A minimal checkout: opentr.sh, common.sh, a .env, and stubbed binaries.

    Hermetic on purpose — the real repo's `.env` is the live stack's, and the
    whole point of the flag is not to touch it.
    """
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "opentr.sh").write_text(_script())
    (checkout / "scripts" / "common.sh").write_text(COMMON.read_text(encoding="utf-8"))
    (checkout / "VERSION").write_text("0.0.0-test\n")
    # Only the overlays start_app probes with `[ -f ... ]` need to exist.
    (checkout / "docker-compose.yml").write_text("services: {}\n")
    (checkout / "docker-compose.override.yml").write_text("services: {}\n")
    (checkout / "docker-compose.gpu.yml").write_text("services: {}\n")
    # The .env that clobbers a pre-exported value — the defect being fixed.
    (checkout / ".env").write_text(f"GPU_DEVICE_ID={DOTENV_DEVICE_ID}\n")

    stubs = tmp_path / "stubs"
    stubs.mkdir()
    # 3 GPUs, so index 3 is out of range and index 1 is valid.
    _write_stub(
        stubs / "nvidia-smi",
        'if [ "$1" = "-L" ]; then\n'
        '  echo "GPU 0: Stub A (UUID: GPU-0)"\n'
        '  echo "GPU 1: Stub B (UUID: GPU-1)"\n'
        '  echo "GPU 2: Stub C (UUID: GPU-2)"\n'
        "fi\nexit 0",
    )
    # `docker info` mentioning nvidia selects the Container Toolkit branch, and
    # the recorded env proves what a real `docker compose` would have inherited.
    _write_stub(
        stubs / "docker",
        f'echo "GPU_DEVICE_ID=${{GPU_DEVICE_ID:-unset}}" >> {tmp_path}/docker-env.txt\n'
        "echo nvidia\nexit 0",
    )
    return Sandbox(checkout=checkout, stubs=stubs, docker_env=tmp_path / "docker-env.txt")


def _run(sandbox_dir: Sandbox, *args, extra_env=None):
    env = {"PATH": f"{sandbox_dir.stubs}:{os.environ['PATH']}", "HOME": str(sandbox_dir.checkout)}
    env.update(extra_env or {})
    proc = subprocess.run(
        ["bash", "opentr.sh", *args],
        cwd=sandbox_dir.checkout,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    return proc, proc.stdout + proc.stderr


def _reservations(output: str) -> dict[str, str]:
    """Parse the `GPU device reservations` block the dry run prints.

    Matches any declared device var plus the *_DEVICE_ID family — the old
    suffix-only pattern silently never matched `DIAR_NATIVE_GPU`, so the
    moved-to-GPU-N assertion for it could not fail OR pass truthfully."""
    names = "|".join(sorted(set(_declared_device_vars()), key=len, reverse=True))
    return dict(re.findall(rf"\b({names}|[A-Z][A-Z0-9_]*_DEVICE_ID)=(\d+)", output))


def test_the_control_a_pre_exported_value_is_still_clobbered_by_dotenv(sandbox):
    """The defect itself, reproduced — without it the next test proves nothing.

    `GPU_DEVICE_ID=1 ./opentr.sh start dev` does NOT run on GPU 1. If this ever
    starts passing on its own, the `source ./.env` was reordered and the flag may
    no longer be needed.
    """
    _, output = _run(
        sandbox, "start", "dev", "--no-nas", "--dry-run", extra_env={"GPU_DEVICE_ID": "1"}
    )
    assert _reservations(output)["GPU_DEVICE_ID"] == DOTENV_DEVICE_ID, output


def test_the_flag_beats_a_dotenv_value(sandbox):
    """...and the flag is applied after the sourcing, so it wins."""
    proc, output = _run(sandbox, "start", "dev", "--no-nas", "--dry-run", "--gpu-device", "1")
    assert proc.returncode == 0, output

    reservations = _reservations(output)
    for name in _declared_device_vars():
        assert reservations.get(name) == "1", f"{name} not moved to GPU 1:\n{output}"
    for name in EXCLUDED_DEVICE_VARS:
        assert reservations.get(name) != "1", (
            f"{name} must NOT follow --gpu-device ({EXCLUDED_DEVICE_VARS[name]}):\n{output}"
        )


def test_the_override_reaches_the_process_that_runs_compose(sandbox):
    """Exported, not merely assigned.

    `docker compose` interpolates `device_ids:` from its own environment, so a
    value set without `export` would move nothing. The stub `docker` records what
    it inherited; `--dry-run` stops before `up`, but `check_docker` has already
    run by then, in the same environment the real `up` would use.
    """
    _run(sandbox, "start", "dev", "--no-nas", "--dry-run", "--gpu-device", "1")
    recorded = sandbox.docker_env.read_text()
    assert "GPU_DEVICE_ID=1" in recorded, (
        f"the child process inherited {recorded!r} — --gpu-device did not export"
    )


def test_a_missing_value_fails_readably_instead_of_aborting_on_set_u(sandbox):
    """`set -u` turns a missing operand into an unbound-variable abort."""
    proc, output = _run(sandbox, "start", "dev", "--gpu-device")
    assert proc.returncode != 0, output
    assert "unbound variable" not in output, output
    assert "--gpu-device requires a GPU index" in output, output


def test_a_non_numeric_device_is_rejected(sandbox):
    proc, output = _run(sandbox, "start", "dev", "--no-nas", "--dry-run", "--gpu-device", "abc")
    assert proc.returncode != 0, output
    assert "non-negative integer" in output, output


def test_an_out_of_range_device_is_rejected_against_the_real_gpu_list(sandbox):
    """The stub host has 3 GPUs; index 3 would fail as an opaque daemon error."""
    proc, output = _run(sandbox, "start", "dev", "--no-nas", "--dry-run", "--gpu-device", "3")
    assert proc.returncode != 0, output
    assert "this host has 3 GPU(s)" in output, output


def test_gpu_split_is_warned_about_rather_than_silently_collapsed(sandbox):
    """--with-gpu-split exists to use two cards; --gpu-device collapses it to one."""
    proc, output = _run(
        sandbox,
        "start",
        "dev",
        "--no-nas",
        "--dry-run",
        "--with-gpu-split",
        "--gpu-device",
        "1",
    )
    assert proc.returncode == 0, output
    assert "collapses both onto GPU 1" in output, output
