"""Discarding stderr on a command substitution obliges you to handle its failure.

Under ``set -euo pipefail`` a command-substitution **assignment** whose pipeline fails aborts
the script. With ``2>/dev/null`` it does so printing **nothing**: the phase simply stops, the
EXIT traps run, and the log shows a missing rest-of-phase with no error and no FAIL line.

Measured 2026-09-07 — the rehearsal's ``upgrade`` scenario, ``from-v0.3.3`` hop. Phase 15
printed every assertion up to and including ``PASS R-6`` and then ended. Phases 16-18 (the
``update --rollback`` tail, ~20 assertions) never ran. Reproduced exactly::

    f() { head="$(docker exec no-such-container psql 2>/dev/null | tr -d ' ')"; echo unreached; }
    f                       # prints nothing after; rc=1
    # with `|| head=""`     # continues, head=''

⚠️ **The two halves are individually reasonable and jointly contradictory.** ``2>/dev/null``
declares "this command may fail and I do not want the noise"; leaving the failure to ``set -e``
declares "if it fails, kill everything". A site doing both has stated two incompatible
intentions, and the code around it usually proves which was meant — the R-8 probes already read
``${total:-?}``, i.e. they were written expecting an empty result they could never actually
receive.

⚠️ **This is NOT the SIGPIPE family** that ``test_opentr_docker_probe_sigpipe.py`` covers, and
the two must not be merged. That one is about a *reader* (``grep -q``, ``head -N``) exiting
early and killing the producer; here the reader is ``tr``, which reads to EOF, and the failure
is the *producer* returning non-zero. Different cause, different fix, different scanner —
which is precisely why the existing scanner did not catch these five sites despite already
listing ``test-upgrade.sh`` among the files it inspects.

"Safe by call site" is not accepted as a guard: ``gr_host_platform`` was only safe because
every caller happened to invoke it inside an ``if``, which is a property the next caller can
remove without touching this file.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_TESTS = REPO_ROOT / "scripts" / "release-tests"

pytestmark = pytest.mark.skipif(
    not RELEASE_TESTS.is_dir() or shutil.which("bash") is None,
    reason="scripts/release-tests or bash is not present in this checkout",
)

# NAME="$( ... 2>/dev/null ... )" on one line, optionally `local`-declared.
_ASSIGN = re.compile(r'^\s*(?:local\s+)?[A-Za-z_][A-Za-z0-9_]*="\$\(.*2>/dev/null.*\)"\s*$')


def _unguarded_sites() -> list[str]:
    out: list[str] = []
    for script in sorted(RELEASE_TESTS.rglob("*.sh")):
        for n, line in enumerate(script.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            if _ASSIGN.match(line) and "||" not in line:
                out.append(f"{script.relative_to(REPO_ROOT)}:{n}: {line.strip()[:120]}")
    return out


def test_no_stderr_discarding_assignment_is_left_unguarded():
    offenders = _unguarded_sites()
    assert not offenders, (
        "these assignments discard stderr (declaring the command may fail) but leave the "
        "failure to `set -e`, which aborts the phase SILENTLY — no error, no FAIL line, "
        'just a missing rest-of-phase. Add `|| NAME=""` and let the existing empty-value '
        "handling report it:\n  " + "\n  ".join(offenders)
    )


def test_the_scanner_can_fire(tmp_path: Path):
    """Guard the guard: a detector matching nothing is indistinguishable from a clean tree."""
    victim = tmp_path / "bad.sh"
    victim.write_text(
        "    head=\"$(docker exec c psql -tA 2>/dev/null | tr -d '[:space:]')\"\n",
        encoding="utf-8",
    )
    line = victim.read_text(encoding="utf-8").splitlines()[0]
    assert _ASSIGN.match(line) and "||" not in line, (
        "the pattern no longer matches the exact shape that truncated phases 16-18"
    )

    fixed = tmp_path / "good.sh"
    fixed.write_text(
        '    head="$(docker exec c psql -tA 2>/dev/null | tr -d \'[:space:]\')" || head=""\n',
        encoding="utf-8",
    )
    good = fixed.read_text(encoding="utf-8").splitlines()[0]
    assert not (_ASSIGN.match(good) and "||" not in good), (
        "the guarded form is still reported — the check would be unsatisfiable"
    )


def _run(body: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", textwrap.dedent(body)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_the_unguarded_shape_really_does_abort_silently():
    """The must-fire control, stated as behaviour rather than as a story.

    Uses `false` rather than docker so it needs no daemon and cannot be flaky.
    """
    result = _run("""
        set -euo pipefail
        f() {
          echo "R-6 pass"
          local head
          head="$(false 2>/dev/null | tr -d '[:space:]')"
          echo "R-7 reached"
        }
        f
        echo "AFTER"
    """)
    assert "R-6 pass" in result.stdout
    assert "R-7 reached" not in result.stdout, (
        "an unguarded stderr-discarding assignment no longer aborts — bash semantics "
        "changed, and this module plus the five guards it protects should be re-checked "
        "rather than left asserting a hazard that no longer exists"
    )
    assert result.stderr.strip() == "", (
        f"the abort is supposed to be SILENT (that is what made it expensive to find); "
        f"got stderr: {result.stderr!r}"
    )


def test_the_guarded_shape_continues_with_an_empty_value():
    result = _run("""
        set -euo pipefail
        f() {
          echo "R-6 pass"
          local head
          head="$(false 2>/dev/null | tr -d '[:space:]')" || head=""
          echo "R-7 reached head='$head'"
        }
        f
        echo "AFTER"
    """)
    assert "R-7 reached head=''" in result.stdout, (
        f"the guard does not let the phase continue with an empty value:\n{result.stdout}"
    )
    assert "AFTER" in result.stdout


def test_a_bare_condition_and_command_is_not_this_hazard():
    """Pin the boundary, because it was assumed wrong here once.

    `[[ -n "$x" ]] && cmd` mid-function looks like the same shape and is NOT: bash exempts
    it from `set -e`. Recording the measurement stops a future reader "fixing" the wrong
    line, as happened on 2026-09-07 before this was measured.
    """
    result = _run("""
        set -euo pipefail
        f() { local x=""; echo "before"; [[ -n "$x" ]] && echo warn; echo "after"; }
        f
        echo "AFTER"
    """)
    assert "after" in result.stdout and "AFTER" in result.stdout, (
        "a mid-function `cond && cmd` now aborts under set -e; the boundary this module "
        f"documents has moved and the guards should be revisited:\n{result.stdout}"
    )
