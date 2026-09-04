"""Every test/rehearsal script must bring containers up through a FRONT END, not by hand.

This repo has two front ends and they are not interchangeable:

* ``./opentr.sh`` — the DEVELOPMENT script. Correct for the dev-loop tooling
  (``run-dev-tests.sh``, ``run-auth-e2e.sh``, …), which drives the dev stack in a git clone.
* ``./opentranscribe.sh`` — the SHIPPED management script a curl install gets. Correct for the
  release rehearsals (``scripts/release-tests/*``), which exist to prove what a real
  self-hoster can do. ``opentr.sh`` is deliberately absent from ``release-manifest.txt``.

Both own compose-file selection for their side. A script that hand-builds
``docker compose -f docker-compose.<something>.yml ... up`` is a second copy of that logic,
and this repo has now shipped that bug twice:

* the release rehearsals hardcoded ``-f docker-compose.yml -f docker-compose.prod.yml
  [-f docker-compose.gpu.yml]``, so ``get_compose_files()`` — GPU vs Blackwell vs CPU-only,
  nginx, backup — was dead code at rehearsal time
  (``scripts/release-tests/REHEARSAL_ALIGNMENT_PLAN.md``);
* ``run-auth-e2e.sh`` started the LDAP and Keycloak IdPs with a bare
  ``docker compose -f docker-compose.<idp>.yml up -d``. Compose derives the project from the
  CURRENT DIRECTORY's name, so that only worked while the checkout happened to be named after
  the live stack's project. Measured with a throwaway project: run that way, the container
  joined ``base_default`` instead of ``<project>_default`` and could not resolve a sibling
  container by name at all — i.e. from a git worktree the backend could reach neither IdP, and
  every auth E2E failure would have looked like an auth bug.

Both overlay files say so themselves: "``./opentr.sh`` is the only supported entry point".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = REPO_ROOT / "scripts"

# Scripts that bring containers up as part of running tests, and the front end each MUST use.
# The choice is per-script and follows who runs the command in real life — see the module
# docstring. Adding a script here is the point: it is how a new one inherits the rule.
DELEGATING_SCRIPTS = {
    "run-auth-e2e.sh": "opentr.sh",
    "run-dev-tests.sh": "opentr.sh",
    "lib/dev-test-overlays.sh": "opentr.sh",
    "release-tests/test-fresh-install.sh": "opentranscribe.sh",
    "release-tests/test-upgrade.sh": "opentranscribe.sh",
}

# Scripts that legitimately still name a compose file inline, each with a written reason. A
# stale entry fails (see the test below), so an exemption cannot outlive its subject.
#
# Deliberately short. Several scripts DO assemble a chain — validate-deployments.sh (whose whole
# job is enumerating compose permutations and running `config -q` on each; it starts nothing),
# run-diarization-gpu-tests.sh (a dedicated test image in its OWN `-p` project, isolated from the
# dev stack by design), and the offline package's own opentr-offline.sh / uninstall-offline-package.sh
# (which ARE the front end for a deployment that ships neither opentr.sh nor opentranscribe.sh).
# None of them appear here because none names a compose file inline — they build the chain from a
# variable — so the detector does not fire on them and an entry would be immediately stale.
HAND_BUILT_EXEMPTIONS = {
    "release-tests/test-lite-mode.sh": (
        "docker-compose.lite.yml is absent from release-manifest.txt and get_compose_files() "
        "has no lite branch, so NO shipped command can select it — lite is a repo/dev-only "
        "deployment shape today. Pinned by "
        "test_compose_file_selection.py::test_lite_mode_is_not_reachable_by_a_shipped_deployment, "
        "which fails the moment that changes. Since issue #660 this script also layers "
        "docker-compose.diar-native.yml to pair the CPU-EP speaker-embedding sidecar "
        "with the lite workers — still only reachable from here and opentr.sh, still "
        "not shippable."
    ),
}

_CODE_ONLY = re.compile(r"^\s*#")

# Deriving the compose project from a directory name. `basename $REPO_ROOT` is the shape that
# breaks from a git worktree (.claude/worktrees/<name>), where it yields the worktree's own
# directory name and never matches the live stack's project label.
_BASENAME_PROJECT = re.compile(
    r"COMPOSE_PROJECT_NAME=.*basename|basename.*REPO_ROOT|basename.*PROJECT_ROOT"
)


def _basename_project_derivations(lines: list[str]) -> list[str]:
    return [line.strip() for line in lines if _BASENAME_PROJECT.search(line)]


def _script(rel: str) -> Path:
    return SCRIPTS / rel


def _code_lines(path: Path) -> list[str]:
    """Source lines with comments dropped — prose about a command is not a command."""
    return [
        line for line in path.read_text(encoding="utf-8").splitlines() if not _CODE_ONLY.match(line)
    ]


def _names_a_compose_file(path: Path) -> list[str]:
    """Lines where the script names a compose file itself rather than asking a front end."""
    return [line.strip() for line in _code_lines(path) if "-f docker-compose" in line]


def _all_scripts() -> list[Path]:
    return sorted(p for p in SCRIPTS.rglob("*.sh") if p.is_file())


@pytest.mark.parametrize("rel", sorted(DELEGATING_SCRIPTS), ids=lambda r: r)
def test_bringup_scripts_do_not_name_compose_files(rel: str):
    """The regression guard. Fails the moment a script grows its own `-f` chain again."""
    path = _script(rel)
    assert path.is_file(), f"{rel} not found — update DELEGATING_SCRIPTS"
    offenders = _names_a_compose_file(path)
    assert not offenders, (
        f"{rel} names compose files itself: {offenders}. Use "
        f"./{DELEGATING_SCRIPTS[rel]} — it owns compose-file selection for this script's side. "
        "A hand-built chain is a second implementation that drifts, and compose derives the "
        "project from the current directory's name, so it also lands on the wrong network from "
        "a git worktree."
    )


@pytest.mark.parametrize("rel", sorted(DELEGATING_SCRIPTS), ids=lambda r: r)
def test_bringup_scripts_invoke_their_front_end(rel: str):
    """Not naming compose files is necessary but not sufficient — it must actually delegate.

    Without this half, a script could pass the test above by simply never starting anything.
    """
    path = _script(rel)
    front_end = DELEGATING_SCRIPTS[rel]
    code = "\n".join(_code_lines(path))
    assert front_end in code, (
        f"{rel} never invokes ./{front_end}. It is expected to bring containers up through "
        "that front end; if it genuinely no longer starts anything, remove it from "
        "DELEGATING_SCRIPTS in the same change."
    )


def test_the_detector_actually_fires():
    """Must-fire control. A detector that matches nothing reports a clean tree.

    test-lite-mode.sh is exempt precisely because it still hand-builds a chain, so it doubles as
    the live positive case. An earlier draft of this detector was a `docker compose ... up`
    regex and matched NOTHING even in scripts that were full of hand-built chains, because the
    chain is assembled into a bash array several lines above the command that consumes it.
    """
    offenders = _names_a_compose_file(_script("release-tests/test-lite-mode.sh"))
    lite_lines = [line for line in offenders if "docker-compose.lite.yml" in line]
    assert len(offenders) >= 3, (
        f"the compose-file detector found {len(offenders)} chain lines in test-lite-mode.sh, "
        "which is known to hand-build one per phase — it is dead or crippled, and the "
        "parametrized tests beside it prove nothing"
    )
    assert len(lite_lines) >= 1, (
        "the detector found chain lines but none naming docker-compose.lite.yml — it is "
        f"matching something other than the overlay it is meant to catch: {offenders}"
    )


def test_no_exemption_outlives_its_subject():
    """A stale exemption is worse than none: it reads as a considered decision.

    Same discipline as backend/tests/audit-allowlist.txt — the list can only shrink.
    """
    stale = []
    for rel in HAND_BUILT_EXEMPTIONS:
        path = _script(rel)
        if not path.is_file():
            stale.append(f"{rel} (file gone)")
        elif not _names_a_compose_file(path):
            stale.append(f"{rel} (no longer names any compose file)")
    assert not stale, (
        f"HAND_BUILT_EXEMPTIONS entries no longer describe anything real: {stale}. "
        "Delete them in the same change that made them obsolete."
    )


def test_every_hand_built_chain_is_accounted_for():
    """Nothing under scripts/ may name a compose file without being listed somewhere.

    This is what makes the two lists above a complete account rather than a sample: a NEW script
    that hand-builds a chain fails here even if nobody thought to add it to DELEGATING_SCRIPTS.
    """
    known = set(DELEGATING_SCRIPTS) | set(HAND_BUILT_EXEMPTIONS)
    unaccounted = {}
    for path in _all_scripts():
        rel = path.relative_to(SCRIPTS).as_posix()
        if rel in known:
            continue
        offenders = _names_a_compose_file(path)
        if offenders:
            unaccounted[rel] = offenders
    assert not unaccounted, (
        f"these scripts name compose files but appear in neither DELEGATING_SCRIPTS nor "
        f"HAND_BUILT_EXEMPTIONS: {unaccounted}. Either delegate to ./opentr.sh (dev tooling) / "
        "./opentranscribe.sh (anything rehearsing the shipped user path), or add an exemption "
        "with a written reason."
    )


def test_the_compose_project_is_resolved_by_label_not_by_directory_name():
    """`basename $REPO_ROOT` is wrong from a git worktree, and this repo uses worktrees.

    scripts/lib/compose-project.sh is the ONE implementation; it may keep the directory-name
    guess as a last-resort fallback (reached only when no stack is running, where every lookup
    would find nothing anyway). Any OTHER script deriving a compose project that way is the
    bug this centralisation removed.
    """
    lib = SCRIPTS / "lib" / "compose-project.sh"
    assert lib.is_file(), "scripts/lib/compose-project.sh is missing"
    assert "com.docker.compose.project" in lib.read_text(encoding="utf-8"), (
        "compose-project.sh no longer resolves the project from the compose label"
    )

    offenders = {}
    for path in _all_scripts():
        if path == lib:
            continue
        hits = _basename_project_derivations(_code_lines(path))
        if hits:
            offenders[path.relative_to(SCRIPTS).as_posix()] = hits
    assert not offenders, (
        f"these scripts derive a compose project from a directory name: {offenders}. "
        "Source scripts/lib/compose-project.sh and call compose_project_name() instead — "
        "from a git worktree the directory name never matches the live stack's project."
    )


def test_the_basename_project_detector_actually_fires():
    """Must-fire control for the test above.

    That test's first assertion is "compose-project.sh exists", which short-circuits before the
    scan on any tree lacking the file — so without this case, a scan predicate that matched
    nothing would look identical to a clean tree. These are the exact lines
    scripts/lib/dev-test-overlays.sh carried before the extraction.
    """
    must_fire = [
        '    echo "${detected:-$(basename "$REPO_ROOT")}"',
        'COMPOSE_PROJECT_NAME="$(basename "$PWD")"',
        'proj=$(basename "$PROJECT_ROOT")',
    ]
    assert _basename_project_derivations(must_fire) == [line.strip() for line in must_fire]

    must_stay_clean = [
        'detected="$(docker ps --filter label=com.docker.compose.project ...)"',
        'name="$(basename "$media_path")"',
        'log_step "Starting IdP overlays via ./opentr.sh"',
    ]
    assert _basename_project_derivations(must_stay_clean) == []
