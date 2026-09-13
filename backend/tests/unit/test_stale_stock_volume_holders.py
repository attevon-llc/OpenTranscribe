"""A blocked stock-volume removal must NAME its blocker, and reclaim only what it owns.

`gr_check_stale_stock_volumes` removes the stock-named volumes a previous release
test left behind. Until 2026-09-13 it ran `docker volume rm ... >/dev/null 2>&1`
and, on failure, died with a parenthesised guess:

    ✗ FATAL: could not remove opentranscribe_postgres_data (still in use?)

That is what the v0.5.0 rehearsal's Scenario A died of. The guess was even
correct — but unactionable, and it pointed away from the real defect: the
preflight's container check (`gr_check_no_live_stack`) only looks at **running**
containers, while a STOPPED container still holds its volume. So a run that
aborted mid-flight passes the check and then blocks the removal. The scenario's
own cleanup removed the identical volume seconds later, because that path removes
containers first.

Two properties are pinned here, and they pull in opposite directions on purpose:

* the blocker must be NAMED (docker's own error, plus the holding containers), and
* a holder may be removed ONLY when it is provably a previous release test's —
  the `com.opentranscribe.release-test` label. These volumes carry *production*
  names; "it looked like ours" is exactly the reasoning that must never authorise
  a removal here, and a bare name grep has already destroyed an unrelated
  container on this host (issue #693).

The functions are executed as bash against a fake `docker` on PATH, not read for
keywords: a guard that greps its own source cannot tell a call from a comment.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
GUARDRAILS = REPO_ROOT / "scripts" / "release-tests" / "lib" / "guardrails.sh"

#: A `docker` that records argv and answers the three queries these functions make.
#: `$HOLDERS` is the container list a volume reports; `$LABELED` names the subset
#: that carries the release-test label. `volume rm` fails while any holder remains,
#: exactly as the real daemon does.
FAKE_DOCKER = r"""#!/bin/bash
printf '%s\n' "$*" >> "$FAKE_LOG"

remaining() {                      # holders minus those already removed
    local c out=""
    for c in ${HOLDERS:-}; do
        grep -qxF "removed:$c" "$FAKE_STATE" 2>/dev/null && continue
        out="$out $c"
    done
    printf '%s' "${out# }"
}

case "$1 $2" in
    "ps -a")
        printf '%s\n' $(remaining)
        ;;
    "volume inspect")
        exit 0
        ;;
    "volume rm")
        if [[ -n "$(remaining)" ]]; then
            echo "Error response from daemon: remove $3: volume is in use - [abc123]" >&2
            exit 1
        fi
        exit 0
        ;;
    "inspect "*)
        # Real call shape is `docker inspect <name> --format <fmt>`, so the
        # container is $2. Matching on "$1 $2" would need the literal name here.
        for c in ${LABELED:-}; do
            [[ "$c" == "$2" ]] && { printf 'fresh-install\n'; exit 0; }
        done
        printf '<no value>\n'
        ;;
    "stop "*|"rm "*)
        printf 'removed:%s\n' "$2" >> "$FAKE_STATE"
        ;;
esac
exit 0
"""

#: Minimal stand-ins so the functions can run outside the full harness.
_HARNESS_PRELUDE = """
gr_log()  { echo "[log] $*" >&2; }
gr_ok()   { echo "[ok] $*"  >&2; }
gr_warn() { echo "[warn] $*" >&2; }
gr_die()  { echo "[die] $*" >&2; exit 9; }
gr_volume_has_live_marker() { return 1; }   # 1 == "no live-data marker"
gr_live_marker_reason() { echo "reason"; }
GR_PROTECTED_VOLUMES=(postgres_data)
GR_STOCK_PROJECT=opentranscribe
OT_RELEASE_TEST_RESET_VOLUMES=1
"""


def _extract_function(name: str) -> str:
    """Lift a function out of the shipped script, from `name() {` to its closing brace."""
    lines = GUARDRAILS.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}() {{"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start : end + 1])


def _run(tmp_path: Path, holders: str, labeled: str) -> tuple[int, str, list[str]]:
    """Run the real gr_check_stale_stock_volumes. Returns (rc, stderr, docker argv)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(FAKE_DOCKER, encoding="utf-8")
    (bindir / "docker").chmod(0o755)
    log = tmp_path / "docker.log"
    log.touch()
    state = tmp_path / "state"
    state.touch()

    script = "\n".join(
        [
            _HARNESS_PRELUDE,
            _extract_function("gr_volume_holders"),
            _extract_function("gr_reclaim_aborted_run_holders"),
            _extract_function("gr_check_stale_stock_volumes"),
            "gr_check_stale_stock_volumes",
        ]
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env={
            "PATH": f"{bindir}:/usr/bin:/bin",
            "FAKE_LOG": str(log),
            "FAKE_STATE": str(state),
            "HOLDERS": holders,
            "LABELED": labeled,
        },
    )
    return result.returncode, result.stderr, log.read_text(encoding="utf-8").splitlines()


