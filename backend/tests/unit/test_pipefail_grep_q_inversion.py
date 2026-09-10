"""`<producer> | grep -q ...` under `set -o pipefail` inverts, silently and intermittently.

``grep -q`` exits at its FIRST match. If the producer is still writing at that moment it dies
with SIGPIPE, and ``pipefail`` hands that status to the whole pipeline — so the shell sees
**failure for a pattern that MATCHED**.

⚠️ **OUTPUT SIZE IS NOT WHAT MAKES IT SAFE. That premise was measured and REFUTED on
2026-09-07, and this file was built on it.** The variable is not "does the producer outrun the
64 KB pipe buffer" — it is **whether the producer still has a write to make after the reader
leaves**, which is a question about ELAPSED TIME, not bytes. Three measurements, this host:

* a **40-byte** producer whose two writes are 2 ms apart inverted **300 / 300**;
* the same 40 bytes written back-to-back with no gap inverted **0 / 3000**;
* the real ``docker info`` — **1,609 bytes, ~40x UNDER the pipe buffer**, and previously
  exempted here on exactly that ground — inverted **12 / 3000** (~1 in 250). An earlier
  400-iteration run of the same command saw 0 and proved nothing; at that rate a null result
  is the expected outcome roughly half the time, which is precisely why "we ran it and it was
  fine" is not evidence here.

So a producer is safe from this only when it makes **no further write after the match**: an
in-memory shell builtin below the pipe buffer, or a match that lands on the producer's final
write. An external binary that queries a daemon — ``docker``, ``ss``, ``buildx`` — is never
safe merely for being terse, because the gaps between its writes are RPC round-trips.

This repo has already shipped it three times, each in the direction that reports a problem as
fine:

* ``scripts/release-tests/test-lite-mode.sh`` asked ``docker logs <celery-cpu-worker>``
  (**measured 3.9 MB on this host, 60x the buffer**) whether the diar-native sidecar had logged
  that it was serving speaker embeddings. It could never once report success — it warned "has
  not yet logged the sidecar-served message" whether or not the sidecar was working, so a real
  regression would have arrived as familiar noise.
* ``scripts/install-offline-package.sh`` asked ``dpkg -l`` (**measured 447 KB, ~7x**) whether
  the NVIDIA Container Toolkit was installed. Installed toolkit -> reported missing -> the
  offline installer configured ``use_gpu=false`` / ``cpu`` / ``int8`` on a working GPU host,
  with no error printed anywhere.
* ``scripts/release/10-preflight.sh`` asked ``docker ps`` whether the live stack was up, and
  recorded ``live-stack pass`` when the pipeline failed. Its sibling ``65-rehearse.sh`` carries
  a comment explaining why it deliberately does NOT do this; preflight did it anyway.

``scripts/tests/test-scan-not-a-pass.sh`` states the rule in a comment. A comment is not a gate,
and the file it names as its sibling contained four violations. So the rule lives here now.

The fix is always ``[ "$(producer | grep -c PATTERN)" -gt 0 ]``: ``grep -c`` consumes the whole
stream, so there is no early exit to race.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "scripts"

_PIPEFAIL_RE = re.compile(r"^\s*set\s+-[a-zA-Z]*o\s+pipefail\b", re.M)
_GREP_Q_RE = re.compile(r"\|\s*grep\s+-[a-zA-Z]*q")

# Producers that provably make NO FURTHER WRITE after the match, so there is nothing for
# `grep -q`'s early exit to break. Keyed `<repo-relative script>::<producer>` with a MANDATORY
# reason. A stale entry — one whose site no longer exists — FAILS, so this file can only
# shrink: same convention as `backend/tests/audit-allowlist.txt`.
#
# ⚠️ **"MEASURED OUTPUT SIZE" IS NOT AN ADMISSIBLE REASON ANY MORE.** Eight entries used to
# live here on that basis; every one has been converted to `grep -c` instead, because the
# module docstring's measurements show the premise is false — `docker info` at 1,609 B
# inverted 12 / 3000. `test_a_small_producer_inverts_when_its_writes_are_spaced_out` below
# pins the refutation so a size-based exemption cannot quietly come back.
#
# The only reason that admits an entry is: **the pattern can only match on the producer's
# LAST write** (or the producer is a shell builtin emitting a SMALL in-memory string — and
# that ceiling is far lower than the pipe buffer, so do not reason from 64 KB. Re-measured
# 2026-09-07 on this host, `printf 'FIRST\n<payload>\n' | head -1` under `set -euo pipefail`:
# 1 KiB 0/100 aborts, 7 KiB 0/100, **16 KiB 4/100**, 32 KiB 31/100, 60 KiB 100/100. It starts
# failing at a QUARTER of the buffer and is already intermittent there, which is the same
# time-not-size story as everything else in this file. Pinned by
# `test_opentr_docker_probe_sigpipe.py::test_the_printf_exemption_has_a_measured_ceiling`.)
SAFE_PRODUCERS: dict[str, str] = {
    'scripts/release-tests/selftest-rollback-fault-injection.sh::bash "$WORKDIR/bare-check.sh" 2>/dev/null': "the marker is the fixture's FINAL write — its only other statement redirects to /dev/null — so a match leaves the producer with nothing more to write, and no match means grep read to EOF",
    'scripts/release-tests/selftest-rollback-fault-injection.sh::bash "$WORKDIR/guarded-check.sh" 2>/dev/null': "as above — the sibling fixture, same last-write-is-the-marker structure",
}


def _producer(segment: str) -> str:
    """The command feeding `grep -q`, normalised into a stable allowlist key."""
    head = segment.rsplit("|", 1)[0].strip()
    head = re.sub(r"^(if|elif|while|until)\s+", "", head)
    head = re.sub(r"^!\s*", "", head)
    return re.sub(r"\s+", " ", head).strip()


def _logical_lines(source: str) -> list[tuple[int, str]]:
    """(line number of the first physical line, joined text) with `\\` continuations merged.

    Without this the producer sits on a different physical line from its `grep -q` and comes
    back empty — a finding that names no command, which nobody can act on and which cannot be
    given an allowlist key. Four such sites existed in `scripts/tests/test-publish-platforms.sh`.
    """
    joined: list[tuple[int, str]] = []
    buf, start = "", 0
    for lineno, line in enumerate(source.splitlines(), 1):
        if not buf:
            start = lineno
        if line.rstrip().endswith("\\"):
            buf += line.rstrip()[:-1] + " "
            continue
        joined.append((start, buf + line))
        buf = ""
    if buf:
        joined.append((start, buf))
    return joined


def _scan(source: str) -> list[tuple[int, str]]:
    """(line number, producer) for every `| grep -q` outside a comment."""
    if not _PIPEFAIL_RE.search(source):
        return []
    found: list[tuple[int, str]] = []
    for lineno, line in _logical_lines(source):
        if line.lstrip().startswith("#"):
            continue
        for match in _GREP_Q_RE.finditer(line):
            found.append((lineno, _producer(line[: match.start() + 1])))
    return found


def _live_sites() -> list[tuple[str, int, str]]:
    sites: list[tuple[str, int, str]] = []
    for path in sorted(SCRIPTS_DIR.rglob("*.sh")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno, producer in _scan(path.read_text(encoding="utf-8", errors="replace")):
            sites.append((rel, lineno, producer))
    return sites


def test_no_unmeasured_grep_q_under_pipefail() -> None:
    offenders = [
        f"{rel}:{lineno}  producer: {producer}"
        for rel, lineno, producer in _live_sites()
        if f"{rel}::{producer}" not in SAFE_PRODUCERS
    ]
    assert not offenders, (
        "`<producer> | grep -q ...` in a script that sets `pipefail`. grep -q exits at its "
        "first match, so the producer can die with SIGPIPE and the pipeline reports FAILURE "
        "for a pattern that MATCHED — silently and intermittently, at ANY output size: "
        "`docker info` is 1,609 B here (~40x UNDER the pipe buffer) and inverted 12/3000.\n  "
        + "\n  ".join(offenders)
        + "\n"
        'Rewrite as `[ "$(producer | grep -c PATTERN)" -gt 0 ]` (grep -c reads the whole '
        "stream). A SAFE_PRODUCERS entry is admissible ONLY on the grounds that the pattern "
        "can only match on the producer's FINAL write — never on measured output size, which "
        "was refuted on 2026-09-07 (see this module's docstring)."
    )


def test_allowlist_has_no_stale_entries() -> None:
    """An exemption must not outlive the site it exempts."""
    live = {f"{rel}::{producer}" for rel, _lineno, producer in _live_sites()}
    stale = sorted(key for key in SAFE_PRODUCERS if key not in live)
    assert not stale, (
        "SAFE_PRODUCERS entries whose `| grep -q` site no longer exists — delete them in the "
        "same commit that removed the site, or the next one to appear at that key is exempted "
        "without anyone deciding it should be:\n  " + "\n  ".join(stale)
    )


def test_the_scanner_discriminates() -> None:
    """Must-fire and must-stay-clean: a detector matching nothing reads as a clean tree."""
    fires = "set -euo pipefail\nif docker logs c 2>&1 | grep -q 'ready'; then :; fi\n"
    assert _scan(fires) == [(2, "docker logs c 2>&1")], "must-fire case did not fire"

    # Same line, but the script never enables pipefail -> the hazard does not exist.
    assert _scan("if docker logs c 2>&1 | grep -q 'ready'; then :; fi\n") == []
    # The fixed idiom must not be reported.
    assert (
        _scan('set -euo pipefail\nif [ "$(docker logs c | grep -c x)" -gt 0 ]; then :; fi\n') == []
    )
    # A commented-out example must not be reported (this repo documents the bad form in prose).
    assert _scan("set -euo pipefail\n# never write: docker logs c | grep -q x\n") == []

    # A `\` continuation must resolve to the real producer, not an empty string, and must be
    # attributed to the line the statement STARTS on.
    continued = 'set -euo pipefail\nif printf "%s" "$c" \\\n     | grep -qE "x"; then :; fi\n'
    assert _scan(continued) == [(2, 'printf "%s" "$c"')], (
        "a backslash-continued pipeline reported the wrong producer or line — the finding "
        "would name no command and could not be allowlisted"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash to run the control")
def test_the_inversion_is_real_not_folklore() -> None:
    """Execute both idioms against a >64 KB producer and observe the flip.

    Without this, the rule above is an assertion about bash that nothing here checks. It is
    also what pins the *direction*: the bad idiom reports NO-MATCH for input that matches.
    """
    script = textwrap.dedent(
        """
        set -euo pipefail
        producer() { seq 1 20000 | sed 's/^/opentranscribe-container-/'; }
        if producer | grep -q 'opentranscribe-container-1'; then echo "QUIET=matched"; else echo "QUIET=no-match"; fi
        if [ "$(producer | grep -c 'opentranscribe-container-1')" -gt 0 ]; then echo "COUNT=matched"; else echo "COUNT=no-match"; fi
        """
    )
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60).stdout

    assert "COUNT=matched" in out, (
        "the `grep -c` form failed to match a pattern that is present — the control itself is "
        f"broken, so the comparison below proves nothing. Output:\n{out}"
    )
    assert "QUIET=no-match" in out, (
        "`producer | grep -q` did NOT invert here, so this platform's pipe buffering differs "
        "from the one the rule was measured on. Re-measure before relaxing the rule rather "
        f"than deleting it. Output:\n{out}"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash to run the control")
def test_a_small_producer_inverts_when_its_writes_are_spaced_out() -> None:
    """The measurement that REFUTES "small output is safe", pinned as an executable control.

    This file's ``SAFE_PRODUCERS`` used to exempt eight sites on the grounds that their
    producer had been *measured* at a few hundred bytes to a couple of KB, i.e. comfortably
    inside the 64 KB pipe buffer. That reasoning is wrong, and it is wrong in the direction
    that leaves live inversions in a release gate: ``docker info`` is 1,609 B here and still
    inverted 12 / 3000 in a real run, because the bytes are not the variable — **whether the
    producer still has a write to make when the reader leaves** is, and the gaps between
    ``docker info``'s writes are daemon round-trips.

    Both halves are asserted, because either alone is satisfiable by a broken control:

    * a **40-byte** producer with a 2 ms gap between its two writes inverts EVERY time;
    * the identical 40 bytes written back-to-back never inverts.

    Same size, opposite outcomes. Anyone tempted to re-admit a size-based exemption has to
    delete this test first.
    """
    script = textwrap.dedent(
        """
        set -uo pipefail
        # 40 bytes total -- ~1600x under the 64 KB pipe buffer -- but with real time between
        # the matching write and the last one, as any binary querying a daemon has.
        slow=0
        for _ in $(seq 1 25); do
          if { printf 'Runtimes: nvidia runc\\n'; sleep 0.002; printf 'Kernel: 6.8\\n'; } \\
             | grep -q nvidia; then :; else slow=$((slow+1)); fi
        done
        echo "SLOW_INVERSIONS=$slow"

        # The control: same bytes, no gap.
        fast=0
        for _ in $(seq 1 25); do
          if { printf 'Runtimes: nvidia runc\\n'; printf 'Kernel: 6.8\\n'; } \\
             | grep -q nvidia; then :; else fast=$((fast+1)); fi
        done
        echo "FAST_INVERSIONS=$fast"
        """
    )
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120).stdout

    assert "FAST_INVERSIONS=0" in out, (
        "the no-gap control itself inverted, so this platform cannot distinguish the two "
        f"cases and the comparison below proves nothing. Output:\n{out}"
    )
    assert "SLOW_INVERSIONS=25" in out, (
        "a 40-BYTE producer with a 2 ms gap between its writes did NOT invert here. That is "
        "the whole basis for refusing 'measured N bytes' as a SAFE_PRODUCERS reason, so "
        "re-measure on this platform before re-admitting one — do not simply relax this "
        f"assertion. Output:\n{out}"
    )
