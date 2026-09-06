"""`opentr.sh restart-*` must act on the deployment it was ASKED for, or refuse.

The restart-* dispatch arms took no arguments and called bare ``docker compose restart``,
which resolves the DEFAULT compose project. Three consequences, all observed:

1. ``restart-backend --fresh <name>`` silently restarted the **main dev stack** instead of the
   isolated one. `--fresh` was not merely ignored — it was aimed at the wrong, live stack.
2. It printed ``✅ Backend services restarted successfully`` regardless, because stderr went to
   ``/dev/null`` and the exit status was discarded. Reported symptom: that success line next to
   an empty container table, having restarted nothing the caller wanted.
3. A typo'd deployment name had the same effect as a correct one — there was nothing to typo
   *into*, since the name was never read.

These run the REAL functions out of the REAL script against a fake ``docker`` on ``PATH``, and
assert the arguments actually issued. Asserting on the printed message instead would pass
against the bug: the old code printed success while doing the wrong thing, and that is the
whole finding.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENTR = REPO_ROOT / "opentr.sh"

# Real helpers the restart path depends on. Extracted rather than stubbed, so the project name
# and compose chain under test are the ones the script really builds.
NEEDED_FUNCS = (
    "fresh_sanitize_name",
    "fresh_project_name",
    "fresh_read_aux",
    "fresh_compose_chain",
    "restart_resolve_target",
    "restart_compose",
    "restart_backend",
    "restart_frontend",
    "restart_all",
)


def _extract(name: str, source: str) -> str:
    """Slice `name() { ... }` out of opentr.sh (top-level `}` at column 0)."""
    match = re.search(rf"^{re.escape(name)}\(\) \{{$", source, re.M)
    assert match, f"{name}() not found in opentr.sh — was it renamed?"
    end = re.search(r"^\}$", source[match.start() :], re.M)
    assert end, f"no closing brace for {name}()"
    return source[match.start() : match.start() + end.end()]


@pytest.fixture(scope="module")
def harness_prelude() -> str:
    source = OPENTR.read_text(encoding="utf-8")
    return "\n\n".join(_extract(name, source) for name in NEEDED_FUNCS)


def _run(
    harness_prelude: str,
    tmp_path: Path,
    body: str,
    *,
    docker_rc: int = 0,
    make_overlay: str | None = "test1",
    override: str = "",
) -> subprocess.CompletedProcess[str]:
    """Run `body` with the real restart functions loaded and a fake `docker` on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    calls = tmp_path / "docker-calls.log"
    (bindir / "docker").write_text(
        "#!/bin/bash\n"
        f'printf "PROJECT=%s ARGS=%s\\n" "${{COMPOSE_PROJECT_NAME:-<unset>}}" "$*" >> "{calls}"\n'
        f"exit {docker_rc}\n"
    )
    (bindir / "docker").chmod(0o755)

    fresh_dir = tmp_path / "fresh"
    fresh_dir.mkdir(exist_ok=True)
    if make_overlay:
        (fresh_dir / f"{make_overlay}.yml").write_text("services: {}\n")

    script = (
        "set -uo pipefail\n"
        f'export PATH="{bindir}:$PATH"\n'
        f'FRESH_OVERLAY_DIR="{fresh_dir}"\n'
        f'cd "{REPO_ROOT}"\n'  # fresh_compose_chain tests for compose files relative to cwd
        f"{harness_prelude}\n"
        f"{override}\n"
        f"{body}\n"
    )
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    proc.stdout += "\n--- docker calls ---\n"
    proc.stdout += calls.read_text() if calls.exists() else "(docker was never invoked)"
    return proc


def test_fresh_restart_targets_the_fresh_project_not_the_live_stack(harness_prelude, tmp_path):
    proc = _run(harness_prelude, tmp_path, "restart_backend --fresh test1")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    restarts = [
        line
        for line in proc.stdout.splitlines()
        if line.startswith("PROJECT=") and " restart " in line
    ]
    assert restarts, f"no `docker compose restart` was issued at all:\n{proc.stdout}"
    for line in restarts:
        assert "PROJECT=otfresh-test1" in line, (
            "a restart was issued against the WRONG compose project — this is the bug: bare "
            "`docker compose` resolves the default project, i.e. the LIVE dev stack.\n" + line
        )
        assert "test1.yml" in line, (
            "the fresh deployment's generated overlay is not in the compose chain, so this "
            "addresses differently-configured services than the ones that are running.\n" + line
        )