def test_an_aborted_runs_container_is_reclaimed_so_the_volume_can_go(tmp_path: Path):
    """THE property. A previous release test's leftovers must not block the next run."""
    rc, stderr, calls = _run(
        tmp_path, holders="opentranscribe-postgres", labeled="opentranscribe-postgres"
    )

    assert rc == 0, f"removal still failed after reclaiming an owned holder: {stderr}"
    assert "stop opentranscribe-postgres" in calls, f"holder was never stopped: {calls}"
    assert "rm opentranscribe-postgres" in calls, f"holder was never removed: {calls}"
    assert "removed stale volume opentranscribe_postgres_data" in stderr


def test_it_never_force_kills_a_container(tmp_path: Path):
    """`docker stop` then `docker rm` — `rm -f` is SIGKILL, and that has wedged this GPU."""
    _, _, calls = _run(
        tmp_path, holders="opentranscribe-postgres", labeled="opentranscribe-postgres"
    )

    # Outside the loop: an empty `calls` would satisfy every assertion below while
    # proving nothing. This run MUST have reclaimed a holder, so it must have issued
    # both verbs — that is what makes the absence of `rm -f` meaningful.
    assert "stop opentranscribe-postgres" in calls, f"nothing was stopped: {calls}"
    assert "rm opentranscribe-postgres" in calls, f"nothing was removed: {calls}"

    forceful = [c for c in calls if c.startswith(("rm -f", "kill ", "stop -t 0"))]
    assert not forceful, f"a forceful teardown was used: {forceful}"


def test_an_unlabeled_holder_is_left_alone_and_named(tmp_path: Path):
    """The volumes carry PRODUCTION names, so only the label may authorise a removal.

    A holder this harness cannot prove it created must survive, and the operator
    must be told which container to deal with — not handed "(still in use?)".
    """
    rc, stderr, calls = _run(tmp_path, holders="somebody-elses-postgres", labeled="")

    assert rc == 9, "an unremovable volume must still be fatal"
    assert not [c for c in calls if c.startswith(("stop ", "rm "))], (
        f"an unlabeled container was removed: {calls}"
    )
    assert "somebody-elses-postgres" in stderr, (
        "the blocking container must be NAMED — that is the whole point of the change"
    )
    assert "volume is in use" in stderr, "docker's own error must be surfaced, not discarded"


def test_the_fake_daemon_can_actually_refuse(tmp_path: Path):
    """GUARD THE GUARD: if `volume rm` always succeeded, every test above is vacuous.

    Same holder set as the passing case, but nothing is labeled, so nothing is
    reclaimed — the removal must fail. This is what proves the success in
    test_an_aborted_runs_container_is_reclaimed... came from the reclaim and not
    from a stub that never says no.
    """
    rc, _, _ = _run(tmp_path, holders="opentranscribe-postgres", labeled="")
    assert rc == 9, "the fake daemon accepted a removal it should have refused"


def test_a_volume_with_no_holder_is_removed_directly(tmp_path: Path):
    """CONTROL: the ordinary case must not have acquired a dependency on reclaiming."""
    rc, stderr, calls = _run(tmp_path, holders="", labeled="")

    assert rc == 0, f"a free volume failed to be removed: {stderr}"
    assert not [c for c in calls if c.startswith(("stop ", "rm "))], (
        f"containers were touched when nothing held the volume: {calls}"
    )
