"""A throwaway helper container must pin its image AND its platform (2026-09-07).

The release rehearsal refused all three scenarios at phase 00, on every volume, with:

    ⚠ could not probe opentranscribe_postgres_data for the live-data marker (docker rc=255)
    ✗ FATAL: opentranscribe_postgres_data carries the .opentranscribe-live-data marker

Both lines are about the same volume and they disagree. The truth was the first one: this
host's multi-arch build work had left a **linux/arm64** ``alpine:latest`` in the local image
store, ``docker run alpine`` on this amd64 host selected it, and ``test`` died with
``exec format error`` → rc 255. The probe never read a marker at all.

Two defects, and this module covers both:

1. **The probe was un-platformed** (and unpinned), so whatever a previous build left in the
   local store decided which binary ran. ``--platform`` makes it immune; the tag pin keeps
   ``:latest`` from reintroducing the drift.
2. **Fail-closed reported the wrong reason.** "could not check" was rendered as "carries the
   marker" — a positive claim about data the code had never read. That is the expensive half:
   it points the operator at the volume (delete it? is it really live?) instead of at the
   probe. The tri-state and ``gr_live_marker_reason`` exist to keep those distinguishable.

⚠️ Note what is NOT asserted: that an undetermined probe is allowed to proceed. It must still
refuse. Failing closed was correct throughout — only the *message* was wrong.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
GUARDRAILS = REPO_ROOT / "scripts" / "release-tests" / "lib" / "guardrails.sh"
SCRIPTS = REPO_ROOT / "scripts"

pytestmark = pytest.mark.skipif(
    not GUARDRAILS.is_file() or shutil.which("bash") is None,
    reason="scripts/release-tests/lib/guardrails.sh or bash is not present in this checkout",
)

# `docker run` ... eventually `alpine`, on one logical line, not inside a comment.
# Deliberately matches `postgres:17.5-alpine` too: the hazard is an un-platformed
# `docker run`, not the specific base image, and that site had the identical defect.
_BARE_ALPINE = re.compile(r"docker\s+run\b[^\n]*\balpine\b")

# A literal `--platform`, or the expansion of an args array built from the host
# platform. The array form is how a script pins the platform while still degrading
# gracefully when `docker version` cannot answer, so it must count as pinned.
_PLATFORM_PINNED = re.compile(r"--platform\b|platform_args\[@\]")


def _shell_lines(path: Path) -> list[tuple[int, str]]:
    """Logical shell lines: backslash continuations JOINED, comments dropped.

    ⚠️ Joining is not tidiness — a per-physical-line scanner is blind to the exact shape
    this repo actually had. All three offending sites in ``selftest-cleanup.sh`` were

        docker run -d --rm --name otselftest-pg-fake \\
            -v otselftest_pgdata:/var/lib/postgresql/data \\
            alpine sleep 60

    where ``docker run`` and ``alpine`` are on different physical lines, so neither line
    matches on its own. The first draft of this check reported a clean tree over them and
    only the guardrail self-test caught it.
    """
    out: list[tuple[int, str]] = []
    pending: list[str] = []
    start = 0
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not pending and line.strip().startswith("#"):
            continue
        if not pending:
            start = n
        if line.rstrip().endswith("\\"):
            pending.append(line.rstrip()[:-1])
            continue
        pending.append(line)
        out.append((start, " ".join(part.strip() for part in pending)))
        pending = []
    if pending:
        out.append((start, " ".join(part.strip() for part in pending)))
    return out


# ----------------------------------------------------------------- static: no bare alpine


def test_no_script_runs_alpine_without_pinning_the_platform():
    """The reintroduction guard. One un-platformed site is enough to wedge the rehearsal."""
    offenders: list[str] = []
    for script in sorted(SCRIPTS.rglob("*.sh")):
        for n, line in _shell_lines(script):
            if _BARE_ALPINE.search(line) and not _PLATFORM_PINNED.search(line):
                offenders.append(f"{script.relative_to(REPO_ROOT)}:{n}: {line.strip()}")

    assert not offenders, (
        "these `docker run ... alpine` calls do not pass --platform, so a stale local image "
        "of the wrong architecture is selected instead and the command dies with "
        "`exec format error` (rc=255):\n  " + "\n  ".join(offenders)
    )


def test_the_scanner_would_actually_fire(tmp_path: Path):
    """Guard the guard: a detector that matches nothing is a clean report.

    The real check passing is only meaningful if the pattern can fire at all.
    """
    victim = tmp_path / "bad.sh"
    victim.write_text("docker run --rm -v foo:/probe alpine test -e /probe/x\n", encoding="utf-8")
    hits = [line for _, line in _shell_lines(victim) if _BARE_ALPINE.search(line)]
    assert hits, "the bare-alpine pattern no longer matches the exact line that caused the bug"

    fixed = tmp_path / "good.sh"
    fixed.write_text(
        "docker run --rm --platform linux/amd64 -v foo:/probe alpine:3.20 test -e /probe/x\n",
        encoding="utf-8",
    )
    assert not [
        line
        for _, line in _shell_lines(fixed)
        if _BARE_ALPINE.search(line) and not _PLATFORM_PINNED.search(line)
    ], "the fixed form is still reported — the check would be unsatisfiable"


def test_the_scanner_sees_a_continued_docker_run(tmp_path: Path):
    """The real-world shape: `docker run` and the image on different physical lines.

    This is not hypothetical — it is how all three offending sites in
    selftest-cleanup.sh were written, and a per-physical-line scanner reported them clean.
    """
    victim = tmp_path / "continued.sh"
    victim.write_text(
        "docker run -d --rm --name fake \\\n"
        "    -v vol:/var/lib/postgresql/data \\\n"
        "    alpine sleep 60\n",
        encoding="utf-8",
    )
    offenders = [
        line
        for _, line in _shell_lines(victim)
        if _BARE_ALPINE.search(line) and not _PLATFORM_PINNED.search(line)
    ]
    assert len(offenders) == 1, (
        "a `docker run ... \\` continuation with the image on a later line must be reported "
        f"exactly once — the exact blind spot that let three real sites through; got {offenders}"
    )
    # The joined logical line must carry BOTH physical halves, or the scanner is matching
    # something other than the continuation it claims to have reassembled.
    assert "docker run" in offenders[0] and "alpine sleep 60" in offenders[0], (
        f"the reported line is not the reassembled continuation: {offenders[0]!r}"
    )


def test_the_probe_image_is_pinned_not_latest():
    source = GUARDRAILS.read_text(encoding="utf-8")
    match = re.search(r'GR_PROBE_IMAGE="\$\{GR_PROBE_IMAGE:-([^}"]+)\}"', source)
    assert match, "GR_PROBE_IMAGE is gone — the probe image is no longer pinned in one place"
    assert not match.group(1).endswith(":latest"), (
        f"GR_PROBE_IMAGE is {match.group(1)!r}: `:latest` is what let a linux/arm64 image "
        "left over from a multi-arch build become the probe on an amd64 host"
    )
    assert ":" in match.group(1), f"GR_PROBE_IMAGE {match.group(1)!r} carries no tag at all"


# -------------------------------------------------- behavioural: tri-state + honest message


def _drive(script_body: str) -> subprocess.CompletedProcess[str]:
    harness = textwrap.dedent(f"""
        set -uo pipefail
        gr_warn() {{ echo "WARN: $*"; }}
        gr_log()  {{ echo "LOG: $*"; }}
        gr_ok()   {{ echo "OK: $*"; }}
        gr_die()  {{ echo "DIE: $*"; exit 9; }}
        source "{GUARDRAILS}" 2>/dev/null || true
        {script_body}
    """)
    return subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, timeout=120, check=False
    )


def test_an_unrunnable_probe_returns_could_not_check_not_marker_present():
    """rc=2, distinct from rc=0. This is the bit that made the fatal message a lie.

    Forced deterministically by pointing GR_PROBE_IMAGE at an image that cannot run,
    which is exactly the shape the arm64 ``alpine:latest`` produced.
    """
    if shutil.which("docker") is None:
        pytest.skip("docker is not available on this host")

    # `if ...; then rc=0; else rc=$?; fi`, not `cmd; rc=$?` — guardrails.sh enables
    # `set -e` when sourced, so a bare failing call aborts this harness before the
    # echo. Drafting this test the wrong way reproduced the exact defect it covers.
    result = _drive(
        'if GR_PROBE_IMAGE="ot-nonexistent-probe-image:definitely-not-here" '
        "gr_volume_has_live_marker some-volume-that-need-not-exist; "
        'then rc=0; else rc=$?; fi; echo "RC=$rc"'
    )
    assert "RC=2" in result.stdout, (
        "an unrunnable probe must return 2 (COULD NOT CHECK), not 0 (marker present) — "
        f"0 is what made the guardrail assert a marker it had never read.\n{result.stdout}"
    )


def test_the_two_states_produce_different_operator_text():
    """A distinct return code that renders identically has fixed nothing."""
    result = _drive("gr_live_marker_reason 0; echo; gr_live_marker_reason 2; echo")
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(lines) >= 2, f"gr_live_marker_reason produced no text: {result.stdout!r}"
    marker_text, undetermined_text = lines[0], lines[1]

    assert marker_text != undetermined_text, (
        "an undetermined probe and a positive marker find render identically, which is the "
        "original defect: the operator is told the volume carries a marker that was never read"
    )
    assert "carries" in marker_text
    assert "COULD NOT BE CHECKED" in undetermined_text, (
        f"the undetermined case must say so plainly; got {undetermined_text!r}"
    )


def test_every_call_site_refuses_on_could_not_check_as_well_as_on_marker_present():
    """Failing closed was always right — the tri-state must not have relaxed it.

    The probe is tri-state, so a site that merely branches on truthiness — ``if probe; then
    refuse; fi`` — reads rc=2 (COULD NOT CHECK) as FALSE and deletes an unprobeable volume.
    Every site must CAPTURE the status and refuse on anything that is not 1.

    ⚠️ The capture must itself be ``if probe; then rc=0; else rc=$?; fi`` and never
    ``probe; rc=$?``: this file runs under ``set -e``, where the bare form aborts the script
    before ``$?`` can be read. So the presence of ``if`` is REQUIRED here, not forbidden —
    what distinguishes right from wrong is whether the status lands in a variable.
    """
    source = GUARDRAILS.read_text(encoding="utf-8")

    call_lines = [
        line.strip()
        for line in source.splitlines()
        if "gr_volume_has_live_marker" in line
        and not line.lstrip().startswith("#")
        and "gr_volume_has_live_marker()" not in line
    ]
    assert len(call_lines) >= 4, (
        "the probe lost call sites — it guards stale-volume reset, two cleanup paths and "
        f"gr_assert_target_is_test_database; found {len(call_lines)}"
    )

    uncaptured = [line for line in call_lines if "marker_rc=$?" not in line]
    assert not uncaptured, (
        "these call sites branch on gr_volume_has_live_marker's truthiness instead of "
        "capturing it. rc=2 (COULD NOT CHECK) reads as FALSE there, so an unprobeable volume "
        "would be DELETED — the opposite of failing closed:\n  " + "\n  ".join(uncaptured)
    )

    unsafe = [line for line in call_lines if not line.startswith("if ")]
    assert not unsafe, (
        "these call sites capture the status with a bare `probe; rc=$?`. guardrails.sh runs "
        "under `set -e`, so a non-zero return ABORTS the script before $? is read. Use "
        "`if probe; then rc=0; else rc=$?; fi`:\n  " + "\n  ".join(unsafe)
    )