def test_the_project_assertion_would_catch_the_old_behaviour(harness_prelude, tmp_path):
    """MUST-FIRE control for the test above.

    Against pre-fix `opentr.sh` the behavioural tests cannot even load — `restart_resolve_target`
    does not exist, so they ERROR in fixture setup rather than failing on their assertions. That
    is red, but it proves only that the function is absent; it does NOT prove the assertions can
    detect a restart aimed at the wrong project. So reinstate exactly the old dispatch — bare
    `docker compose`, no project, no chain — and confirm the observable this test keys on really
    does flip.
    """
    old_behaviour = 'restart_compose() { docker compose "$@"; }\n'
    proc = _run(harness_prelude, tmp_path, "restart_backend --fresh test1", override=old_behaviour)

    restarts = [
        line
        for line in proc.stdout.splitlines()
        if line.startswith("PROJECT=") and " restart " in line
    ]
    assert restarts, f"the control issued no restart, so it controls nothing:\n{proc.stdout}"
    assert all("PROJECT=<unset>" in line for line in restarts), (
        "reinstating the old bare `docker compose` did NOT produce an unscoped restart, so the "
        f"sibling test's `PROJECT=otfresh-test1` assertion is not what catches this bug:\n{proc.stdout}"
    )
    assert not any("otfresh-test1" in line for line in restarts)


def test_default_restart_is_unchanged_by_the_fix(harness_prelude, tmp_path):
    """No `--fresh` must still mean bare `docker compose` — no project, no `-f` chain."""
    proc = _run(harness_prelude, tmp_path, "restart_backend")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    restarts = [
        line
        for line in proc.stdout.splitlines()
        if line.startswith("PROJECT=") and " restart " in line
    ]
    assert restarts, f"no restart issued:\n{proc.stdout}"
    for line in restarts:
        assert "PROJECT=<unset>" in line, f"default path gained a project override:\n{line}"
        assert " -f " not in line, f"default path gained an explicit compose chain:\n{line}"


@pytest.mark.parametrize(
    ("body", "why"),
    [
        (
            "restart_backend --fresh nosuchdeployment",
            "an unknown name must not fall through to the live stack",
        ),
        ("restart_backend --fresh", "a missing name must not fall through to the live stack"),
        ("restart_backend --bogus", "an unknown flag must not be silently dropped"),
    ],
)
def test_unresolvable_target_refuses_and_touches_no_docker(harness_prelude, tmp_path, body, why):
    proc = _run(harness_prelude, tmp_path, body)
    assert proc.returncode == 2, (
        f"{why} — expected exit 2, got {proc.returncode}\n{proc.stdout}{proc.stderr}"
    )
    assert "docker was never invoked" in proc.stdout, (
        f"{why}, but docker ran anyway — against the default project, that is the live stack:\n{proc.stdout}"
    )


def test_a_failed_restart_is_not_reported_as_success(harness_prelude, tmp_path):
    """The old code sent stderr to /dev/null and dropped the status, so it always 'succeeded'."""
    proc = _run(harness_prelude, tmp_path, "restart_backend --fresh test1", docker_rc=1)
    assert proc.returncode != 0, (
        "docker compose failed but restart_backend returned 0 — a caller (or a human) reads "
        f"that as a completed restart:\n{proc.stdout}{proc.stderr}"
    )
    assert "restarted successfully" not in proc.stdout, (
        f"printed a success line for a restart that failed:\n{proc.stdout}"
    )


def test_frontend_and_all_share_the_same_scoping(harness_prelude, tmp_path):
    for body in ("restart_frontend --fresh test1", "restart_all --fresh test1"):
        proc = _run(harness_prelude, tmp_path, body)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        restarts = [
            line
            for line in proc.stdout.splitlines()
            if line.startswith("PROJECT=") and " restart" in line
        ]
        assert restarts, f"{body} issued no restart:\n{proc.stdout}"
        assert all("PROJECT=otfresh-test1" in line for line in restarts), (
            f"{body} restarted the default project:\n{proc.stdout}"
        )


def test_dispatch_arms_forward_their_arguments() -> None:
    """The functions can only honour `--fresh` if the dispatch actually passes it.

    This is the half the harness above cannot see: `restart_backend` was correct in isolation
    and still ignored `--fresh`, because the case arm called it with no arguments.
    """
    source = OPENTR.read_text(encoding="utf-8")
    for command, func in (
        ("restart-backend", "restart_backend"),
        ("restart-frontend", "restart_frontend"),
        ("restart-all", "restart_all"),
    ):
        arm = re.search(rf"^  {re.escape(command)}\)\n(.*?)^    ;;$", source, re.M | re.S)
        assert arm, f"dispatch arm for {command} not found"
        assert f'{func} "$@"' in arm.group(1), (
            f"the `{command}` dispatch arm does not forward its arguments to {func}, so every "
            f"flag the user typed is dropped before it can be parsed:\n{arm.group(1)}"
        )
        assert "shift" in arm.group(1), (
            f"the `{command}` arm forwards $@ without `shift`, so {func} receives the command "
            "name itself as its first option and rejects it"
        )


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_opentr_shellcheck_clean() -> None:
    proc = subprocess.run(
        ["shellcheck", "-S", "warning", str(OPENTR)], capture_output=True, text=True, timeout=180
    )
    assert proc.returncode == 0, f"shellcheck findings in opentr.sh:\n{proc.stdout}"
