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


def _extract_function(name: str) -> str:
    """Lift a function out of the shipped script, brace-matched from its `name() {` line.

    Extracting rather than re-typing is the point: a copy in this file would keep passing
    after the real one changed, which is the failure mode this whole file exists to avoid.
    """
    lines = REHEARSE.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}() {{"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start : end + 1])


#: A `docker` that records every invocation and answers the two queries the teardown makes.
#: `$FAKE_LOG` collects argv one line per call; container queries return ids, so the stop/rm
#: path is actually taken rather than short-circuiting on an empty list.
FAKE_DOCKER = """#!/bin/bash
printf '%s\\n' "$*" >> "$FAKE_LOG"
case "$1 $2" in
    "ps -q"|"ps -aq") printf 'cid1\\ncid2\\n' ;;
    "volume ls")      printf '%s\\n' "${FAKE_VOLUMES:-}" ;;
esac
exit 0
"""


def _run_teardown(tmp_path: Path, fake_volumes: str = "") -> tuple[list[str], str]:
    """Execute the REAL teardown function with a fake docker on PATH.

    Returns (docker invocations, stderr).
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(FAKE_DOCKER, encoding="utf-8")
    (bindir / "docker").chmod(0o755)
    log = tmp_path / "docker.log"
    log.touch()

    script = f'YELLOW=""; NC=""\n{_extract_function("teardown_scenario_stack_on_interrupt")}\nteardown_scenario_stack_on_interrupt\n'
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env={
            "PATH": f"{bindir}:/usr/bin:/bin",
            "FAKE_LOG": str(log),
            "FAKE_VOLUMES": fake_volumes,
        },
        check=True,
    )
    return log.read_text(encoding="utf-8").splitlines(), result.stderr


def test_the_teardown_is_scoped_by_label_and_never_by_name(tmp_path: Path):
    """THE property. A bare name grep once destroyed an unrelated container here (#693).

    Every query the teardown issues must carry the release-test label filter, so a
    container the rehearsal did not create cannot be in the set it stops.
    """
    calls, _ = _run_teardown(tmp_path)

    queries = [c for c in calls if c.startswith("ps ")]
    assert queries, "the teardown issued no container query at all"
    for query in queries:
        assert "--filter label=com.opentranscribe.release-test" in query, (
            f"{query!r} selects containers without the release-test label — that set can "
            "include the live dev stack, or an unrelated container on this host"
        )


def test_it_stops_and_removes_the_containers_it_found(tmp_path: Path):
    """MUST FIRE: a teardown that queries and then does nothing leaves the next run blocked."""
    calls, _ = _run_teardown(tmp_path)

    assert "stop cid1 cid2" in calls, f"containers were never stopped: {calls}"
    assert "rm cid1 cid2" in calls, f"stopped containers were never removed: {calls}"


def test_it_never_deletes_a_volume(tmp_path: Path):
    """Deleting volumes under interrupt would bypass the live-data marker check.

    `lib/guardrails.sh` re-checks every volume for `.opentranscribe-live-data` before
    removing it. Reimplementing that inside a trap handler is how a rehearsal deletes
    someone's real data; naming the volumes is enough.
    """
    calls, stderr = _run_teardown(tmp_path, fake_volumes="opentranscribe_postgres_data")

    assert not [c for c in calls if c.startswith("volume rm")], (
        f"the teardown removed a volume: {calls}"
    )
    assert "opentranscribe_postgres_data" in stderr, (
        "leftover volumes must be NAMED for the operator"
    )
    assert "OT_RELEASE_TEST_RESET_VOLUMES=1" in stderr, "and the remedy printed alongside them"


def test_a_clean_exit_touches_nothing(tmp_path: Path):
    """CONTROL: the trap fires on EVERY exit, including a successful rehearsal.

    Each scenario cleans up after itself, so on the normal path the query comes back
    empty and the function must return before stopping anything. Without this, a green
    run's EXIT trap would issue docker writes against whatever else was running.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(
        '#!/bin/bash\nprintf \'%s\\n\' "$*" >> "$FAKE_LOG"\nexit 0\n', encoding="utf-8"
    )
    (bindir / "docker").chmod(0o755)
    log = tmp_path / "docker.log"
    log.touch()

    script = f'YELLOW=""; NC=""\n{_extract_function("teardown_scenario_stack_on_interrupt")}\nteardown_scenario_stack_on_interrupt\n'
    subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env={"PATH": f"{bindir}:/usr/bin:/bin", "FAKE_LOG": str(log)},
        check=True,
    )

    calls = log.read_text(encoding="utf-8").splitlines()
    assert not [c for c in calls if c.startswith(("stop", "rm", "volume rm"))], (
        f"nothing was running, yet the teardown issued write operations: {calls}"
    )


def test_the_teardown_runs_before_the_dev_stack_restart():
    """Ordering matters: the scenario stack binds the SAME stock ports the dev stack wants.

    Structural — the race needs Docker to observe. What it pins is that the call sits
    above the `./opentr.sh start dev` line inside `restore_live_stack`.
    """
    restore = _extract_function("restore_live_stack")

    teardown_at = restore.index("teardown_scenario_stack_on_interrupt")
    restart_at = restore.index("./opentr.sh start dev")
    assert teardown_at < restart_at, (
        "the dev stack is restarted before the scenario containers are stopped — they "
        "hold 5173-5180, so the restart races them for the ports"
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
