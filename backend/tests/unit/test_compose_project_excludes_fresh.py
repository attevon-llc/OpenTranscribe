"""`compose_project_name()` must never answer with a `--fresh` deployment's project.

**This exists because of a measured failure, not a hypothetical.** The function detects the live
stack's compose project by asking docker for a running `postgres` container and reading its
project label. It took the FIRST match. On 2026-09-07 six `--fresh` stacks were up for a parallel
mutation run, so seven postgres containers matched, and docker's listing order decided the
answer: it returned ``otfresh-mut-session``. ``scripts/diar-native-smoke.sh`` then looked for the
sidecar in a mutation stack, did not find one, and failed the gate's diar-native phase — a live
stack that was working perfectly reported as broken.

The blast radius is every caller, not that one check: ``overlay_container_name`` resolves through
it, and so does ``scripts/lib/dev-test-overlays.sh``'s overlay orchestration. Any developer with
a ``--fresh`` stack up gets a non-deterministic answer to "which project am I" — the same class
as the unscoped ``docker ps`` in ``opentr.sh stop`` that destroyed an unrelated container
(issue #693).

These tests drive the REAL function from the REAL script with a fake ``docker`` first on
``PATH``, so nothing touches a daemon and they are safe to run with a live stack up. A test that
re-implemented the filter would pass against a version that never applies it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
LIB = REPO_ROOT / "scripts" / "lib" / "compose-project.sh"

pytestmark = pytest.mark.skipif(not LIB.exists(), reason="scripts/lib/compose-project.sh absent")


def _run(tmp_path: Path, ps_output: str, *, env: dict[str, str] | None = None) -> str:
    """Call the real ``compose_project_name`` with ``docker ps`` returning *ps_output*."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    shim = bin_dir / "docker"
    # Echoes the canned project labels for `ps`, nothing for anything else.
    shim.write_text(
        '#!/bin/bash\nif [ "${1:-}" = "ps" ]; then printf "%s" "$FAKE_PS"; fi\nexit 0\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)

    run_env = dict(os.environ)
    run_env.pop("COMPOSE_PROJECT_NAME", None)
    run_env["PATH"] = f"{bin_dir}{os.pathsep}{run_env.get('PATH', '')}"
    run_env["FAKE_PS"] = ps_output
    run_env["REPO_ROOT"] = str(REPO_ROOT)
    if env:
        run_env.update(env)

    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["bash", "-c", f'set -euo pipefail; source "{LIB}"; compose_project_name'],
        capture_output=True,
        text=True,
        env=run_env,
        timeout=30,
    )
    assert result.returncode == 0, f"compose_project_name failed: {result.stderr}"
    return result.stdout.strip()


def test_a_fresh_project_is_never_returned_when_the_live_stack_is_up(tmp_path: Path):
    """THE REGRESSION. Fresh stacks listed first must not win over the live project."""
    ps = "otfresh-mut-session\notfresh-mut-lockout\ntranscribe-app\n"

    assert _run(tmp_path, ps) == "transcribe-app", (
        "a --fresh deployment is an isolated stack beside the live one and must never be "
        "mistaken for it — this is the exact ordering that failed the gate's diar-native phase"
    )


def test_the_live_project_is_found_regardless_of_docker_ordering(tmp_path: Path):
    """Docker's listing order must not decide the answer."""
    assert _run(tmp_path, "opentranscribe\notfresh-a\n") == "opentranscribe"
    assert _run(tmp_path, "otfresh-a\nopentranscribe\n") == "opentranscribe"


def test_an_unrelated_project_on_the_same_host_is_never_returned(tmp_path: Path):
    """THE SECOND REGRESSION, and why this is an allowlist rather than an exclusion list.

    Excluding ``otfresh-*`` alone was not enough. This host runs other applications under their
    own compose projects, and once the fresh stacks were torn down the very next answer was
    ``dsva`` — a different product entirely that happens to have a postgres. Any exclusion list
    is a guess about what else exists on the machine; only naming what THIS repo runs under is
    sound.
    """
    assert _run(tmp_path, "dsva\nsomeone-elses-app\ntranscribe-app\n") == "transcribe-app"

    lone = _run(tmp_path, "dsva\nsomeone-elses-app\n")
    assert lone == REPO_ROOT.name, (
        f"with no live stack the answer must be the directory guess, not a neighbour: {lone!r}"
    )


def test_only_fresh_stacks_running_falls_back_rather_than_naming_one(tmp_path: Path):
    """With no live stack, the answer must be the directory guess — never a fresh project.

    Returning a fresh project here would be worse than returning nothing: callers would find
    real containers belonging to somebody else's isolated deployment and act on them.
    """
    result = _run(tmp_path, "otfresh-mut-session\notfresh-mut-lockout\n")

    assert not result.startswith("otfresh-"), f"leaked a fresh project: {result!r}"
    assert result == REPO_ROOT.name


def test_an_explicit_compose_project_name_still_wins(tmp_path: Path):
    """The env override is the operator speaking; detection must not second-guess it."""
    got = _run(tmp_path, "transcribe-app\n", env={"COMPOSE_PROJECT_NAME": "otfresh-deliberate"})

    assert got == "otfresh-deliberate", (
        "an explicitly exported COMPOSE_PROJECT_NAME is a direct instruction — including when "
        "it names a fresh stack, which is how one would deliberately target one"
    )


def test_no_running_postgres_at_all_falls_back_to_the_directory_name(tmp_path: Path):
    """The documented no-stack-up path, and it must not abort under `set -euo pipefail`.

    Guards the reason this filter is a bash loop rather than `... | grep -v | head -1`:
    `grep -v` exits 1 when it filters everything out, and with `pipefail` that turns an
    ordinary "no live stack" into a hard abort of whatever sourced this library.
    """
    assert _run(tmp_path, "") == REPO_ROOT.name
