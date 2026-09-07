"""The local frontend gate must run the frontend TESTS, not only the type-checker.

189 vitest files and ~1,750 tests ran in **no local gate at all**. The only invocations of
``npm run test`` / ``test:audit`` / ``test:audit:selftest`` in the whole repo were three steps
in ``.github/workflows/pre-commit.yml`` — CI only. ``scripts/frontend-check.sh`` ran eslint,
``check:i18n``, ``svelte-check`` and (outside ``--check-only``) ``vite build``, and nothing in
``.pre-commit-config.yaml`` ran vitest either.

Both local callers pass ``--check-only``: ``scripts/run-dev-tests.sh``'s frontend phase, and
``scripts/test-matrix.sh``'s leg 1.4. So a green ``run-dev-tests.sh --full`` said nothing
whatsoever about 1,750 frontend tests, and the **pre-release rehearsal was equally blind** —
a defect could reach a release tag with every local gate green and only the PR's CI job able to
see it. Measured 2026-09-07 the moment it was wired in: 2 real failures in
``src/routes/login/page.test.ts``.

This module pins three properties, because losing any one restores the hole:

1. the three commands are invoked at all;
2. they are invoked **inside** ``--check-only`` (outside it, both local callers skip them and
   nothing changes);
3. a failure actually fails the script — asserted by RUNNING the real ``run_step`` against a
   failing command, not by reading it.

Point 3 has a specific history. The post-Claude-fix recheck used to carry a hand-written copy
of the check list, so a step added to the first pass and not to the copy would be silently
dropped as soon as an auto-fix succeeded: the recheck would pass on the two steps it knew about
and print "All frontend checks passed after Claude auto-fix" over a failing test suite. The
duplication is gone; test 4 below is what keeps it gone.
"""

from __future__ import annotations

import re
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "frontend-check.sh"

pytestmark = pytest.mark.skipif(
    not SCRIPT.is_file(), reason="scripts/frontend-check.sh not in this checkout"
)

#: The npm scripts that carry the frontend's actual test evidence, and what each is for.
_REQUIRED_STEPS = {
    "npm run test": "the vitest suite itself — 189 files, ~1,750 tests",
    "npm run test:audit:selftest": (
        "the auditor's own 27 cases. It runs FIRST because a detector that stops matching "
        "reports zero findings, which is indistinguishable from a clean suite"
    ),
    "npm run test:audit": "the frontend test-quality auditor (10 detectors)",
}


def _source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _function_body(name: str) -> str:
    """The body of a shell function, up to its closing brace at column 0."""
    source = _source()
    marker = f"{name}() {{"
    assert marker in source, f"{name}() is not defined in frontend-check.sh"
    after = source.split(marker, 1)[1]
    body = after.split("\n}", 1)[0]
    assert body.strip(), f"{name}() parsed as empty — every check below would be vacuous"
    return body


