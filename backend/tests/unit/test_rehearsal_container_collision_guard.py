"""The rehearsal's preflight must see a container that will collide, whatever project owns it.

Closes #899.

``lib/guardrails.sh``'s "is anything in the way" check filtered by **compose project label**
(``opentranscribe``, ``transcribe-app``). But docker refuses to create a container whose
**name** is already taken, *whatever project owns it* — so the label is not the precondition.

A previous scenario's own stack is precisely the case this misses: it runs under project
``ot-reltest-lite`` / ``ot-reltest-fresh`` while using the stock ``opentranscribe-*`` container
NAMES. Measured 2026-09-09, the guard printed::

    [guardrails] ✓ no live opentranscribe-*/transcribe-app-* containers running
    [guardrails] ✗ FATAL: required ports already in use: 5173 5174 ... 5199

Both lines describe the same 18 containers. The port guard caught what the container guard
could not, so nothing shipped broken — but the ✓ had already sent the operator looking
somewhere else, which is the whole cost of a check that reports clear without having looked.

⚠️ **The obvious fix is wrong.** ``--filter 'name=^opentranscribe-'`` would false-positive on
an unrelated ``opentranscribe-homepage``, which the original comment in that file calls out and
is why the label filter was chosen. The set is therefore DERIVED from ``docker-compose.yml``'s
own ``container_name:`` declarations — exactly the names this scenario will try to create, so
it is both complete (any project) and precise (nothing else).

⚠️ **An interpolated entry is RESOLVED, not excluded.** The first version of this dropped every
``${...}`` name on the theory that those workers move with the project — but all three
gpu-scale/gpu-split entries read ``${COMPOSE_PROJECT_NAME:-opentranscribe}``, so on any stack
that did not set the variable they are named exactly what a stock install creates, and they
collide. That left 3 of 19 names unguarded. ``${VAR:-default}`` is substituted with its
default; ``${VAR}`` with no default is dropped, since nothing here can know its value.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
GUARDRAILS = REPO_ROOT / "scripts" / "release-tests" / "lib" / "guardrails.sh"
COMPOSE = REPO_ROOT / "docker-compose.yml"

pytestmark = pytest.mark.skipif(
    not GUARDRAILS.is_file() or not COMPOSE.is_file(),
    reason="guardrails.sh or docker-compose.yml is not present in this checkout",
)


def _helper_proc(
    tmp_path: Path,
    fake_docker_names: list[str],
    ps_flag: str = "",
    repo_root: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Drive the REAL bash helper with a fake ``docker`` on PATH.

    A fake binary rather than a mock so the function under test is the shipped one — a
    re-implementation here would pass while the real guard stayed blind.

    The helper is called through a trailing ``echo RC=$?`` so an abort under
    ``set -euo pipefail`` is distinguishable from a clean empty result. Those look
    identical on stdout and are the failure this file exists to prevent.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "docker"
    fake.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/bash
            # Only answers `docker ps [-a] --format {{{{.Names}}}}`.
            printf '%s\\n' {" ".join(repr(n) for n in fake_docker_names) or "''"}
            """
        ).replace("'", '"'),
        encoding="utf-8",
    )
    fake.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["GR_REPO_ROOT"] = str(repo_root or REPO_ROOT)

    script = (
        f'source "{GUARDRAILS}" >/dev/null 2>&1 || true\n'
        f'gr_colliding_container_names "{ps_flag}"\n'
        'echo "HELPER_RC=$?"\n'
    )
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60
    )


def _run_helper(tmp_path: Path, fake_docker_names: list[str], ps_flag: str = "") -> list[str]:
    """The names the helper reported. Fails loudly if it aborted instead of returning."""
    out = _helper_proc(tmp_path, fake_docker_names, ps_flag)
    assert "HELPER_RC=" in out.stdout, (
        "the helper aborted the shell instead of returning — under `set -euo pipefail` "
        f"that kills the whole scenario script.\nstdout: {out.stdout}\nstderr: {out.stderr}"
    )
    return [
        line
        for line in out.stdout.splitlines()
        if line.strip() and not line.startswith("HELPER_RC=")
    ]


