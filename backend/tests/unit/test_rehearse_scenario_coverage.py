"""Every release scenario script must actually be RUN by the rehearse stage.

`scripts/release-tests/test-lite-mode.sh` existed, was complete, called itself "Scenario C"
in its own banner — and nothing under `scripts/release/` ever invoked it. So
`docker-build-push.sh all` BUILT the lite image and a release would have PUBLISHED it with
zero release-time functional evidence behind it (issue #667).

That is not a small gap for this component in particular: the full/CUDA image publishes no
arm64 manifest, so `arm64_deployment_preflight()` defaults every arm64 host to
`DEPLOYMENT_MODE=lite`. Lite is the only deployment those users can install, and it was the
one shape the rehearsal never exercised.

The coverage check is DERIVED from the filenames rather than listing the three scenarios,
because a hand-maintained list is what would have to be updated by the same person who
forgot to wire the scenario in. A fourth scenario script added later is covered the moment
it lands, or this fails.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
REHEARSE = REPO_ROOT / "scripts" / "release" / "65-rehearse.sh"
RELEASE_TESTS = REPO_ROOT / "scripts" / "release-tests"

# `selftest-*.sh` exercise the harness's own guardrails against throwaway containers and are
# deliberately NOT release scenarios; `provision-test-media.sh` is a fixture helper the
# preflight stage calls. Only `test-*.sh` are deployment scenarios.
SCENARIO_GLOB = "test-*.sh"


def scenario_scripts() -> list[str]:
    return sorted(p.name for p in RELEASE_TESTS.glob(SCENARIO_GLOB))


def test_the_scenario_glob_actually_finds_the_known_scenarios():
    """Guard the guard: a glob that matched nothing would make every check below vacuous."""
    found = scenario_scripts()
    for expected in ("test-fresh-install.sh", "test-upgrade.sh", "test-lite-mode.sh"):
        assert expected in found, f"{expected} missing from {found} — the glob is wrong"
    assert not any(n.startswith("selftest-") for n in found), (
        "selftest-*.sh are harness self-tests, not release scenarios, and must not be "
        "required to run in the rehearsal"
    )


@pytest.mark.parametrize("scenario", scenario_scripts())
def test_every_release_scenario_script_is_invoked_by_the_rehearse_stage(scenario):
    text = REHEARSE.read_text()
    # Strip comments: the stage explains WHY Scenario C exists by naming the script, and a
    # mention in prose is not an invocation.
    code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    assert f"release-tests/{scenario}" in code, (
        f"{scenario} is a release scenario that 65-rehearse.sh never runs. A scenario nothing "
        f"invokes is not coverage — it is a script that happens to exist. Either wire it into "
        f"the stage or move it out of scripts/release-tests/test-*.sh."
    )


@pytest.mark.parametrize("scenario", scenario_scripts())
def test_every_scenario_runs_non_interactively(scenario):
    """`--yes` is mandatory: the `I UNDERSTAND` prompt has no tty in a pipeline run.

    Without it the scenario dies with `No such device or address`, which reads as a scenario
    failure rather than a harness misconfiguration.
    """
    code = "\n".join(line.split("#", 1)[0] for line in REHEARSE.read_text().splitlines())
    invocations = [ln for ln in code.splitlines() if f"release-tests/{scenario}" in ln]
    assert invocations, f"no invocation of {scenario} to check"

    # A call site may pass --yes itself, or delegate to the teardown helper, which appends
    # `--cleanup --yes` to whatever it is handed. Both are non-interactive; requiring the flag
    # to appear literally on every line would fail the helper form for no reason.
    # Look for the helper's forwarding line directly. A brace-matching regex over the function
    # body does NOT work here: the body interpolates "${BLUE}", so `[^}]*` terminates on that
    # colour variable long before reaching the forwarded arguments.
    helper_passes_yes = bool(re.search(r'"\$@"\s+--cleanup\s+--yes', code))
    for line in invocations:
        if "--yes" in line:
            continue
        assert "teardown_scenario" in line, (
            f"{scenario} is invoked without --yes and not via the teardown helper:\n"
            f"  {line.strip()}\n"
            f"The confirmation prompt has no tty here and would fail with "
            f"'No such device or address'."
        )
        assert helper_passes_yes, (
            f"{scenario} is invoked via teardown_scenario, but that helper no longer passes "
            f"--yes — every scenario teardown would hang on the confirmation prompt."
        )


def test_every_scenarys_result_reaches_the_stage_exit_code():
    """A scenario whose rc is collected but never consulted is a scenario that cannot fail.

    Each `<name>_rc` variable must appear in all three aggregation branches (4 = operator
    abort, 3 = precondition, 1 = failed), or that scenario's verdict is silently discarded.
    """
    text = REHEARSE.read_text()
    rc_vars = sorted(set(re.findall(r"\b(\w+_rc)=0\b", text)))
    assert len(rc_vars) >= 3, f"expected one rc variable per scenario, found {rc_vars}"

    for branch_marker in ("-eq 4", "-eq 3", "-ne 0"):
        # The aggregation block is the run of `if/elif` lines carrying these comparisons.
        branch_lines = [ln for ln in text.splitlines() if branch_marker in ln and "_rc" in ln]
        assert branch_lines, f"no aggregation branch found for '{branch_marker}'"
        joined = " ".join(branch_lines)
        missing = [v for v in rc_vars if v not in joined]
        assert not missing, (
            f"{missing} never reach the '{branch_marker}' branch of the exit-code "
            f"aggregation, so those scenarios cannot affect the stage's verdict."
        )


def test_the_json_criteria_name_every_scenario():
    """`--json` is a machine contract; a scenario absent from criteria[] is invisible to it.

    This used to grep the script for literal `"id":"fresh-install"` fragments, which was only
    ever true while the stage hand-assembled its criteria[] inline. It now records through
    criteria-lib.sh and emits `$(criteria_json)`, so the literal check went stale — and a
    check that greps for a shape the code no longer uses fails on a correct refactor while
    saying nothing about the property it was written to protect.

    The property is: for every scenario, an outcome reaches criteria[]. That is `record <id>`
    plus `criteria_json` in the emitted payload.
    """
    text = REHEARSE.read_text()
    code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())

    assert '"criteria":[%s]' in code, (
        "the rehearse stage's --json payload no longer carries a criteria[] field"
    )
    assert "criteria_json" in code, (
        "criteria[] is declared in the printf format string but nothing fills it"
    )
    for expected_id in ("fresh-install", "upgrade-from-previous", "lite-mode"):
        assert re.search(rf"\brecord\s+{re.escape(expected_id)}\b", code), (
            f"no `record {expected_id}` — that scenario contributes nothing to criteria[], "
            f"so a caller reading --json would not know whether it ran"
        )