def _uncommented(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


# --------------------------------------------------------------------------------------
# 1 + 2: the commands are invoked, and they are inside --check-only.
# --------------------------------------------------------------------------------------


def test_the_check_set_runs_vitest_and_both_auditors():
    body = _uncommented(_function_body("run_all_checks"))
    missing = [cmd for cmd in _REQUIRED_STEPS if cmd not in body]
    assert not missing, (
        f"scripts/frontend-check.sh no longer invokes: {missing}. Purpose of each: "
        f"{ {k: v for k, v in _REQUIRED_STEPS.items() if k in missing} }. Without them this "
        "script proves the frontend COMPILES and nothing about whether it works, and — since "
        "run-dev-tests.sh and test-matrix.sh are its only local callers — nothing else locally "
        "runs the frontend suite either."
    )


def test_the_test_steps_are_not_gated_behind_the_build_flag():
    """`--check-only` must still run them, or both local callers skip them.

    ``run-dev-tests.sh --full`` and ``test-matrix.sh`` leg 1.4 both pass ``--check-only``.
    ``--check-only`` exists to skip the ``vite build`` (91.8 s of a 328 s whole-tree run,
    issue #688) — not to skip the tests, which measured 31.8 s for all three combined.
    """
    body = _uncommented(_function_body("run_all_checks"))
    build_guard = re.search(r'if\s+\[\s+"\$BUILD_ENABLED"\s*=\s*true\s+\]', body)
    assert build_guard, "the BUILD_ENABLED guard is gone — --check-only no longer skips anything"

    inside_build = body[build_guard.start() :]
    gated = [cmd for cmd in _REQUIRED_STEPS if cmd in inside_build]
    assert not gated, (
        f"these are inside the BUILD_ENABLED guard, so --check-only skips them: {gated}. Both "
        "local callers pass --check-only, so that is identical to not running them at all."
    )
    # ...and the guard is not vacuous: the build itself must still be behind it.
    assert "npm run build" in inside_build, (
        "npm run build is no longer inside the BUILD_ENABLED guard, so --check-only would run "
        "the 91.8 s vite build and the assertion above would pass for the wrong reason"
    )


# --------------------------------------------------------------------------------------
# 3: a failure actually fails the script. Executed, not read.
# --------------------------------------------------------------------------------------


def _drive_run_step(command: str) -> tuple[str, str]:
    """Source the REAL script (without running main) and drive `run_step` once."""
    harness = textwrap.dedent(
        f"""
        set -- --no-claude --check-only
        # Strip the trailing `main` invocation so sourcing defines functions without running
        # the gate. Everything else — set -euo pipefail, run_step, run_all_checks — is the
        # real code, which is the point: a re-implementation here would pass against a script
        # that no longer propagates failures.
        source <(sed 's/^main$//' {SCRIPT!s})
        # Sourcing a process substitution makes BASH_SOURCE `/dev/fd/N`, so the script's own
        # SCRIPT_DIR/FRONTEND_DIR resolution lands on `/dev/frontend`. Re-point it: run_step's
        # `cd` would otherwise fail for every command and make the must-fire case below pass
        # for the wrong reason (it did, before this line).
        FRONTEND_DIR={REPO_ROOT!s}/frontend
        run_step "driven" "" {command}
        echo "CHECK_FAILED=$CHECK_FAILED"
        echo "CHECK_OUTPUT_LEN=${{#CHECK_OUTPUT}}"
        """
    )
    proc = subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, timeout=60, cwd=str(REPO_ROOT)
    )
    return proc.stdout, proc.stderr


def test_a_failing_step_sets_the_failure_flag():
    """Must-fire: a non-zero step marks the run failed and captures its output."""
    out, err = _drive_run_step("bash -c 'echo boom >&2; exit 1'")
    assert "CHECK_FAILED=true" in out, f"a failing step did not fail the run.\n{out}\n{err}"
    assert "CHECK_OUTPUT_LEN=0" not in out, "a failing step recorded no output to report"


def test_a_passing_step_leaves_the_run_clean():
    """Must-stay-clean: without this, a `run_step` hardcoded to fail would pass the test above."""
    out, err = _drive_run_step("true")
    assert "CHECK_FAILED=false" in out, f"a passing step marked the run failed.\n{out}\n{err}"
    assert "CHECK_OUTPUT_LEN=0" in out, "a passing step recorded failure output"


# --------------------------------------------------------------------------------------
# 4: one check list, not two.
# --------------------------------------------------------------------------------------


def test_the_recheck_reuses_the_same_check_set():
    """The recheck after a Claude auto-fix must not re-list a subset of the steps.

    A subset there means: auto-fix succeeds, recheck passes on the steps it knows about, and
    the script returns 0 with "All frontend checks passed after Claude auto-fix" while a test
    suite it never re-ran is red.
    """
    main_body = _uncommented(_function_body("main"))
    assert main_body.count("run_all_checks") >= 2, (
        "main() calls run_all_checks fewer than twice — the first pass and the "
        "recheck-after-Claude-fix must both go through the same list"
    )
    strays = [cmd for cmd in _REQUIRED_STEPS if cmd in main_body]
    strays += [c for c in ("npm run build", "npx svelte-check") if c in main_body]
    assert not strays, (
        f"main() invokes checks directly instead of via run_all_checks: {strays}. A second "
        "hand-written list is how a step gets added to one path and forgotten in the other."
    )