def test_the_helper_exists_at_all():
    """Guard the guard: a missing function would make every case below vacuously empty."""
    assert "gr_colliding_container_names()" in GUARDRAILS.read_text(encoding="utf-8"), (
        "gr_colliding_container_names is gone; the collision sweep this module verifies "
        "has been removed or renamed"
    )


def test_it_catches_a_stock_name_owned_by_another_compose_project(tmp_path: Path):
    """THE #899 case: the previous scenario's stack, invisible to a label filter."""
    found = _run_helper(tmp_path, ["opentranscribe-backend", "opentranscribe-postgres"])
    assert "opentranscribe-backend" in found, (
        "a container named opentranscribe-backend was not reported as colliding. This is the "
        "exact shape that produced '✓ no live containers' beside 'FATAL: ports already in "
        f"use' — got {found}"
    )
    assert "opentranscribe-postgres" in found


def test_an_unrelated_container_sharing_the_prefix_is_not_reported(tmp_path: Path):
    """Must-stay-clean, and the reason a bare name-prefix filter was rejected.

    `opentranscribe-homepage` is not a name this scenario creates, so it cannot collide and
    must not refuse the run — the false positive the original label filter was avoiding.
    """
    found = _run_helper(tmp_path, ["opentranscribe-homepage", "opentranscribe-marketing"])
    assert found == [], (
        f"unrelated containers sharing the name prefix were reported as collisions: {found}"
    )


def test_nothing_running_reports_nothing(tmp_path: Path):
    assert _run_helper(tmp_path, []) == []


def test_the_derived_name_set_is_real_and_non_trivial():
    """If the compose parse returned nothing, every assertion above would pass vacuously."""
    names = [
        line.split("container_name:", 1)[1].strip()
        for line in COMPOSE.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("container_name:")
    ]
    literal = [n for n in names if "${" not in n]
    assert len(literal) >= 10, (
        f"only {len(literal)} literal container_name entries parsed from docker-compose.yml; "
        "the sweep would be nearly empty and would miss real collisions"
    )
    assert "opentranscribe-backend" in literal


def test_an_interpolated_name_is_resolved_to_its_default_not_dropped(tmp_path: Path):
    """The gpu-scale/gpu-split workers DO collide, and the first draft could not see them.

    `container_name: ${COMPOSE_PROJECT_NAME:-opentranscribe}-celery-worker-gpu-scaled`
    resolves to `opentranscribe-celery-worker-gpu-scaled` on any stack that did not set the
    variable — which is exactly what a leftover `./opentr.sh start dev --gpu-scale` worker
    is called. Dropping every `${...}` entry left 3 of the 19 declared names unguarded;
    keeping them raw is worse still, since the literal `${...}` string matches no container
    and would imply coverage that is absent.
    """
    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    (fake_repo / "docker-compose.yml").write_text(
        "services:\n"
        "  scaled:\n"
        "    container_name: ${COMPOSE_PROJECT_NAME:-opentranscribe}-celery-worker-gpu-scaled\n",
        encoding="utf-8",
    )
    out = _helper_proc(tmp_path, ["opentranscribe-celery-worker-gpu-scaled"], repo_root=fake_repo)
    assert "opentranscribe-celery-worker-gpu-scaled" in out.stdout, (
        "a leftover gpu-scale worker was not reported as colliding; docker will refuse to "
        f"create that name and the preflight will have said the coast was clear.\n{out.stdout!r}"
    )


