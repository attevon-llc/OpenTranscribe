"""The smoke stage's capability probe must read the CONTAINER's answer, not docker's chatter.

FOUND ON THE REAL v0.5.0 SMOKE RUN (2026-09-14). `85-smoke.sh`'s `assert_cuda_capability`
ran:

    run_out=$(docker run --rm --pull always "$img" \\
        python3 -c 'import torch; print(torch.version.cuda or "")' 2>&1)
    cuda_ver=$(printf '%s' "$run_out" | tr -d '\\r' | tail -n 1)

`--pull always` makes docker write `Status: Downloaded newer image for <img>` to stderr, and
`2>&1` folds that into the same capture as the container's stdout. For a CPU-only image
`torch.version.cuda` is empty, so python prints a bare newline, `$( )` strips trailing
newlines, and `tail -n 1` returns DOCKER'S STATUS LINE as the CUDA version.

Two consequences, and only the second one matters:

* **lite** — a correct CPU-only image reports a non-empty "version" and FAILS. Noisy but
  safe, and it is what exposed this.
* **full** — an image that shipped WITHOUT CUDA prints nothing, the status line survives as
  the last line, `-n "$cuda_ver"` is TRUE, and the stage reports
  `full/CUDA capability confirmed`. **That is issue #680's failure mode with a green tick**,
  produced by the function whose own header says it exists to prevent exactly it.

It had never fired because `lite` is published for the first time in v0.5.0, so this arm
never ran against a published image until the real release.

The fix pulls separately, captures stdout ONLY, and prefixes the value with a `CUDAVER:`
sentinel so that "the value is empty" and "the probe printed nothing" stop being the same
string — the #681 rule ("could not check" is not "checked and correct") applied to the one
place where an empty answer is also a legitimate one.

⚠️ This test drives the REAL function, extracted from the real script, against a fake
`docker` on PATH. The fake reproduces what the real python would print for BOTH the pre-fix
and post-fix probe commands, so the two must-fire cases below genuinely go red against
pre-fix HEAD rather than passing vacuously on a rewritten command.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
SMOKE = REPO_ROOT / "scripts" / "release" / "85-smoke.sh"

#: A fake `docker` faithful to the real one in the two respects this bug turns on:
#: pull progress goes to STDERR, and the container's answer goes to STDOUT. It emits what
#: the real python would print for whichever probe command it is handed, so the same fake
#: exercises pre-fix and post-fix code.
FAKE_DOCKER = """#!/usr/bin/env python3
import os, sys

argv = sys.argv[1:]
if argv and argv[0] == "--context":
    argv = argv[2:]
sub = argv[0] if argv else ""
mode = os.environ["FAKE_DOCKER_MODE"]
joined = " ".join(argv)

if sub == "pull":
    if mode == "pullfail":
        sys.stderr.write("Error response from daemon: manifest unknown\\n")
        sys.exit(1)
    print("sha256:deadbeefcafe")
    sys.exit(0)

if sub == "run":
    # THE CRUX: docker announces the pull on stderr whenever --pull is requested.
    if "--pull" in argv:
        sys.stderr.write("v0.5.0: Pulling from davidamacey/opentranscribe-backend-lite\\n")
        sys.stderr.write("Status: Downloaded newer image for davidamacey/x:v0.5.0\\n")

    if "provision-models" in joined:
        sys.exit(5)          # TOKEN_DENIED — the expected, healthy outcome
    if "onnxruntime" in joined:
        print("CPUExecutionProvider")
        sys.exit(0)

    if "torch" in joined:
        if mode == "runfail":
            sys.stderr.write("exec format error\\n")
            sys.exit(126)
        if mode == "silent":
            sys.exit(0)      # exits 0, prints NOTHING — "could not check"
        value = "12.8" if mode == "cuda" else ""
        # Emit what the real python would emit for the probe we were actually given,
        # so this fake is not calibrated to the post-fix command.
        print(("CUDAVER:" + value) if "CUDAVER" in joined else value)
        sys.exit(0)

