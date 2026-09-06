"""Every release stage that DERIVES its work list must fail closed on an empty one.

The release stages deliberately stopped hardcoding their component/platform lists
(issue #680: ``backend frontend docs`` silently never covered ``lite``). They now derive
them at runtime from ``security-scan.sh list-repos`` and
``docker-build-push.sh list-platforms``, read through ``done < <(cmd)``.

That construct has a specific hazard, and it bit us: **the reader runs in this shell but
``cmd`` runs in a SUBSHELL**, so a non-zero exit from the deriving command — or any
output shape the loop cannot parse — is invisible. The loop reads EOF, iterates zero
times, leaves its accumulator empty, and every downstream "for each item" check then
iterates over nothing and finds nothing wrong. `set -e` does not help: the subshell's
status is not the loop's.

Measured, with the real shape reduced to a harness::

    OLD:  STAGE PASSED having checked 0 components     exit=0
    NEW:  GUARD FIRED: refusing to scan an empty component set   exit=3

So an underivable list made the stage **pass having verified nothing** — the exact
"green gate that measured nothing" shape issue #431's tooling exists to catch, and the
same distinction issue #681 drew for the scanner itself: *"scanned, findings tolerable"
and "never scanned" are different outcomes and must stay that way.*

This test pins the guard on every such loop. It is deliberately structural (a grep over
the stage scripts) rather than an execution test: driving the real stages needs local
multi-GB images, Docker Hub credentials, or the live stack stopped, none of which belong
in the fast unit suite.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_DIR = REPO_ROOT / "scripts" / "release"

# (script, the accumulator the loop fills, a regex proving an emptiness guard exists)
#
# Each entry names a real derive-loop. The guard regex is intentionally loose about
# formatting and strict about the ONE thing that matters: something compares the
# accumulator against zero after the loop.
GUARDED_LOOPS: list[tuple[str, str, str]] = [
    (
        "50-scan.sh",
        "REPO_FOR_COMPONENT",
        r"\$\{#REPO_FOR_COMPONENT\[@\]\}\s*==\s*0",
    ),
    ("50-scan.sh", "legs_verified", r"legs_verified\s*==\s*0"),
    ("40-build.sh", "legs_declared", r"legs_declared\s*==\s*0"),
    ("40-build.sh", "baked_checked", r"baked_checked\s*==\s*0"),
    # 80-publish.sh guards the same hazard in a DIFFERENT SHAPE, and that is fine --
    # what matters is that zero iterations cannot read as a pass, not where the check
    # sits. Its loop feeds `report_check` into per-bucket counters, and `check_verdict`
    # returns `not-measured` ("no ... were checked at all") when CHECK_RAN is 0, rather
    # than the caller testing an accumulator after the loop. Found by the coverage test
    # below, then verified by reading `check_verdict` -- worth stating because the
    # stage that PUBLISHES is the one where "passed having checked nothing" would cost
    # the most.
    ("80-publish.sh", "CHECK_RAN", r"CHECK_RAN\[\$bucket\]\s*==\s*0"),
]


@pytest.mark.parametrize(("script", "accumulator", "guard_re"), GUARDED_LOOPS)
def test_derive_loop_fails_closed_on_an_empty_list(
    script: str, accumulator: str, guard_re: str
) -> None:
    path = RELEASE_DIR / script
    assert path.exists(), f"{script} is missing -- did the stage get renamed?"
    source = path.read_text(encoding="utf-8")

    assert re.search(guard_re, source), (
        f"{script} fills '{accumulator}' from a `done < <(...)` derive loop but never "
        f"checks whether it came back EMPTY. A non-zero exit from the deriving command "
        f"is invisible to the reader (it runs in a subshell), so the loop iterates zero "
        f"times and every downstream per-item check passes over nothing -- the stage "
        f"reports success having verified NOTHING. Add a zero-check that fails closed "
        f"(exit 3, or `record <id> not-measured`), the way the sibling loops do."
    )


def test_the_guard_scanner_would_notice_a_regression() -> None:
    """Positive control: the patterns must not match text that lacks the guard.

    A detector that matches nothing reports zero findings, which is indistinguishable
    from a clean tree -- the failure mode `audit-tests.py --selftest` exists for. Prove
    each pattern actually discriminates.
    """
    unguarded = (
        "declare -A REPO_FOR_COMPONENT=()\n"
        "while IFS=$'\\t' read -r component repo; do\n"
        '    REPO_FOR_COMPONENT["$component"]="$repo"\n'
        "done < <(./scripts/security-scan.sh list-repos)\n"
        "missing=()\n"
    )
    for _script, _accumulator, guard_re in GUARDED_LOOPS:
        assert not re.search(guard_re, unguarded), (
            f"guard pattern {guard_re!r} matches source that has NO emptiness check -- "
            "it would pass against the very regression it exists to catch"
        )


def test_every_derive_loop_in_the_release_dir_is_covered() -> None:
    """The list above must not go stale as new derive loops are added.

    `security-scan.sh list-repos` / `docker-build-push.sh list-platforms` are the two
    commands whose output shape drives stage work lists. Any NEW `done < <(...)` reading
    either of them needs its own guard and its own row above -- otherwise this test
    silently stops covering the thing it is named for.
    """
    derive_cmds = ("list-repos", "list-platforms")
    found: set[tuple[str, str]] = set()
    for path in sorted(RELEASE_DIR.glob("*.sh")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if "done < <(" in line and any(cmd in line for cmd in derive_cmds):
                found.add((path.name, line.strip()))

    covered_scripts = {script for script, _, _ in GUARDED_LOOPS}
    uncovered = sorted(f"{name}: {line}" for name, line in found if name not in covered_scripts)
    assert not uncovered, (
        "release stage scripts derive a work list from list-repos/list-platforms in a "
        "script that GUARDED_LOOPS does not cover:\n  " + "\n  ".join(uncovered) + "\n"
        "Add an emptiness guard to that loop and a row to GUARDED_LOOPS, or the stage "
        "can pass having derived nothing."
    )