def test_a_name_with_no_resolvable_default_is_dropped(tmp_path: Path):
    """Must-stay-clean. Nothing here can know `${FOO}`'s value, and guessing would be the
    `name=^opentranscribe-` false-positive hazard arriving by another route."""
    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    (fake_repo / "docker-compose.yml").write_text(
        "services:\n"
        "  a:\n"
        "    container_name: ${SOME_UNSET_VAR}-worker\n"
        "  b:\n"
        "    container_name: opentranscribe-backend\n",
        encoding="utf-8",
    )
    found = [
        line
        for line in _helper_proc(
            tmp_path, ["opentranscribe-backend", "-worker"], repo_root=fake_repo
        ).stdout.splitlines()
        if line.strip() and not line.startswith("HELPER_RC=")
    ]
    assert found == ["opentranscribe-backend"], (
        f"an unresolvable interpolation leaked into the comparison set: {found}"
    )


def test_every_declared_name_reaches_the_comparison_set(tmp_path: Path):
    """The set must cover ALL of docker-compose.yml, interpolated entries included.

    A count is the only thing that catches "resolved 16 of 19 and reported clear" — the
    exact shape of the bug this replaced, which every by-name assertion missed.
    """
    declared = {
        line.split("container_name:", 1)[1].strip()
        for line in COMPOSE.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("container_name:")
    }
    expected = sorted(
        {re.sub(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-([^}]*)\}", r"\1", n) for n in declared}
    )
    assert not any("${" in n for n in expected), (
        "a declared container_name carries no resolvable default, so this test's own "
        f"oracle would hide it: {[n for n in expected if '${' in n]}"
    )
    found = _run_helper(tmp_path, expected)
    assert sorted(found) == expected, (
        "the helper does not cover every name docker-compose.yml declares; missing "
        f"{sorted(set(expected) - set(found))}"
    )


def test_a_compose_file_with_no_literal_names_returns_instead_of_aborting(tmp_path: Path):
    """`grep -v` exits 1 when it filters EVERYTHING out, and that must not kill the run.

    The name set is built by `sed … | grep -v '\\${' | sed … | sort -u`, assigned inside a
    file that runs `set -euo pipefail`. A compose file whose every `container_name` is
    project-interpolated makes `grep -v` output nothing and exit 1; pipefail promotes that
    to the pipeline's status and `set -e` aborts **the sourcing script** — so the guard
    added to stop a rehearsal starting blind would instead stop it starting at all, at the
    point of a routine safety check, with no error message of its own.

    The correct answer for that input is "no collidable names", returned normally.
    """
    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    (fake_repo / "docker-compose.yml").write_text(
        "services:\n"
        "  worker-a:\n"
        "    container_name: ${COMPOSE_PROJECT_NAME:-opentranscribe}-worker-a\n"
        "  worker-b:\n"
        "    container_name: ${COMPOSE_PROJECT_NAME:-opentranscribe}-worker-b\n",
        encoding="utf-8",
    )
    out = _helper_proc(tmp_path, ["opentranscribe-backend"], repo_root=fake_repo)
    assert "HELPER_RC=" in out.stdout, (
        "the helper aborted the shell on a compose file with no literal container_name. "
        "Every caller sources guardrails.sh under `set -euo pipefail`, so this takes the "
        f"whole scenario down.\nstdout: {out.stdout!r}\nstderr: {out.stderr!r}"
    )
    assert "HELPER_RC=0" in out.stdout, f"expected a clean return, got {out.stdout!r}"