sys.exit(0)
"""


def extract_function(name: str) -> str:
    """Pull one shell function verbatim out of the real script.

    Extracted rather than reimplemented for the reason `test_opentr_stop_container_scoping.py`
    gives: a reimplementation tests the copy, and would have passed throughout this bug.
    """
    lines = SMOKE.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start : end + 1])


def run_probe(tmp_path: Path, mode: str, expect_nonempty: str) -> subprocess.CompletedProcess:
    """Run the real `assert_cuda_capability` against the fake docker, and report its verdict."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "docker"
    fake.write_text(FAKE_DOCKER, encoding="utf-8")
    fake.chmod(0o755)

    harness = tmp_path / "harness.sh"
    harness.write_text(
        "set -uo pipefail\n"
        "RED=''; GREEN=''; YELLOW=''; BLUE=''; NC=''\n"
        "fail=0\n"
        "probe_ran=0;    probe_unrunnable=()\n"
        "cap_checked=0;  cap_mismatch=()\n"
        "diar_checked=0; diar_wrong=()\n"
        f"{extract_function('assert_cuda_capability')}\n"
        f'assert_cuda_capability "probe" "davidamacey/x:v0.5.0" "" "{expect_nonempty}" || true\n'
        # The verdict, as three orthogonal facts — matching the stage's own criteria split.
        'echo "VERDICT fail=${fail} ran=${probe_ran} '
        'unrunnable=${#probe_unrunnable[@]} mismatch=${#cap_mismatch[@]}"\n',
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["FAKE_DOCKER_MODE"] = mode
    return subprocess.run(
        ["bash", str(harness)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def verdict(proc: subprocess.CompletedProcess) -> dict[str, int]:
    line = next(ln for ln in (proc.stdout + proc.stderr).splitlines() if ln.startswith("VERDICT "))
    return {k: int(v) for k, v in (kv.split("=") for kv in line.split()[1:])}


@pytest.fixture(autouse=True)
def _needs_bash():
    if shutil.which("bash") is None:  # pragma: no cover - bash is present everywhere here
        pytest.skip("bash not available")


def test_the_script_and_function_still_exist():
    """GUARD THE GUARD: a rename would make every case below silently vacuous."""
    assert SMOKE.is_file(), f"missing {SMOKE}"
    body = extract_function("assert_cuda_capability")
    assert "docker" in body and len(body.splitlines()) > 10, "extracted an implausible body"


class TestTheDangerousDirection:
    """A CUDA image that lost its CUDA must FAIL. This is the one that matters."""

    def test_a_full_image_without_cuda_is_reported_as_a_mismatch(self, tmp_path: Path):
        """RED against pre-fix HEAD, where docker's status line stood in for the version.

        Pre-fix this printed `full/CUDA capability confirmed` — a published, broken,
        CUDA-less backend passing the gate built to catch it (#680).
        """
        v = verdict(run_probe(tmp_path, mode="cpu", expect_nonempty="true"))
        assert v["fail"] == 1, "a CUDA image reporting no CUDA must fail the stage"
        assert v["mismatch"] == 1, "it must be recorded as a capability MISMATCH"
        assert v["unrunnable"] == 0, "the probe ran fine — this is a real disagreement"

    def test_a_real_cuda_image_still_passes(self, tmp_path: Path):
        """MUST-STAY-CLEAN control: without it, a probe that always failed would pass above."""
        v = verdict(run_probe(tmp_path, mode="cuda", expect_nonempty="true"))
        assert v["fail"] == 0
        assert v["mismatch"] == 0
        assert v["ran"] == 1


class TestTheLiteDirection:
    def test_a_correct_cpu_only_image_passes(self, tmp_path: Path):
        """RED against pre-fix HEAD — this is the false alarm that exposed the bug.

        The v0.5.0 lite image is genuinely CPU-only (verified by hand against the published
        image: `torch.version.cuda` is empty) and the stage failed it anyway.
        """
        v = verdict(run_probe(tmp_path, mode="cpu", expect_nonempty="false"))
        assert v["fail"] == 0, "a genuinely CPU-only image must not be failed by pull chatter"
        assert v["mismatch"] == 0
        assert v["ran"] == 1

    def test_a_lite_image_that_shipped_cuda_still_fails(self, tmp_path: Path):
        """MUST-STAY-CLEAN: #680's other direction stays detectable."""
        v = verdict(run_probe(tmp_path, mode="cuda", expect_nonempty="false"))
        assert v["fail"] == 1
        assert v["mismatch"] == 1


class TestCouldNotCheckIsNotAPass:
    """#681's rule at the one place an empty answer is also a legitimate answer."""

    def test_a_probe_that_cannot_execute_is_unrunnable_not_cpu_only(self, tmp_path: Path):
        v = verdict(run_probe(tmp_path, mode="runfail", expect_nonempty="false"))
        assert v["fail"] == 1
        assert v["unrunnable"] == 1, "a wrong-arch ELF must not read as 'CPU-only confirmed'"
        assert v["ran"] == 0, "a probe that could not execute did not run"

    def test_a_silent_probe_is_unrunnable_not_cpu_only(self, tmp_path: Path):
        """Exit 0 with no output is 'could not check', and needs the sentinel to tell.

        This is precisely what the sentinel buys: without a `CUDAVER:` prefix, a probe that
        printed nothing and a CPU-only image that printed an empty version are the SAME
        captured string, and the lite branch reads both as a pass.
        """
        v = verdict(run_probe(tmp_path, mode="silent", expect_nonempty="false"))
        assert v["fail"] == 1
        assert v["unrunnable"] == 1
        assert v["ran"] == 0

    def test_a_failed_pull_is_unrunnable_not_cpu_only(self, tmp_path: Path):
        v = verdict(run_probe(tmp_path, mode="pullfail", expect_nonempty="false"))
        assert v["fail"] == 1
        assert v["unrunnable"] == 1
        assert v["ran"] == 0


def test_the_probe_does_not_merge_stderr_into_its_answer():
    """Pin the mechanism, not just the outcome.

    The outcome tests above all route through the fake docker; this one reads the source, so
    a future refactor that reintroduces `2>&1` on the probe fails here with a message naming
    the cause rather than as four mysterious verdict changes.
    """
    body = extract_function("assert_cuda_capability")
    probe = body[body.index("import torch") - 400 : body.index("import torch") + 200]
    assert "2>&1" not in probe, (
        "the capability probe merges stderr into stdout again — docker's "
        "'Status: Downloaded newer image for ...' will be read as torch.version.cuda"
    )
    assert "--pull always" not in probe, (
        "the probe pulls inline again; pull separately so docker's progress output "
        "cannot reach the capture"
    )
    assert "CUDAVER" in body, (
        "the sentinel is gone — an empty version and a silent probe are once again the "
        "same string, and 'could not check' reads as 'CPU-only confirmed' (#681)"
    )
