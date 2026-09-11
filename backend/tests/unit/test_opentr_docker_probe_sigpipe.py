"""``opentr.sh``'s docker probes must not invert when the daemon is still writing.

THE DEFECT THIS PINS
--------------------
``detect_and_configure_hardware`` decided whether this host has a usable GPU with::

    if docker info 2>/dev/null | grep -q nvidia; then

``opentr.sh`` runs under ``set -uo pipefail``. ``grep -q`` exits at its **first
match**, so while ``docker info`` is still writing it gets ``SIGPIPE`` and exits
141; ``pipefail`` then makes the *pipeline's* status 141 -- and a **match is read
as a non-match**. Being a race it is intermittent, which is why it survived: on
this host, idle, it inverted roughly once per 200-600 invocations (measured
``rc=141``), and a stack start happens right after a teardown, when the daemon is
busiest.

Observed cost, 2026-09-07 (``/tmp/ot-run-dev-tests.ahnET0/overlay-bringup.log``):
``./opentr.sh start dev`` printed ``NVIDIA GPU detected but Container Toolkit not
available``, dropped **both** ``docker-compose.gpu.yml`` and
``docker-compose.diar-native-gpu.yml``, and the whole dev stack -- ``celery-worker``
included -- came up with ``HostConfig.DeviceRequests: null``. The diar-native
sidecar served ~68 real ``/diarize`` requests logging ``device="cpu"``. Nothing
surfaced it, exactly as ``docker-compose.diar-native-gpu.yml``'s own header
predicts ("nothing would ever surface the mistake").

WHY THESE TESTS DRIVE THE REAL SCRIPT
-------------------------------------
The bug is a property of how bash composes ``pipefail`` with an early-exiting
reader. A reimplementation of the probe in Python cannot exhibit it, and a grep
for ``grep -q`` in the source is a spelling check rather than a behavioural one.
So the real ``opentr.sh`` functions are extracted and **executed** against a fake
``docker`` on ``PATH`` -- the same "drive the real loop" convention
``test_opentr_stop_container_scoping.py`` established.

Nothing here reaches a real daemon: every case puts a stub ``docker`` (and
``nvidia-smi``) earlier on ``PATH`` than the real ones, so these run in CI, where
docker is absent, and are safe with a live stack up.

The two execution-based must-fire cases write **more than one pipe buffer** (64 KiB)
after the matching line, purely so the race is deterministic in a test. Under the old
code they produce ``SIGPIPE`` and the inverted verdict every time; under the fixed
code the output is captured into a variable first, so there is no pipe left to break.

⚠️ **Do not read that 64 KiB as the threshold. IT IS NOT SIZE-DEPENDENT**, and this
file's earlier revision implied otherwise. Measured 2026-09-07: a **40-byte** producer
with 2 ms between its two writes inverted **300/300**; the same 40 bytes written
back-to-back inverted **0/3000**; and the real ``docker info`` -- **1,609 B, ~40x
UNDER the buffer** -- inverted **12/3000**. The variable is whether the producer still
has a write to make when the reader leaves, i.e. ELAPSED TIME, not bytes. The overflow
payload here buys determinism, not validity. Corollary for anyone tempted to declare a
site safe by running it: at ~1 in 250, a 400-iteration null result is expected roughly
45% of the time and proves nothing. Full table: ``scripts/CLAUDE.md``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENTR = REPO_ROOT / "opentr.sh"

#: Comfortably more than a Linux pipe buffer (64 KiB), so a reader that exits on
#: its first match leaves the writer with an unwritable pipe every time. The
#: matching token is emitted BEFORE this payload, which is what makes the old
#: pipeline report "no match" for output that plainly contains one.
_PIPE_BUFFER_BYTES = 64 * 1024
_OVERFLOW_BYTES = 16 * _PIPE_BUFFER_BYTES


def _function_source(text: str, name: str) -> str:
    """Source of one top-level ``name() { ... }`` block, closing brace included."""
    start = text.index(f"\n{name}() {{")
    end = text.index("\n}\n", start)
    return text[start : end + len("\n}\n")]


def _has_function(text: str, name: str) -> bool:
    return f"\n{name}() {{" in text


def _hardware_detection_source(script: Path) -> str:
    """``detect_and_configure_hardware`` plus the runtime probe it delegates to.

    Tolerant of the probe's absence on purpose: this same extraction has to work
    against the pre-fix revision (via ``git archive HEAD``) for the red-before-green
    check, where ``docker_runtime_has_nvidia`` does not exist yet and the pipeline
    is inline.
    """
    text = script.read_text(encoding="utf-8")
    parts = []
    if _has_function(text, "docker_runtime_has_nvidia"):
        parts.append(_function_source(text, "docker_runtime_has_nvidia"))
    parts.append(_function_source(text, "detect_and_configure_hardware"))
    return "\n".join(parts)


def _write_stub(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _stub_dir(tmp_path: Path, docker_body: str) -> Path:
    """A directory holding stub ``docker`` and ``nvidia-smi`` executables."""
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    _write_stub(stubs / "docker", docker_body)
    # detect_and_configure_hardware needs nvidia-smi to succeed (that is the branch
    # under test) and to answer the Blackwell compute-cap query with a non-12.x value.
    _write_stub(
        stubs / "nvidia-smi",
        "case \"$*\" in\n  *compute_cap*) echo '8.6' ;;\nesac\nexit 0",
    )
    return stubs


def _overflow_payload(tmp_path: Path) -> Path:
    payload = tmp_path / "overflow.txt"
    payload.write_bytes(b"x" * _OVERFLOW_BYTES)
    return payload


def _run_bash(script_body: str, stubs: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{stubs}{os.pathsep}{env.get('PATH', '')}"
    # A FORCE_CPU_MODE inherited from the developer's shell would short-circuit
    # detect_and_configure_hardware before the probe ever runs.
    env.pop("FORCE_CPU_MODE", None)
    return subprocess.run(
        ["bash", "-c", script_body],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )


def _detect_hardware(script: Path, stubs: Path) -> tuple[str, str]:
    """Run the real ``detect_and_configure_hardware`` under ``set -uo pipefail``.

    Returns ``(DOCKER_RUNTIME, combined output)``.
    """
    body = (
        "set -uo pipefail\n"
        f"{_hardware_detection_source(script)}\n"
        "detect_and_configure_hardware\n"
        'printf "RESULT_DOCKER_RUNTIME=[%s]\\n" "${DOCKER_RUNTIME:-}"\n'
    )
    proc = _run_bash(body, stubs)
    combined = proc.stdout + proc.stderr
    marker = "RESULT_DOCKER_RUNTIME=["
    assert marker in combined, f"detect_and_configure_hardware did not complete:\n{combined}"
    runtime = combined.split(marker, 1)[1].split("]", 1)[0]
    return runtime, combined


def _sidecar_probe(script: Path, stubs: Path) -> tuple[int, str]:
    """Run the real ``diar_native_container_present`` under ``set -uo pipefail``."""
    text = script.read_text(encoding="utf-8")
    body = (
        "set -uo pipefail\n"
        f"{_function_source(text, 'diar_native_container_present')}\n"
        "diar_native_container_present\n"
        'printf "RESULT_RC=[%s]\\n" "$?"\n'
    )
    proc = _run_bash(body, stubs)
    combined = proc.stdout + proc.stderr
    marker = "RESULT_RC=["
    assert marker in combined, f"diar_native_container_present did not complete:\n{combined}"
    return int(combined.split(marker, 1)[1].split("]", 1)[0]), combined


# --------------------------------------------------------------------------- #
# docker info -> DOCKER_RUNTIME
# --------------------------------------------------------------------------- #


def test_a_docker_info_that_outwrites_a_pipe_buffer_still_reports_the_nvidia_runtime(
    tmp_path: Path,
) -> None:
    """MUST-FIRE case for the defect.

    ``docker info`` names the nvidia runtime on its first line and then keeps
    writing past a pipe buffer. That is a GPU host by any reading -- but a
    ``| grep -q nvidia`` reader exits at the match, the writer takes SIGPIPE, and
    ``pipefail`` reports the whole pipeline as failed, i.e. "no nvidia".
    """
    payload = _overflow_payload(tmp_path)
    stubs = _stub_dir(
        tmp_path,
        'if [ "${1:-}" = "info" ]; then\n'
        "  printf ' Runtimes: io.containerd.runc.v2 nvidia runc\\n'\n"
        # `exec` so the stub's OWN exit status is cat's -- including 141 when the
        # reader has already gone away. Without it the stub would swallow the
        # SIGPIPE and the case would silently stop reproducing the bug.
        f'  exec cat "{payload}"\n'
        "fi\n"
        "exit 0",
    )

    runtime, output = _detect_hardware(OPENTR, stubs)

    assert runtime == "nvidia", (
        "docker info named the nvidia runtime, but the probe concluded otherwise -- "
        "this is the `grep -q` + pipefail SIGPIPE inversion that dropped "
        "docker-compose.gpu.yml and docker-compose.diar-native-gpu.yml from a real "
        f"start on 2026-09-07.\n{output}"
    )


def test_a_small_docker_info_reporting_nvidia_selects_the_nvidia_runtime(
    tmp_path: Path,
) -> None:
    """MUST-STAY-CLEAN control: the ordinary case must be unchanged by the fix."""
    stubs = _stub_dir(
        tmp_path,
        'if [ "${1:-}" = "info" ]; then\n'
        "  printf ' Server Version: 27.0.0\\n Runtimes: io.containerd.runc.v2 nvidia runc\\n'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0",
    )

    runtime, output = _detect_hardware(OPENTR, stubs)

    assert runtime == "nvidia", output


def test_a_host_whose_daemon_reports_no_nvidia_runtime_still_falls_back_to_cpu(
    tmp_path: Path,
) -> None:
    """MUST-STAY-CLEAN control: a genuinely CPU-only host must keep getting CPU.

    Without this, "always answer nvidia" would pass every other case in this file.
    """
    stubs = _stub_dir(
        tmp_path,
        'if [ "${1:-}" = "info" ]; then\n'
        "  printf ' Server Version: 27.0.0\\n Runtimes: io.containerd.runc.v2 runc\\n'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0",
    )

    runtime, output = _detect_hardware(OPENTR, stubs)

    assert runtime == "", output
    assert "Container Toolkit not available" in output, output


def test_a_daemon_that_cannot_answer_reads_as_could_not_check_not_as_no_toolkit(
    tmp_path: Path,
) -> None:
    """A daemon that will not answer is a THIRD outcome, not the negative one.

    Collapsing them is what made the transient inversion above indistinguishable
    from a CPU-only host in the log -- the same 1-vs-2 split ``security-scan.sh``
    and ``scripts/lib/manifest_platform_check.py`` already draw. The fallback to
    CPU is still correct here; misreporting *why* is not.
    """
    stubs = _stub_dir(tmp_path, 'if [ "${1:-}" = "info" ]; then\n  exit 1\nfi\nexit 0')

    runtime, output = _detect_hardware(OPENTR, stubs)

    assert runtime == "", output
    assert "COULD NOT DETERMINE" in output, (
        "an unreachable daemon was reported as a definitive 'no toolkit' verdict -- "
        f"a real GPU host then degrades to CPU with no signal that anything went wrong.\n{output}"
    )


# --------------------------------------------------------------------------- #
# docker ps -> diar_native_container_present
# --------------------------------------------------------------------------- #


def test_the_sidecar_probe_survives_a_docker_ps_that_outwrites_a_pipe_buffer(
    tmp_path: Path,
) -> None:
    """MUST-FIRE case for the sibling probe.

    ``diar_native_container_present`` used ``| grep -q .`` -- the same shape, and
    ``docker ps`` on a busy host writes plenty. Inverted, it reads as "this
    deployment has no sidecar", which drops the overlay from ``rebuild-backend``
    and silently hands celery-worker back to the in-process PyAnnote fallback the
    probe exists to prevent.
    """
    payload = _overflow_payload(tmp_path)
    stubs = _stub_dir(
        tmp_path,
        'if [ "${1:-}" = "ps" ]; then\n'
        "  printf 'deadbeefcafe\\n'\n"
        f'  exec cat "{payload}"\n'
        "fi\n"
        "exit 0",
    )

    rc, output = _sidecar_probe(OPENTR, stubs)

    assert rc == 0, (
        "docker ps listed a diar-native container, but the probe reported none -- "
        f"the `grep -q` + pipefail SIGPIPE inversion again.\n{output}"
    )


def test_the_sidecar_probe_is_false_when_docker_ps_matches_nothing(tmp_path: Path) -> None:
    """MUST-STAY-CLEAN control: an absent sidecar must still read as absent."""
    stubs = _stub_dir(tmp_path, "exit 0")

    rc, output = _sidecar_probe(OPENTR, stubs)

    assert rc == 1, output


# --------------------------------------------------------------------------- #
# the SHAPE, not just this call site
# --------------------------------------------------------------------------- #
#
# Guarding only `docker_runtime_has_nvidia` would guard the symptom. The hazard is a
# general composition rule -- pipefail + a reader that stops before EOF + a producer
# still writing -- so the scan below looks for the shape across every script this
# change owns. It is deliberately NARROW, because a scanner that fires on correct
# code is one people learn to suppress:
#
#   * only files that actually set `pipefail` (otherwise the status is not aggregated
#     and there is nothing to invert);
#   * only readers that genuinely stop early -- `grep -q`/`--quiet`, `grep -m1`,
#     `head`, `sed -n '...q'`. `grep -c` and a bare `grep` read to EOF and are the
#     documented FIX (scripts/CLAUDE.md), so they must never be flagged;
#   * only producers that are a real command still writing when the reader leaves.
#     `printf '%s\n' "$var" | grep -q` is safe: the payload is already in memory and
#     goes into the pipe in one write. Flagging it would be the nuisance case;
#   * and only in a context where the damage is real -- see the two below.

#: Scripts converted so far. Widening this list is the point: the same shape exists
#: elsewhere in `scripts/` and each of those files needs its own owner to convert it.
#:
#: The second block is the RELEASE and REHEARSAL family. It is here because the tracked
#: sibling `test_pipefail_grep_q_inversion.py` scans all of `scripts/` but only for the
#: `| grep -q` half — it cannot see the ASSIGNMENT/abort half (`x="$(producer | head -1)"`
#: under `set -e`), which is the #617/#618 shape and which these files carried at eight
#: sites, five of them in `test-upgrade.sh` alone. Two scanners with different reaches, one
#: hazard; do not add a third.
_OWNED_SCRIPTS = (
    REPO_ROOT / "opentr.sh",
    REPO_ROOT / "setup-opentranscribe.sh",
    REPO_ROOT / "scripts" / "diar-native-smoke.sh",
    REPO_ROOT / "scripts" / "lib" / "dev-test-overlays.sh",
    # release + rehearsal
    REPO_ROOT / "scripts" / "build-all.sh",
    REPO_ROOT / "scripts" / "check-dependency-parity.sh",
    REPO_ROOT / "scripts" / "gpu-scale-smoke.sh",
    REPO_ROOT / "scripts" / "install-offline-package.sh",
    REPO_ROOT / "scripts" / "lite-smoke.sh",
    REPO_ROOT / "scripts" / "run-auth-e2e.sh",
    REPO_ROOT / "scripts" / "test-watch-e2e.sh",
    REPO_ROOT / "scripts" / "pki" / "run-pki-e2e-leg.sh",
    REPO_ROOT / "scripts" / "release" / "10-preflight.sh",
    REPO_ROOT / "scripts" / "release-tests" / "test-upgrade.sh",
    REPO_ROOT / "scripts" / "release-tests" / "selftest-rollback-fault-injection.sh",
    REPO_ROOT / "scripts" / "release-tests" / "lib" / "api-client.sh",
    REPO_ROOT / "scripts" / "release-tests" / "lib" / "assertions.sh",
    REPO_ROOT / "scripts" / "release-tests" / "lib" / "compose-chain.sh",
    REPO_ROOT / "scripts" / "release-tests" / "lib" / "guardrails.sh",
)

#: Every script that decides, for itself, whether this host has a usable NVIDIA
#: container runtime. All three write that verdict somewhere that matters:
#: ``opentr.sh`` into the compose chain, ``setup-opentranscribe.sh`` into the user's
#: ``.env`` (permanently), ``opentranscribe.sh`` into the production compose chain.
_DOCKER_RUNTIME_PROBES = (
    REPO_ROOT / "opentr.sh",
    REPO_ROOT / "setup-opentranscribe.sh",
    REPO_ROOT / "opentranscribe.sh",
)

#: A producer that is a real process, i.e. one that can still be writing. A shell
#: builtin emitting an in-memory string is not this, and must not be flagged.
#:
#: ⚠️ THE TEXT-FILTER HALF (`grep`, `sort`, `tr`, `cut`, `uniq`, `wc`, `tail`, `xargs`) IS
#: NOT DECORATION. It was left out of the first revision, and the omission is why
#: `selftest-rollback-fault-injection.sh:132`'s
#: `copy_line="$(grep -n … | head -1 | cut -d: -f1)"` — a textbook instance of the
#: #617/#618 assignment-abort shape — was invisible to a scanner written for it. A filter
#: is every bit as much "a real process that can still be writing" as `docker` is: `grep -n`
#: over a multi-megabyte `pg_dump` emits its matches incrementally, exactly like `find` does.
#:
#: Widening this list required fixing TWO scanner bugs first (see `_pipeline_segments` and
#: `_strip_heredoc_bodies`) — with the narrow list neither bug had a way to surface, because
#: no line in an owned script reached the reporting branch at all. Widening it before the
#: fixes produced 2 findings of which BOTH were false positives.
_CHATTY_PRODUCER = re.compile(
    r"\b(?:docker|git|find|ls|curl|ss|nvidia-smi|dpkg|rpm|comm|diff|kubectl|awk|sed|cat|jq"
    r"|grep|sort|tr|cut|uniq|wc|tail|xargs|openssl)\b"
)

#: Findings that are examined and deliberately not fixed. Keyed
#: ``"<file name>:<line>"`` -> written reason, the same shape (and the same rules) as
#: ``scripts/audit-tests.py``'s allowlist and ``test_shell_expansion_guards.py``'s
#: ``_ALLOWLIST``: a reason is **mandatory**, and a **stale entry fails the run** so an
#: exemption can never outlive its subject.
#:
#: It is currently EMPTY, and that is a measurement rather than an oversight: after the two
#: scanner bugs below were fixed, the widened producer list finds nothing in the owned
#: scripts. Read this dict, never a count transcribed into prose.
_PIPEFAIL_ALLOWLIST: dict[str, str] = {}


#: A trailing ``|| true`` / ``|| echo …`` inside the command substitution genuinely
#: neutralises the ``set -e`` half of the hazard: the SIGPIPE status is swallowed and
#: the CAPTURED VALUE is still correct (the reader got the line it wanted before it
#: left). `resolve_latest_release` in setup-opentranscribe.sh is already written this
#: way, and flagging it would be a false positive on code that is right -- the exact
#: nuisance that gets a scanner suppressed rather than obeyed. It does NOT excuse the
#: boolean case, where the inverted answer is the whole problem and `|| true` would
#: only make it worse.
_ERREXIT_GUARDED = re.compile(r"\|\|\s*(true|:|echo\b)")


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """``(first line number, joined text)`` for each backslash-continued command.

    Load-bearing, not tidiness: ``diar_native_container_present`` spelled its
    pipeline across four physical lines, so a line-by-line scan saw a ``docker ps``
    with no reader and a ``| grep -q .`` with no producer, and reported neither. One
    of the two probes this whole file is about would have been invisible to its own
    invariant.
    """
    out: list[tuple[int, str]] = []
    buf = ""
    start = 0
    for n, raw in enumerate(text.splitlines(), start=1):
        if not buf:
            start = n
        if raw.rstrip().endswith("\\"):
            buf += raw.rstrip()[:-1] + " "
            continue
        out.append((start, buf + raw))
        buf = ""
    if buf:
        out.append((start, buf))
    return out


#: Opens a heredoc: ``<<EOF``, ``<<-EOF``, ``<<'EOF'``, ``<<"EOF"``. The tag must start
#: with an identifier character, which is what keeps arithmetic left-shift (``$(( x << 2 ))``)
#: and here-STRINGS (``<<<"$v"``) out.
_HEREDOC_OPEN = re.compile(r"<<(?P<dash>-?)\s*(?P<q>['\"]?)(?P<tag>[A-Za-z_][A-Za-z0-9_]*)(?P=q)")


def _strip_heredoc_bodies(text: str) -> str:
    """Blank out every heredoc BODY line, preserving line numbering.

    ⚠️ MEASURED BUG, not a hypothetical. ``_pipefail_offenders`` decides whether ``set -e``
    is in force by regex-scanning the whole file. ``selftest-rollback-fault-injection.sh``
    runs under ``set -uo pipefail`` — **no** ``-e`` — but it *writes two throwaway scripts*
    with ``cat > … <<EOF`` whose bodies begin ``set -euo pipefail`` (lines 186 and 202).
    The scanner read those as the file's own options, concluded errexit, and reported
    line 132's assignment as "aborts under set -e" when that file can do no such thing.

    A heredoc body is a *different* program with its own option state, so it is excluded
    from the scan entirely rather than guessed at. That is a deliberate blind spot with a
    written reason: a hazardous line inside a generated script is real, but nothing here
    can know which options the generated script sets, and a scanner that guesses is one
    whose findings get suppressed rather than fixed.

    Body lines are replaced with ``""`` rather than deleted so every line number reported
    by the scanner still matches the file on disk.
    """
    out: list[str] = []
    pending: list[tuple[str, bool]] = []  # (tag, strip-leading-tabs)
    for raw in text.splitlines():
        if pending:
            tag, dash = pending[0]
            candidate = raw.lstrip("\t") if dash else raw
            out.append("")
            if candidate.strip() == tag:
                pending.pop(0)
            continue
        out.append(raw)
        if raw.lstrip().startswith("#"):
            continue  # a comment mentioning `<<EOF` opens nothing
        for m in _HEREDOC_OPEN.finditer(raw):
            pending.append((m.group("tag"), bool(m.group("dash"))))
    return "\n".join(out)


def _pipeline_segments(line: str) -> list[str]:
    """Split a logical line on REAL pipe operators only.

    ⚠️ MEASURED BUG, not a hypothetical. This was ``line.split("|")``, which reads the
    ``||`` of a logical OR as two pipe boundaries. ``setup-opentranscribe.sh:1688`` is::

        if grep -q "^NGINX_SERVER_NAME=" .env || grep -q "^#.*NGINX_SERVER_NAME=" .env; then

    — two independent ``grep`` invocations reading FILES, with no pipe anywhere on the
    line. The naive split produced the segments ``['if grep -q … .env ', '',
    ' grep -q … .env; then']``, found a "producer" and an "early-exit reader", and reported
    a SIGPIPE inversion on a line that has no pipe to break. Correct code, flagged as the
    bug — the nuisance finding that gets a scanner suppressed instead of obeyed.

    Quote tracking is here for the same reason: ``grep -cE "a|b"``'s alternation is not a
    pipe stage either. The previous code survived that case by accident (the resulting
    fragment happened not to parse as a reader), not by design.

    An unquoted trailing ``#`` comment is dropped, because this repo's fix comments quote
    the broken form verbatim and a widened producer list would otherwise fire on them.

    ⚠️ **A COMMAND SUBSTITUTION RESETS QUOTING, AND MISSING THAT COSTS THE WHOLE ASSIGNMENT
    HALF OF THE INVARIANT.** A first draft tracked one flat quote character, so in::

        copy_line="$(grep -n '^COPY …' "$WORKDIR/full.sql" | head -1 | cut -d: -f1)"

    the opening ``"`` of the assignment made every later character "quoted" and the two
    pipes were not boundaries at all — the scanner returned a single segment and found
    nothing. That is precisely the ``x="$(producer | head -1)"`` shape the ``set -e`` abort
    branch exists for (#617/#618), i.e. the fix for one false positive would have deleted
    the detector's most valuable true positive. Verified against a real file: with
    ``set -euo pipefail`` substituted into
    ``scripts/release-tests/selftest-rollback-fault-injection.sh``, the flat tracker found
    0 offenders and the nesting one finds line 132.

    So the state is a STACK. Inside ``$(…)`` the quoting context restarts, which is what
    bash does; a ``|`` is a pipeline boundary when the innermost context is either the base
    level or a command substitution, and is literal text inside ``'…'``, ``"…"`` and
    ``$((…))``.
    """
    segments: list[str] = []
    buf: list[str] = []
    stack: list[str] = []  # 'sq' | 'dq' | 'cmd' | 'arith'
    i = 0
    n = len(line)

    def top() -> str | None:
        return stack[-1] if stack else None

    while i < n:
        ch = line[i]
        cur = top()

        if cur == "sq":  # single quotes: no escapes, only the closing quote matters
            if ch == "'":
                stack.pop()
            buf.append(ch)
            i += 1
            continue

        if ch == "\\" and i + 1 < n:  # escapes apply everywhere but inside '...'
            buf.append(ch)
            buf.append(line[i + 1])
            i += 2
            continue
        if ch == "'" and cur != "dq":  # a ' inside "..." is literal
            stack.append("sq")
            buf.append(ch)
            i += 1
            continue
        if ch == '"':
            stack.pop() if cur == "dq" else stack.append("dq")
            buf.append(ch)
            i += 1
            continue
        if line.startswith("$((", i):
            stack.append("arith")
            buf.append(line[i : i + 3])
            i += 3
            continue
        if line.startswith("$(", i):
            stack.append("cmd")  # quoting RESTARTS inside a command substitution
            buf.append(line[i : i + 2])
            i += 2
            continue
        if ch == "`":
            stack.pop() if cur == "cmd" else stack.append("cmd")
            buf.append(ch)
            i += 1
            continue
        if line.startswith("))", i) and cur == "arith":
            stack.pop()
            buf.append("))")
            i += 2
            continue
        if ch == ")" and cur == "cmd":
            stack.pop()
            buf.append(ch)
            i += 1
            continue
        if ch == "#" and cur is None and (i == 0 or line[i - 1].isspace()):
            break  # unquoted trailing comment
        if ch == "|" and cur in (None, "cmd"):
            if i + 1 < n and line[i + 1] == "|":
                buf.append("||")  # logical OR — NOT a pipeline boundary
                i += 2
                continue
            segments.append("".join(buf))
            buf = []
            # `|&` pipes stderr too; it is still exactly one boundary.
            i += 2 if (i + 1 < n and line[i + 1] == "&") else 1
            continue
        buf.append(ch)
        i += 1

    segments.append("".join(buf))
    return segments


def _is_early_exit_reader(segment: str) -> bool:
    """Does this pipeline stage stop reading before EOF?

    ⚠️ Option parsing is deliberately TOKENISED rather than a regex over the rest of
    the line. The first draft used ``grep\\s+[^|;&\\n]*-[a-zA-Z]*q``, whose ``[^|]*``
    ran past grep's own arguments and matched the ``-eq`` of
    ``[ "$(docker ps | grep -c foo)" -eq 0 ]`` -- i.e. it reported the documented FIX
    as the bug, in the very file that had just applied it. A scanner whose first
    finding is a false positive on correct code is one that gets suppressed.
    """
    tokens = segment.split()
    if not tokens:
        return False
    cmd = tokens[0].rsplit("/", 1)[-1]
    if cmd in {"head"}:
        return True
    if cmd == "grep":
        for tok in tokens[1:]:
            if not tok.startswith("-"):
                break  # the pattern; grep's own options have ended
            if tok in {"--quiet", "--silent"} or tok.startswith("-m"):
                return True
            if not tok.startswith("--") and "q" in tok[1:]:
                return True
        return False
    if cmd == "sed":
        # `sed -n '3q'` / `sed '/x/q'` stop at the quit command.
        return any(re.search(r"\bq\b|q'", tok) for tok in tokens[1:] if not tok.startswith("-"))
    return False


def _pipefail_offenders(path: Path) -> list[str]:
    """Lines in ``path`` matching the hazardous shape, in a context where it bites.

    Two contexts, and they fail differently:

    * **boolean** (``if``/``elif``/``while``/``!``/``&&``/``||``) -- the pipeline's
      status IS the decision, so SIGPIPE silently INVERTS the answer. This is the
      2026-09-07 defect.
    * **assignment under ``set -e``** -- ``x="$(producer | head -1)"`` captures the
      right value and then aborts the script on the producer's 141, truncating every
      phase after it with no error trace. That is the #617/#618 family, measured:
      ``bash -c 'set -euo pipefail; v="$(cat 2MB | head -1)"; echo REACHED'`` exits
      141 and never prints. A file without ``set -e`` is unaffected, so this half
      only applies where ``set -e`` is on.
    """
    # Heredoc bodies are OTHER programs with their own option state -- see
    # `_strip_heredoc_bodies`. Stripped before BOTH the `set` detection and the line scan,
    # because reading a generated script's `set -euo pipefail` as this file's own is what
    # made `selftest-rollback-fault-injection.sh` report an abort it cannot suffer.
    text = _strip_heredoc_bodies(path.read_text(encoding="utf-8"))
    if not re.search(r"^\s*set\s+[-a-zA-Z ]*\bpipefail\b", text, re.M):
        return []
    errexit = bool(re.search(r"^\s*set\s+-[a-zA-Z]*e", text, re.M))

    offenders = []
    for n, line in _logical_lines(text):
        stripped = line.strip()
        if stripped.startswith("#") or "|" not in stripped:
            continue
        segments = _pipeline_segments(stripped)
        if len(segments) < 2:
            continue  # `||` only, or a `|` inside quotes -- no pipeline here
        producer = segments[0]
        if not _CHATTY_PRODUCER.search(producer):
            continue
        if not any(_is_early_exit_reader(seg) for seg in segments[1:]):
            continue
        boolean = bool(
            re.match(r"^(?:if|elif|while|until)\b", stripped)
            or stripped.startswith("!")
            or "; then" in stripped
        )
        if boolean:
            offenders.append(f"{path.name}:{n}  [inverts a decision]  {stripped[:120]}")
        elif errexit and "=" in producer and not _ERREXIT_GUARDED.search(stripped):
            offenders.append(f"{path.name}:{n}  [aborts under set -e]  {stripped[:120]}")
    return offenders


def test_no_owned_script_pipes_a_live_producer_into_an_early_exiting_reader() -> None:
    """The general form of the 2026-09-07 defect, across every script this lane owns.

    ``opentr.sh`` had SIX instances of it, not one: the GPU runtime probe, the
    diar-native sidecar probe, flower and nginx health, the gpu-scale/gpu-split
    rebuild loop, and two benchmark preconditions. All six are converted; this stops
    a seventh, and stops the same shape appearing in the two sibling scripts.
    """
    found = [line for path in _OWNED_SCRIPTS for line in _pipefail_offenders(path)]

    # A stale exemption FAILS, exactly as scripts/audit-tests.py's allowlist does: the file
    # can only ever shrink, and an exemption cannot outlive the finding it excuses.
    keys = {line.split("  ", 1)[0] for line in found}
    stale = sorted(set(_PIPEFAIL_ALLOWLIST) - keys)
    assert not stale, (
        "_PIPEFAIL_ALLOWLIST exempts findings that no longer exist. Delete these entries "
        "rather than leaving an exemption with nothing to excuse:\n  " + "\n  ".join(stale)
    )

    offenders = [line for line in found if line.split("  ", 1)[0] not in _PIPEFAIL_ALLOWLIST]
    assert not offenders, (
        "under `set -o pipefail`, a reader that exits before EOF (`grep -q`, `head`, "
        "`grep -m1`, `sed q`) can kill its producer with SIGPIPE (141); pipefail then "
        "makes that the PIPELINE's status. In a condition the answer inverts; in an "
        "assignment under `set -e` the script aborts mid-flight. Fix: "
        '`[ "$(producer | grep -c PATTERN)" -gt 0 ]`, or capture the producer into a '
        "variable and match on that.\n  " + "\n  ".join(offenders)
    )


def test_no_pipefail_script_probes_the_docker_runtime_through_a_pipe() -> None:
    """The narrowest, most load-bearing form of the invariant.

    Three scripts independently ask "does this host have the nvidia container
    runtime?", and each persists the answer somewhere costly. Two of them run under
    ``pipefail``, where ``docker info | grep -q nvidia`` inverts intermittently.

    ``opentranscribe.sh`` keeps the pipe form and is CORRECT to, because it sets
    ``set -e`` only: with no ``pipefail`` the pipeline's status is grep's, so a match
    reads as a match. This test therefore checks the PAIRING (pipefail + piped probe),
    not the spelling -- which is the same thing as saying: if someone later adds
    ``set -o pipefail`` to ``opentranscribe.sh``, this fails, which is exactly the
    regression the comment above that probe warns about.
    """
    offenders = []
    for path in _DOCKER_RUNTIME_PROBES:
        text = _strip_heredoc_bodies(path.read_text(encoding="utf-8"))
        if not re.search(r"^\s*set\s+[-a-zA-Z ]*\bpipefail\b", text, re.M):
            continue
        for n, line in _logical_lines(text):
            stripped = line.strip()
            if stripped.startswith("#") or "docker info" not in stripped:
                continue
            segments = _pipeline_segments(stripped)
            if any(_is_early_exit_reader(seg) for seg in segments[1:]):
                offenders.append(f"{path.name}:{n}  {stripped[:120]}")

    assert not offenders, (
        "a script that sets `pipefail` must not decide GPU support with "
        "`docker info | grep -q ...`: grep -q exits at the first match, docker info "
        "takes SIGPIPE (141), and pipefail turns that MATCH into a NON-match. Capture "
        "`docker info` into a variable and substring-match it -- see "
        "`docker_runtime_has_nvidia` in opentr.sh / setup-opentranscribe.sh.\n  "
        + "\n  ".join(offenders)
    )


def test_the_installers_undetermined_verdict_is_never_persisted_as_cpu() -> None:
    """``setup-opentranscribe.sh`` is the severe case: its GPU verdict is written to
    the user's ``.env`` and never re-detected, so "could not check" must not silently
    become "CPU forever".

    Structural, because the alternative is running a `curl | bash` installer. Three
    things must hold together, and any one of them alone is satisfiable by code that
    still guesses:

    1. the probe reports a distinct third state (``return 2``);
    2. the caller reads the code rather than collapsing it with ``if check_gpu_support``;
    3. the undetermined branch reaches an ``exit``, not just ``fallback_to_cpu``.
    """
    text = (REPO_ROOT / "setup-opentranscribe.sh").read_text(encoding="utf-8")

    probe = _function_source(text, "check_gpu_support")
    assert "return 2" in probe, (
        "check_gpu_support no longer distinguishes 'could not determine' from 'no "
        f"toolkit'; collapsing them is what let a busy daemon pin a GPU host to CPU:\n{probe}"
    )

    caller = _function_source(text, "configure_docker_runtime")
    # Code only -- this function's own comment quotes the broken form to explain it.
    caller_code = "\n".join(
        line for line in caller.splitlines() if not line.lstrip().startswith("#")
    )
    assert "if check_gpu_support; then" not in caller_code, (
        "configure_docker_runtime is back to using check_gpu_support as a boolean, "
        "which silently merges exit 2 into the CPU-fallback branch"
    )
    undetermined = caller_code.split("-eq 2", 1)
    assert len(undetermined) == 2, f"no `-eq 2` branch in configure_docker_runtime:\n{caller}"
    branch = undetermined[1].split("else", 1)[0]
    assert "exit 1" in branch, (
        "the 'could not determine' branch must be able to REFUSE rather than persist a "
        f"guess into .env; found no `exit 1` before the next branch:\n{branch}"
    )
    assert "fallback_to_cpu" not in branch.split("exit 1", 1)[0], (
        "the 'could not determine' branch calls fallback_to_cpu BEFORE it can refuse -- "
        "that writes DETECTED_DEVICE=cpu to the user's .env on a GPU host"
    )


def test_the_scanner_fires_on_the_shape_it_is_meant_to_catch(tmp_path: Path) -> None:
    """Guard the guard, must-fire half.

    A scanner that matches nothing reports zero offenders and reads exactly like a
    clean tree -- the failure mode ``scripts/audit-tests.py --selftest`` exists for.
    So: hand it the real broken line and require it to fire.
    """
    script = tmp_path / "broken.sh"
    script.write_text(
        "#!/bin/bash\nset -uo pipefail\n"
        "if docker info 2>/dev/null | grep -q nvidia; then\n  echo yes\nfi\n",
        encoding="utf-8",
    )

    offenders = _pipefail_offenders(script)

    assert len(offenders) == 1, offenders
    assert "inverts a decision" in offenders[0], offenders


def test_the_scanner_sees_a_pipeline_split_across_continuation_lines(tmp_path: Path) -> None:
    """Guard the guard: ``diar_native_container_present`` wrote its pipeline over
    four physical lines. A line-by-line scan sees a producer with no reader and a
    reader with no producer, and misses the one probe this file is named for."""
    script = tmp_path / "wrapped.sh"
    script.write_text(
        "#!/bin/bash\nset -uo pipefail\n"
        "if docker ps -a --format '{{.ID}}' \\\n"
        "    --filter 'label=x' \\\n"
        "    2>/dev/null | grep -q .; then\n  echo yes\nfi\n",
        encoding="utf-8",
    )

    offenders = _pipefail_offenders(script)

    assert len(offenders) == 1, offenders
    assert "inverts a decision" in offenders[0], offenders


@pytest.mark.parametrize(
    ("label", "body"),
    [
        # The documented FIX must not be reported as the bug. `-eq 0` in particular:
        # a regex scanning the rest of the line reads that as a `-q` option cluster.
        ("grep -c count test", 'if [ "$(docker ps | grep -c foo)" -gt 0 ]; then\n  :\nfi\n'),
        ("grep -c with -eq", 'if [ "$(docker ps | grep -c "^x$")" -eq 0 ]; then\n  :\nfi\n'),
        # A plain grep reads to EOF; only -q/-m1/--quiet stop early.
        ("plain grep", "if docker ps | grep foo; then\n  :\nfi\n"),
        # An alternation inside the pattern must not be mistaken for a pipe stage.
        ("quoted alternation", 'if [ "$(docker ps | grep -cE "a|b")" -gt 0 ]; then\n  :\nfi\n'),
        # `|| true` swallows the SIGPIPE status, so the `set -e` abort cannot happen
        # and the captured value is still correct. Flagging correct code is how a
        # scanner earns a suppression instead of a fix.
        # `set -e` on its own line here: without errexit the abort branch is
        # unreachable and the case would prove nothing.
        ("assignment guarded by || true", "set -e\nv=$(curl -s x | grep -m1 y || true)\n"),
        # A builtin emitting an in-memory string cannot still be writing.
        # ⚠️ THAT EXEMPTION HAS A CEILING -- see
        # `test_the_printf_exemption_has_a_measured_ceiling` below. It is sound only for
        # a SMALL payload, and the scanner cannot know a variable's size statically, so
        # this stays an exemption with a written bound rather than a proof.
        ("printf into grep -q", 'if printf "%s\\n" "$v" | grep -q foo; then\n  :\nfi\n'),
        # No pipefail => the pipeline's status is the reader's, which is correct.
        ("no pipefail", "if docker info | grep -q nvidia; then\n  :\nfi\n"),
        # A comment quoting the broken form (this fix's own warnings do exactly that).
        ("comment", "# never write `docker info | grep -q nvidia`\n"),
        # ── BUG 1: `||` is a logical OR, not two pipeline boundaries. ────────────
        # This is setup-opentranscribe.sh:1688 verbatim. Two greps reading FILES; no
        # pipe on the line at all. `stripped.split("|")` produced a "producer" and an
        # "early-exit reader" out of thin air and reported a SIGPIPE inversion on it.
        (
            "logical OR between two file greps",
            'if grep -q "^NGINX_SERVER_NAME=" .env || grep -q "^#.*NGINX_SERVER_NAME=" .env; then\n'
            "  :\nfi\n",
        ),
        # The same, in the assignment position, so the fix is not accidentally
        # branch-specific. The RHS of the `||` is itself an early-exit reader, so the
        # naive split had both halves it needs and did fire on this.
        ("logical OR in an assignment", "set -e\nv=$(grep -c x f) || grep -q y f\n"),
        # A `|` inside a quoted STRING is data, not a pipe stage. These scripts print
        # advice strings that quote the hazard verbatim, which the naive split read as a
        # live `docker` piped into a live `grep -q`.
        ("pipe inside a quoted string", "set -e\nadvice='docker info | grep -q nvidia'\n"),
        # A trailing comment quoting the broken form, on a line of real code -- the shape
        # every fix comment in these scripts uses.
        (
            "trailing comment quoting the broken form",
            "set -e\nv=$(docker ps -q)  # was: docker info | grep -q nvidia\n",
        ),
        # ── BUG 2: a heredoc body is a DIFFERENT program with its own options. ───
        # `selftest-rollback-fault-injection.sh` runs under `set -uo pipefail` and writes
        # two throwaway scripts whose bodies open `set -euo pipefail`. Reading those as
        # the file's own errexit made a real, harmless line report as an abort.
        (
            "errexit only inside a heredoc body",
            "cat > /tmp/gen.sh <<EOF\n"
            "set -euo pipefail\n"
            "EOF\n"
            'v="$(grep -n x f | head -1 | cut -d: -f1)"\n',
        ),
        # And the body's own hazardous lines are out of scope, deliberately: nothing here
        # can know which options the GENERATED script sets. Documented blind spot.
        (
            "hazardous line inside a heredoc body",
            "set -e\ncat > /tmp/gen.sh <<'EOF'\nv=\"$(grep -n x f | head -1)\"\nEOF\n",
        ),
        # `<<-EOF` strips leading tabs from the terminator; the body must still be skipped.
        (
            "tab-indented heredoc terminator",
            'set -e\ncat > /tmp/gen.sh <<-EOF\nv="$(grep -n x f | head -1)"\n\tEOF\n',
        ),
    ],
)
def test_the_scanner_stays_clean_on_correct_code(label: str, body: str, tmp_path: Path) -> None:
    """Guard the guard, must-stay-clean half -- the nuisance cases.

    Each of these is either the recommended fix or provably safe. A scanner that
    flagged them would be suppressed rather than obeyed, and the invariant above
    would stop meaning anything.
    """
    script = tmp_path / "ok.sh"
    header = "#!/bin/bash\n" + ("" if label == "no pipefail" else "set -uo pipefail\n")
    script.write_text(header + body, encoding="utf-8")

    assert _pipefail_offenders(script) == [], label


@pytest.mark.parametrize(
    ("label", "body", "kind"),
    [
        # ── BUG 1's must-fire half. Suppressing `||` must not suppress the real pipe
        # that shares the line with it, or the false-positive fix would have deleted a
        # true positive along with it.
        (
            "a real pipe on a line that also has ||",
            "if docker info | grep -q nvidia || fallback_to_cpu; then\n  :\nfi\n",
            "inverts a decision",
        ),
        # ── The cost of getting BUG 1's fix wrong. A first draft tracked ONE flat quote
        # character, so the opening `"` of an assignment made the whole command
        # substitution "quoted" and its pipes vanished -- silently deleting the entire
        # #617/#618 abort branch, which is the most valuable half of this scanner.
        (
            "pipes inside a double-quoted command substitution",
            'set -e\ncopy_line="$(grep -n \'^COPY x\' "$f" | head -1 | cut -d: -f1)"\n',
            "aborts under set -e",
        ),
        (
            "backtick command substitution",
            "set -e\nv=`docker ps --format '{{.Names}}' | head -1`\n",
            "aborts under set -e",
        ),
        # ── BUG 2's must-fire half. A file whose OWN top level sets errexit must still
        # report, heredocs present or not; otherwise "ignore heredoc bodies" could be
        # implemented as "ignore everything after the first `<<`".
        (
            "real top-level errexit, with a heredoc elsewhere in the file",
            "set -e\ncat > /tmp/gen.sh <<'EOF'\nharmless\nEOF\nv=\"$(grep -n x f | head -1)\"\n",
            "aborts under set -e",
        ),
        # A here-STRING and an arithmetic left-shift both contain `<<` and open no
        # heredoc. If either were mistaken for one, everything below it would go
        # unscanned -- a scanner that silently stops reading is exactly the "0 findings
        # is indistinguishable from clean" failure this repo has shipped twice.
        (
            "a here-string does not swallow the rest of the file",
            'set -e\ngrep -c x <<<"$v" >/dev/null\nv="$(grep -n x f | head -1)"\n',
            "aborts under set -e",
        ),
        (
            "an arithmetic left-shift does not swallow the rest of the file",
            'set -e\nn=$(( 1 << 4 ))\nv="$(grep -n x f | head -1)"\n',
            "aborts under set -e",
        ),
        # ── The widened producer list. Each of these is a TEXT FILTER in producer
        # position: `grep -n` over a multi-megabyte pg_dump emits incrementally, exactly
        # as `find` does. The narrow list saw none of them, which is why
        # `test-upgrade.sh:1897` and `:2112` -- two real instances of the abort shape --
        # were invisible to the scanner written for that shape.
        ("grep as producer", "set -e\nv=$(grep -n x f | head -1)\n", "aborts under set -e"),
        ("sort as producer", "set -e\nv=$(sort f | head -1)\n", "aborts under set -e"),
        ("tr as producer", "set -e\nv=$(tr -d ' ' < f | head -1)\n", "aborts under set -e"),
        ("cut as producer", "set -e\nv=$(cut -d: -f1 f | head -1)\n", "aborts under set -e"),
        (
            "grep as producer, boolean",
            "if grep x f | grep -q y; then\n  :\nfi\n",
            "inverts a decision",
        ),
    ],
)
def test_the_scanner_fires_on_every_shape_the_two_bug_fixes_could_have_blinded_it_to(
    label: str, body: str, kind: str, tmp_path: Path
) -> None:
    """Guard the guard: each fix above removes a false positive, and could remove true
    positives with it.

    Both bugs were fixed by making the scanner see LESS: ``||`` is no longer a pipeline
    boundary, and heredoc bodies are no longer read. Either change is one over-reach away
    from a scanner that reports nothing -- and a detector reporting 0 findings is
    indistinguishable from a clean tree, which is the exact failure
    ``scripts/audit-tests.py --selftest`` was built for and which this repo has shipped
    twice. So every shape the fixes pass through gets a must-fire case.
    """
    script = tmp_path / "fires.sh"
    script.write_text(f"#!/bin/bash\nset -uo pipefail\n{body}", encoding="utf-8")

    offenders = _pipefail_offenders(script)

    assert len(offenders) == 1, f"{label}: {offenders}"
    assert kind in offenders[0], f"{label}: {offenders}"


#: Payload size the ``printf`` exemption above is asserted safe at. Deliberately tiny --
#: see the ceiling this pins. It has to stay below one stdio buffer: the exemption's
#: premise is that the payload leaves in ONE ``write(2)``, and bash's ``printf`` builtin
#: flushes through stdio, whose buffer for a pipe is ``st_blksize`` -- 4096 B on Linux.
#: ``4 * 1024`` plus the ``FIRST\n`` header and the trailing newline is 4103 B, i.e. two
#: writes, and under xdist load ``head`` can leave between them, which is exactly the
#: SIGPIPE this test says a builtin cannot get (#918). 1 KiB is one write on every host.
_PRINTF_SAFE_BYTES = 1024
#: ...and a size at which the same construct provably breaks. Measured on this host
#: 2026-09-07, `printf 'FIRST\n<payload>\n' | head -1` under `set -euo pipefail`:
#: 1 KiB 0/100 aborts, 7 KiB 0/100, **16 KiB 4/100**, 32 KiB 31/100, 60 KiB 100/100,
#: 3.4 MiB 100/100.
_PRINTF_BROKEN_BYTES = 256 * 1024


def _printf_into_head_survives(payload_bytes: int) -> bool:
    """Does ``printf <payload> | head -1`` complete under ``set -euo pipefail``?"""
    proc = subprocess.run(
        [
            "bash",
            "-c",
            "set -euo pipefail\n"
            f'v=$(head -c {payload_bytes} /dev/zero | tr "\\0" "a")\n'
            'x=$(printf "FIRST\\n%s\\n" "$v" | head -1)\n'
            '[ "$x" = FIRST ] && echo SURVIVED\n',
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return "SURVIVED" in proc.stdout


def test_the_printf_exemption_has_a_measured_ceiling() -> None:
    """ "A builtin cannot still be writing" is TRUE ONLY BELOW A SIZE, and this pins it.

    ``_pipefail_offenders`` deliberately does not flag ``printf … | grep -q``: the payload
    is already in memory, so it reaches the pipe in one ``write(2)`` and the reader cannot
    leave first. That reasoning holds only while the whole payload fits what the kernel
    will accept in one go -- past that, ``printf`` blocks mid-write, the reader exits, and
    the builtin takes SIGPIPE exactly like ``docker`` does.

    So the exemption is a bounded claim, and the bound is asserted rather than assumed. If
    a future clean case grows its payload past this, it stops being a case about correct
    code and starts hiding a real defect.

    Both halves are required. Without the broken half, "printf is always safe" passes; and
    that belief is what would let someone write ``printf '%s\\n' "$whole_transcript" |
    grep -q x`` and call it exempt.
    """
    assert _printf_into_head_survives(_PRINTF_SAFE_BYTES), (
        f"printf of {_PRINTF_SAFE_BYTES} B into `head -1` no longer completes under "
        "`set -euo pipefail`. The scanner's printf exemption rests on this; if even a small "
        "payload now breaks, the exemption is wrong and must be removed, not adjusted."
    )
    assert not _printf_into_head_survives(_PRINTF_BROKEN_BYTES), (
        f"printf of {_PRINTF_BROKEN_BYTES} B into `head -1` survived, so this host does not "
        "reproduce the ceiling and the exemption's bound is unmeasured here -- re-derive it "
        "rather than widening the exemption on the strength of one passing size."
    )


def test_the_widened_producer_list_is_what_finds_the_upgrade_rehearsals_abort_shape(
    tmp_path: Path,
) -> None:
    """The widening is justified by a REAL line, not by a synthetic one.

    ``scripts/release-tests/selftest-rollback-fault-injection.sh:132`` is::

        copy_line="$(grep -n '^COPY public\\.media_file ' "$WORKDIR/full.sql" | head -1 | …)"

    That file happens to run under ``set -uo pipefail`` -- no ``-e`` -- so it is not a
    finding today, and the scanner is right to stay quiet about it. But its sibling
    ``test-upgrade.sh`` DOES set ``-e`` and carried the identical shape at lines 1897 and
    2112, where a ``grep`` in producer position made both invisible to the narrow list.

    This drives the real file with ``set -e`` substituted in, which is the state a single
    edit away, and requires the widened list to see it and the narrow one not to. If the
    text filters are ever dropped from ``_CHATTY_PRODUCER``, this fails.
    """
    source = REPO_ROOT / "scripts" / "release-tests" / "selftest-rollback-fault-injection.sh"
    text = source.read_text(encoding="utf-8")
    assert "set -uo pipefail" in text, (
        f"{source.name} no longer opens with `set -uo pipefail`; this test's premise "
        "(errexit is one edit away, and is currently OFF) needs re-deriving"
    )

    variant = tmp_path / "errexit-variant.sh"
    variant.write_text(text.replace("set -uo pipefail", "set -euo pipefail", 1), encoding="utf-8")

    narrow = re.compile(
        r"\b(?:docker|git|find|ls|curl|ss|nvidia-smi|dpkg|rpm|comm|diff|kubectl|awk|sed|cat|jq)\b"
    )
    wide_offenders = _pipefail_offenders(variant)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sys.modules[__name__], "_CHATTY_PRODUCER", narrow)
        narrow_offenders = _pipefail_offenders(variant)

    assert any("aborts under set -e" in o for o in wide_offenders), (
        "the widened producer list no longer sees a `grep … | head -1` assignment -- the "
        f"shape test-upgrade.sh carried at lines 1897 and 2112.\n{wide_offenders}"
    )
    assert not narrow_offenders, (
        "the narrow producer list has started matching this line, so this test no longer "
        "measures what the widening buys; re-derive it rather than deleting it.\n"
        f"{narrow_offenders}"
    )


def test_the_extraction_this_file_depends_on_still_finds_both_probes() -> None:
    """Guard the guard: if either function is renamed or restructured, every test
    above would error at extraction rather than reporting on the probe -- and an
    error in a file nobody reads is how a dead guard survives."""
    text = OPENTR.read_text(encoding="utf-8")
    assert _has_function(text, "detect_and_configure_hardware")
    assert _has_function(text, "diar_native_container_present")
    assert _has_function(text, "docker_runtime_has_nvidia"), (
        "the runtime probe was inlined back into detect_and_configure_hardware; keep it a "
        "named function so its SIGPIPE contract stays testable in isolation"
    )


@pytest.mark.parametrize("name", ["detect_and_configure_hardware", "diar_native_container_present"])
def test_the_extracted_source_is_the_whole_function(name: str) -> None:
    """Guard the guard, part two: ``_function_source`` slices on ``\\n}\\n``, so a
    nested block closing at column 0 would silently truncate the extraction and the
    tests above would exercise a fragment."""
    source = _function_source(OPENTR.read_text(encoding="utf-8"), name)
    assert source.rstrip().endswith("}")
    assert source.count("{") == source.count("}"), (
        f"extraction of {name}() is unbalanced -- it was truncated at an inner "
        f"column-0 closing brace:\n{source}"
    )
