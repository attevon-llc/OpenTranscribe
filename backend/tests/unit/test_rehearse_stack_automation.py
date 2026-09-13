"""Safety properties of the rehearsal's automatic stack stop/restart.

`65-rehearse.sh` may now stop the live stack, rehearse, and restart it. That is a
convenience with teeth: it takes down a running deployment and interrupts anything
transcribing. These tests pin the three properties that make it safe, by executing
the decision as bash rather than by reading the source for keywords.

What is NOT covered here: the scenarios themselves, and the restart actually
succeeding. Both need Docker and a real stack. Stated so nobody reads a green run as
"the rehearsal is proven".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
REHEARSE = REPO_ROOT / "scripts" / "release" / "65-rehearse.sh"

#: The decision, lifted verbatim in shape from 65-rehearse.sh. Kept in sync by
#: ``test_the_decision_here_matches_the_shipped_one``.
DECISION = """
auto_stop=false
if [[ "${ASSUME_YES:-false}" == "true" || "${RELEASE_AUTO_STOP_STACK:-false}" == "true" ]]; then
    auto_stop=true
elif [[ -t 0 ]]; then
    auto_stop=prompted
fi
echo "$auto_stop"
"""


def _decide(env: dict[str, str]) -> str:
    """Run the decision with stdin closed — i.e. the unattended case."""
    return subprocess.run(
        ["bash", "-c", DECISION],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", **env},
        check=True,
    ).stdout.strip()


def test_unattended_with_no_approval_refuses_rather_than_stopping_the_stack():
    """THE property. A cron/CI caller must never silently take a deployment down."""
    assert _decide({}) == "false", (
        "with no --yes, no RELEASE_AUTO_STOP_STACK and no tty, the rehearsal must fall "
        "through to its exit-3 refusal — anything else lets an unattended run stop "
        "someone's stack with no human in the loop"
    )


@pytest.mark.parametrize(
    "env",
    [{"ASSUME_YES": "true"}, {"RELEASE_AUTO_STOP_STACK": "true"}],
    ids=["--yes", "RELEASE_AUTO_STOP_STACK"],
)
def test_explicit_human_approval_enables_the_automatic_stop(env: dict[str, str]):
    """MUST FIRE — otherwise the automation silently never engages."""
    assert _decide(env) == "true"


def test_a_falsy_value_is_not_approval():
    """CONTROL: only the literal "true" approves, so an exported empty/`0` is not consent."""
    assert _decide({"RELEASE_AUTO_STOP_STACK": "0"}) == "false"
    assert _decide({"ASSUME_YES": ""}) == "false"


def test_the_restart_is_guarded_and_runs_even_on_failure_or_interrupt():
    """The restart must be trapped, and must not fire for a stack we did not stop.

    Structural, deliberately: firing the trap for real needs Docker. What it pins is
    the pair that turns "we stopped it" into "we put it back" — a trap covering the
    abnormal exits, and a guard so a stack the operator had already stopped is not
    started up underneath them.
    """
    source = REHEARSE.read_text(encoding="utf-8")

    assert "trap restore_live_stack EXIT INT TERM" in source, (
        "restore must run from an EXIT/INT/TERM trap — on the happy path only, a failed "
        "scenario or a Ctrl-C leaves the operator's stack down with no hint why"
    )
    assert "STACK_STOPPED_BY_US=false" in source
    assert '[[ "$STACK_STOPPED_BY_US" == "true" ]] || return 0' in source, (
        "restore must be guarded: starting a stack the operator had deliberately stopped "
        "is its own surprise"
    )


def test_it_never_reaches_for_kill():
    """`./opentr.sh stop` only. A SIGKILLed CUDA context has wedged this host twice.

    Scans EXECUTABLE lines, not the whole file. The first draft matched raw source and
    fired on the comment that explains *why* these are forbidden — a detector that
    cannot tell code from prose would force the explanation to be deleted to stay
    green, which is precisely backwards.
    """
    code_lines = [
        line
        for line in REHEARSE.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]

    for forbidden in ("pkill", "killall", "kill -9", "docker kill"):
        offenders = [line.strip() for line in code_lines if forbidden in line]
        assert not offenders, (
            f"{forbidden!r} is executed in the rehearsal stage: {offenders}. Stopping the "
            "stack must go through ./opentr.sh stop, which is graceful and preserves data."
        )


def test_the_kill_detector_can_actually_fire():
    """GUARD THE GUARD: a scanner that matches nothing passes everything.

    Without this, stripping comments above could have over-stripped and left the test
    green against a file that really did `pkill`.
    """
    code_lines = ["  docker kill $(docker ps -q)", "# docker kill — never do this"]
    offenders = [
        line.strip()
        for line in code_lines
        if not line.lstrip().startswith("#") and "docker kill" in line
    ]

    assert offenders == ["docker kill $(docker ps -q)"], (
        "the comment-stripping scan must still catch a real call, and must ignore the commented one"
    )


def test_the_decision_here_matches_the_shipped_one():
    """Guard against this file's copy of the decision drifting from the real one."""
    source = REHEARSE.read_text(encoding="utf-8")

    assert (
        'if [[ "${ASSUME_YES:-false}" == "true" || '
        '"${RELEASE_AUTO_STOP_STACK:-false}" == "true" ]]; then' in source
    ), (
        "the approval condition in 65-rehearse.sh no longer matches the one exercised "
        "above, so these tests would be asserting against a decision that is not shipped"
    )
    assert "elif [[ -t 0 ]]; then" in source
