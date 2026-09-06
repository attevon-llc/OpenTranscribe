"""`<big producer> | grep -q ...` under `set -o pipefail` inverts, silently and by size.

``grep -q`` exits at its FIRST match. If the producer is still writing at that moment it dies
with SIGPIPE, and ``pipefail`` hands that status to the whole pipeline — so the shell sees
**failure for a pattern that MATCHED**. Whether it fires depends only on whether the producer
outruns the 64 KB pipe buffer, which makes it a landmine: the idiom works on small output,
keeps working through review, and flips the day the producer grows.

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

# Producers measured to fit well inside the 64 KB pipe buffer, so `grep -q` cannot outrun them.
# Keyed `<repo-relative script>::<producer>` with a MANDATORY measured reason. A stale entry —
# one whose site no longer exists — FAILS, so this file can only shrink: same convention as
# `backend/tests/audit-allowlist.txt`.
SAFE_PRODUCERS: dict[str, str] = {
    "scripts/build-all.sh::docker info": "measured 1,609 B on this host — `docker info` is a fixed-size summary, not a listing",
    "scripts/pki/run-pki-e2e-leg.sh::docker ps -a --format '{{.Names}}'": "measured 1,832 B with 20 containers up; container names are bounded by the compose project",
    'scripts/release/10-preflight.sh::docker buildx inspect "$BUILDER" 2>/dev/null': "buildx inspect prints one builder's node table, a few hundred bytes",
    "scripts/release-tests/lib/api-client.sh::docker ps --format '{{.Names}}' 2>/dev/null": "running-container names only; same bound as the pki leg above",
    "scripts/release-tests/lib/compose-chain.sh::docker info 2>/dev/null": "as build-all.sh — fixed-size summary",
    "scripts/release-tests/lib/guardrails.sh::ss -tlnH 2>/dev/null | awk '{print $4}'": "listening sockets, already narrowed to one column by awk; hundreds of bytes",
    'scripts/release-tests/selftest-rollback-fault-injection.sh::bash "$WORKDIR/bare-check.sh" 2>/dev/null': "a fixture script this test writes itself, whose entire output is two marker lines",
    'scripts/release-tests/selftest-rollback-fault-injection.sh::bash "$WORKDIR/guarded-check.sh" 2>/dev/null': "as above — the sibling fixture, same two-line output",
    'scripts/run-auth-e2e.sh::docker ps -q --filter "name=${PKI_CONTAINER_NAME}" 2>/dev/null': "`-q` prints short ids for a single-name filter; at most a few dozen bytes",
    "scripts/test-watch-e2e.sh::docker ps --format '{{.Names}}'": "running-container names only; same bound as the pki leg above",
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
        "for a pattern that MATCHED — silently, and only once the producer outgrows the 64 KB "
        "pipe buffer.\n  " + "\n  ".join(offenders) + "\n"
        'Rewrite as `[ "$(producer | grep -c PATTERN)" -gt 0 ]` (grep -c reads the whole '
        "stream), or add a SAFE_PRODUCERS entry with the producer's MEASURED output size."
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