def test_an_empty_compose_file_returns_instead_of_aborting(tmp_path: Path):
    """Same hazard one stage earlier: `sed` finds nothing, so `grep -v` has no input."""
    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    (fake_repo / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    out = _helper_proc(tmp_path, ["opentranscribe-backend"], repo_root=fake_repo)
    assert "HELPER_RC=0" in out.stdout, (
        f"aborted on a compose file declaring no container_name at all: {out.stdout!r} "
        f"{out.stderr!r}"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_the_helper_does_not_pipe_docker_into_a_short_circuiting_reader():
    """Repo-wide hazard: `docker ps | grep -q` under pipefail turns a match into a non-match.

    This function's whole job is detecting presence, so that inversion would restore the very
    false all-clear #899 is about.
    """
    text = GUARDRAILS.read_text(encoding="utf-8")
    start = text.find("gr_colliding_container_names()")
    body = text[start : text.find("\n}", start)]
    for bad in ("| grep -q", "| head -", "| sed '1q'"):
        assert bad not in body, (
            f"gr_colliding_container_names pipes docker into {bad!r}; under `set -o pipefail` "
            "the producer can die of SIGPIPE and a present container reads as absent"
        )


# --------------------------------------------------------- #900: cleanup must not lie
#
# ⚠️ These are BEHAVIOURAL — they run the real `gr_cleanup` against a fake `docker` and
# judge it on its exit code, its output and the commands it issued. Two earlier drafts
# asserted on the shell source instead and BOTH were unfalsifiable against HEAD: one
# matched the remedy command inside the failure *message* it is required to print, and
# `"gr_die" in body.split("_leftover")[-1]` degenerated to `"gr_die" in body` when
# `_leftover` was absent — which is exactly the pre-fix code it was supposed to catch.
# Verified 2026-09-09: every test below fails against HEAD.


@dataclass
class CleanupRun:
    """What `gr_cleanup` did: how it exited, what it said, and what it ran."""

    returncode: int
    stdout: str
    stderr: str
    invocations: list[str]

    def issued(self, *terms: str) -> list[str]:
        return [c for c in self.invocations if all(t in c for t in terms)]


def _run_cleanup(tmp_path: Path, leftover_names: list[str]) -> CleanupRun:
    """Drive the REAL ``gr_cleanup`` with every docker call intercepted and recorded.

    The fake answers ``docker ps --format`` (the leftover sweep) with ``leftover_names``
    and every other query with nothing — which is a faithful model of the #900 incident:
    the containers belong to the stock ``opentranscribe`` project, so the *labelled*
    ``ps -aq --filter label=…`` sweep genuinely matches none of them.

    Nothing here can touch a real container, volume or path: docker is replaced, the
    ownership stamp points at a nonexistent file, and ``TEST_ROOT`` is unset so the
    ``rm -rf`` step is skipped entirely.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls_log = tmp_path / "docker-calls.log"
    listing = "".join(f"printf '%s\\n' {name!r}\n" for name in leftover_names) or ":\n"

    (bindir / "docker").write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$*" >> {calls_log}\n'
        'if [[ "$1" == "ps" && "$*" == *"--format"* ]]; then\n'
        f"{listing}"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (bindir / "docker").chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["GR_REPO_ROOT"] = str(REPO_ROOT)
    env["TEST_SCENARIO"] = "unit"
    env["TEST_PROJECT_NAME"] = "ot-reltest-unit"
    env["TEST_LABEL"] = "ot.release-test=ot-reltest-unit"
    env["TEST_PORTS"] = ""
    env["GR_OWNED_STAMP"] = str(tmp_path / "no-such-stamp")
    env.pop("TEST_ROOT", None)

    proc = subprocess.run(
        ["bash", "-c", f'source "{GUARDRAILS}"\ngr_cleanup\n'],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    invocations = calls_log.read_text(encoding="utf-8").splitlines() if calls_log.exists() else []
    assert invocations, (
        "the fake docker was never called — gr_cleanup did not run at all, so nothing "
        f"below would mean anything.\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    return CleanupRun(proc.returncode, proc.stdout, proc.stderr, invocations)


def test_a_clean_sweep_still_reports_success(tmp_path: Path):
    """MUST-STAY-CLEAN control. Without it, "always fails" would pass every test below.

    This is the case #900's fix must not break: nothing left behind, so the ✓ is earned.
    """
    run = _run_cleanup(tmp_path, [])
    assert run.returncode == 0, (
        "gr_cleanup now fails on a genuinely clean sweep — the leftover check is firing "
        f"on nothing, which would block every rehearsal.\n{run.stdout}\n{run.stderr}"
    )
    assert "cleanup complete" in run.stdout


def test_cleanup_with_a_stock_stack_still_up_is_not_reported_as_success(tmp_path: Path):
    """Closes #900 — the incident, reproduced.

    Measured 2026-09-09 after an interrupted fresh-install: ``gr_cleanup`` printed
    ``✓ cleanup complete`` and exited **0** with 14 containers still running, because its
    labelled sweep targets ``${TEST_PROJECT_NAME}`` while the installer's containers run
    under the stock ``opentranscribe`` project. The ⚠ volume refusals immediately above the
    ✓ were the evidence — they refuse *because* those containers still hold the volumes —
    and nothing joined the two facts up. The next rehearsal then failed its preconditions
    for a reason that had nothing to do with the release.

    A warning is not enough: the caller's exit code is what the next stage reads.
    """
    run = _run_cleanup(tmp_path, ["opentranscribe-backend", "opentranscribe-postgres"])
    assert run.returncode != 0, (
        "gr_cleanup exited 0 with the stock stack still up. --cleanup reports success, and "
        f"the next run's preflight refuses to start with no stated cause.\n{run.stdout}"
    )
    assert "cleanup complete" not in run.stdout, (
        "a ✓ was printed anyway; that line is what an operator reads, not the exit code"
    )
    assert "opentranscribe-backend" in run.stderr, (
        "the failure does not name what was left behind, which is the whole diagnostic"
    )


def test_the_failure_names_the_command_that_fixes_it(tmp_path: Path):
    """Recovering from this state required working the command out by hand once already."""
    run = _run_cleanup(tmp_path, ["opentranscribe-backend"])
    assert "docker compose -p opentranscribe down" in run.stderr, (
        f"no remedy offered; operator is left to work it out again:\n{run.stderr}"
    )


def test_cleanup_does_not_auto_destroy_an_unowned_stack(tmp_path: Path):
    """Deliberate limit: ``opentranscribe`` is also a real production install's project name.

    ``--cleanup`` runs no preflight asserting the stack under that name is a test one, so
    tearing down containers this run never recorded owning would be a worse bug than the
    one being fixed — the same reasoning the volume sweep already applies ("leaving $vol
    alone — it existed before this run"). Report and refuse; let the operator act.

    Judged on what was RUN, not on what the source says: the remedy command is required to
    appear in the failure message by the test above, so a text scan cannot tell the two
    apart.
    """
    run = _run_cleanup(tmp_path, ["opentranscribe-backend"])
    # Guard the guard: if the leftover branch never ran, "issued no down" is vacuous.
    assert run.returncode != 0, (
        f"the leftover branch did not run, so this test proves nothing.\n{run.stdout}"
    )
    offending = run.issued("compose", " down")
    assert not offending, (
        "gr_cleanup EXECUTED a compose down against a stack it does not own. That project "
        "name is also used by real production installs and this function has no preflight "
        f"proving otherwise: {offending}"
    )
    assert not run.issued("volume", "rm"), (
        f"gr_cleanup removed a volume the leftover containers still hold: {run.invocations}"
    )


# ───────────────────────── the wiring: a helper nothing reads looks exactly like a fixed bug


def _run_container_check(tmp_path: Path, labelled: list[str], named: list[str]):
    """Drive the REAL ``gr_check_container_names`` with a fake docker.

    The fake models the #899 shape precisely: the **label** filters match nothing (the
    leftover stack runs under ``ot-reltest-lite``, not a stock project), while an unfiltered
    ``docker ps --format`` lists stock ``opentranscribe-*`` NAMES. Under the old check that
    combination printed a ✓.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    lab = "".join(f"printf '%s\\n' {n!r}\n" for n in labelled) or ":\n"
    nam = "".join(f"printf '%s\\n' {n!r}\n" for n in named) or ":\n"
    (bindir / "docker").write_text(
        "#!/bin/bash\n"
        'if [[ "$1" == "ps" && "$*" == *"--filter"* ]]; then\n'
        f"{lab}"
        'elif [[ "$1" == "ps" ]]; then\n'
        f"{nam}"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (bindir / "docker").chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["GR_REPO_ROOT"] = str(REPO_ROOT)
    env["TEST_SCENARIO"] = "unit"
    env["TEST_PROJECT_NAME"] = "ot-reltest-unit"
    env["TEST_LABEL"] = "ot.release-test=ot-reltest-unit"
    env["TEST_PORTS"] = ""
    env.pop("TEST_ROOT", None)

    return subprocess.run(
        ["bash", "-c", f'source "{GUARDRAILS}"\ngr_check_container_names\n'],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_the_preflight_refuses_on_a_name_collision_no_label_matches(tmp_path: Path):
    """THE #899 integration case, and the one the helper's own tests cannot prove.

    ``running_named=$(gr_colliding_container_names …)`` could be assigned and never folded
    into the decision, and every helper test above would still pass — a variable nothing
    reads looks identical to a fixed bug. This asserts the *verdict*.
    """
    proc = _run_container_check(tmp_path, labelled=[], named=["opentranscribe-backend"])
    assert proc.returncode != 0, (
        "the preflight reported clear with opentranscribe-backend standing. That is the "
        "measured #899 state: a ✓ printed immediately above 'FATAL: ports already in "
        f"use'.\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    assert "opentranscribe-backend" in (proc.stdout + proc.stderr), (
        "the refusal does not name the container in the way"
    )


def test_the_preflight_still_passes_on_a_genuinely_clear_field(tmp_path: Path):
    """MUST-STAY-CLEAN. Without this, a guard that always refuses satisfies the test above.

    An unrelated `opentranscribe-homepage` is present on purpose: it shares the prefix, is
    not a name this scenario creates, and must not block the run.
    """
    proc = _run_container_check(tmp_path, labelled=[], named=["opentranscribe-homepage"])
    assert proc.returncode == 0, (
        "the preflight refused a clear field — an unrelated container sharing the name "
        f"prefix must not block a rehearsal.\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    )


def test_a_stopped_collider_is_reported_too(tmp_path: Path):
    """A stopped container holds its name and collides on create just as surely."""
    proc = _run_container_check(tmp_path, labelled=[], named=["opentranscribe-postgres"])
    assert "opentranscribe-postgres" in (proc.stdout + proc.stderr), (
        f"neither the live nor the stopped sweep mentioned it: {proc.stdout} {proc.stderr}"
    )


def test_a_name_only_collider_gets_the_rehearsal_remedy_not_opentr_stop(tmp_path: Path):
    """Naming the wrong remedy is the #900 mistake wearing a different function's name.

    A container carrying a stock NAME but no stock compose-project label is a previous
    rehearsal's stack, not the operator's dev stack — and `./opentr.sh stop` does nothing
    to it. Now that the sweep can finally see this case, it is also the likeliest one, so
    sending the operator to the dev stack would trade a blind check for a misleading one.
    """
    proc = _run_container_check(tmp_path, labelled=[], named=["opentranscribe-backend"])
    combined = proc.stdout + proc.stderr
    assert "--cleanup" in combined, (
        "the refusal offers no way to clear a leftover rehearsal stack; the operator is "
        f"told to stop a dev stack that is not what is in the way.\n{combined}"
    )


def test_a_real_dev_stack_still_gets_the_opentr_stop_remedy(tmp_path: Path):
    """MUST-STAY-CLEAN: a labelled stock stack IS the dev stack, and that advice is right.

    Without this the fix above could degrade into "always print the rehearsal remedy",
    which is the same misdirection pointing the other way.
    """
    proc = _run_container_check(
        tmp_path, labelled=["opentranscribe-backend"], named=["opentranscribe-backend"]
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "./opentr.sh stop" in combined, (
        f"a running dev stack was not met with the remedy that actually clears it:\n{combined}"
    )
