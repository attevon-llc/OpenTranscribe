"""`scripts/release/patch-lib.sh` must correctly answer "is this a patch, and is
its rehearsal waivable" — the single decision `release.sh`, `65-rehearse.sh` and
`90-promote.sh` all key off (issue #784).

Three functions, each exercised against a REAL throwaway git repo (never a
re-implementation of git semantics):

- `patch_base_tag` — highest valid non-prerelease tag strictly below TO.
- `patch_release_kind` — patch|minor|major|unknown, derived from the delta.
- `patch_rehearsal_waivable` — 0+reason when waivable, 1+why otherwise. The
  refusal set is exactly: not a patch, no base tag, an empty diff (a
  derivation failure, not "nothing changed"), `git diff` itself failing, or the
  diff touching one of `PATCH_REHEARSAL_TRIGGERS`.

⚠️ Every entry in `PATCH_REHEARSAL_TRIGGERS` gets its own must-fire case here.
Without one per trigger, a `grep -F` that silently stopped matching one of them
would waive every rehearsal whose diff happened to touch only that path — the
exact hazard #784's own text warns about.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PATCH_LIB_SH = REPO_ROOT / "scripts" / "release" / "patch-lib.sh"

pytestmark = [
    pytest.mark.skipif(not PATCH_LIB_SH.exists(), reason="scripts/release/patch-lib.sh missing"),
    pytest.mark.skipif(shutil.which("git") is None, reason="needs git"),
]

GIT = shutil.which("git") or "git"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")


def _commit(repo: Path, path: str, content: str, message: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _tag(repo: Path, tag: str) -> None:
    _git(repo, "tag", tag)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    _init_repo(r)
    _commit(r, "README.md", "v1", "initial")
    return r


def _run(
    repo: Path, func_call: str, dockerhub_username: str = "unused-in-this-file"
) -> subprocess.CompletedProcess:
    """Source the REAL patch-lib.sh and invoke `func_call` against `repo`."""
    script = f"""
set -uo pipefail
REPO_ROOT={shlex.quote(str(repo))}
DOCKERHUB_USERNAME={shlex.quote(dockerhub_username)}
cd "$REPO_ROOT"
# shellcheck source=/dev/null
source {shlex.quote(str(PATCH_LIB_SH))}
{func_call}
"""
    return subprocess.run(
        ["bash", "-c", script], cwd=repo, capture_output=True, text=True, timeout=30
    )


# ───────────────────────────────────────────────────── patch_base_tag


@pytest.mark.unit
def test_patch_base_tag_finds_the_highest_tag_strictly_below_target(repo: Path) -> None:
    _tag(repo, "v0.4.0")
    _commit(repo, "a.txt", "1", "c1")
    _tag(repo, "v0.5.0")
    _commit(repo, "b.txt", "2", "c2")

    proc = _run(repo, "patch_base_tag v0.5.1")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "v0.5.0"


@pytest.mark.unit
def test_patch_base_tag_excludes_prereleases(repo: Path) -> None:
    """A prerelease tag between the real base and the target must not be picked."""
    _tag(repo, "v0.5.0")
    _commit(repo, "a.txt", "1", "c1")
    _tag(repo, "v0.5.1-rc1")
    _commit(repo, "b.txt", "2", "c2")

    proc = _run(repo, "patch_base_tag v0.5.1")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "v0.5.0", "must skip the -rc1 prerelease tag"


@pytest.mark.unit
def test_patch_base_tag_fails_with_no_tags_at_all(repo: Path) -> None:
    proc = _run(repo, "patch_base_tag v0.1.0")
    assert proc.returncode != 0
    assert proc.stdout.strip() == ""


# ───────────────────────────────────────────────────── patch_release_kind


@pytest.mark.unit
@pytest.mark.parametrize(
    ("base", "to", "expected"),
    [
        ("v0.5.0", "v0.5.1", "patch"),
        ("v0.5.0", "v0.6.0", "minor"),
        ("v0.5.0", "v1.0.0", "major"),
        ("v0.5.0", "v0.5.0", "unknown"),
    ],
)
def test_patch_release_kind_derives_from_the_numeric_delta(
    repo: Path, base: str, to: str, expected: str
) -> None:
    proc = _run(repo, f"patch_release_kind {to} {base}")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == expected, proc.stdout


@pytest.mark.unit
def test_patch_release_kind_is_unknown_with_no_base_tag(repo: Path) -> None:
    proc = _run(repo, "patch_release_kind v0.1.0")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "unknown"


# ───────────────────────────────────────────────────── patch_rehearsal_waivable


def _trigger_entries() -> list[tuple[str, str]]:
    """Every (trigger, reason-comment) pair declared in PATCH_REHEARSAL_TRIGGERS.

    Parses the real source rather than hand-copying the list, so this file
    cannot silently drift from what patch-lib.sh actually enforces.
    """
    source = PATCH_LIB_SH.read_text(encoding="utf-8")
    block_match = re.search(r"declare -a PATCH_REHEARSAL_TRIGGERS=\((.*?)\n\)", source, re.S)
    assert block_match, "PATCH_REHEARSAL_TRIGGERS array not found in patch-lib.sh"
    block = block_match.group(1)

    entries: list[tuple[str, str]] = []
    reason_lines: list[str] = []
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            reason_lines.append(line.lstrip("#").strip())
            continue
        m = re.match(r"^'([^']*)'", line)
        assert m, f"unparsed PATCH_REHEARSAL_TRIGGERS entry: {line!r}"
        assert reason_lines, f"trigger {m.group(1)!r} has no reason comment above it"
        entries.append((m.group(1), " ".join(reason_lines)))
        reason_lines = []
    return entries


# A representative path/filename that would appear in `git diff --name-only`
# for each trigger substring. Deliberately built from the trigger string
# itself (never a hand-maintained parallel list) so a new trigger is covered
# the moment it's added, or this fails loudly asking to be extended.
_TRIGGER_SAMPLE_PATHS: dict[str, str] = {
    "backend/alembic/versions/": "backend/alembic/versions/v999_test_migration.py",
    "Dockerfile": "backend/Dockerfile.prod",
    "requirements": "backend/requirements.txt",
    "package-lock.json": "frontend/package-lock.json",
    "docker-compose": "docker-compose.override.yml",
    "setup-opentranscribe.sh": "setup-opentranscribe.sh",
    "opentranscribe.sh": "opentranscribe.sh",
    "release-manifest.txt": "release-manifest.txt",
}


def test_every_trigger_has_a_sample_path_registered() -> None:
    """Guard the guard: a trigger with no sample path would be silently untested."""
    triggers = {t for t, _ in _trigger_entries()}
    missing = triggers - set(_TRIGGER_SAMPLE_PATHS)
    assert not missing, (
        f"PATCH_REHEARSAL_TRIGGERS gained {missing} with no entry in "
        f"_TRIGGER_SAMPLE_PATHS — add one so it gets a must-fire case"
    )


@pytest.mark.unit
def test_every_trigger_is_declared_with_a_written_reason() -> None:
    entries = _trigger_entries()
    assert len(entries) >= 8, f"expected at least 8 triggers, found {len(entries)}"
    for trigger, reason in entries:
        assert reason, f"trigger {trigger!r} has an empty reason comment"


@pytest.mark.unit
@pytest.mark.parametrize("trigger", sorted(_TRIGGER_SAMPLE_PATHS))
def test_each_trigger_disqualifies_the_waiver(repo: Path, trigger: str) -> None:
    """Must-fire: a diff touching ONLY this trigger's path must refuse to waive."""
    _tag(repo, "v0.5.0")
    _commit(repo, _TRIGGER_SAMPLE_PATHS[trigger], "changed", "touches trigger")

    proc = _run(repo, "patch_rehearsal_waivable v0.5.1")
    assert proc.returncode == 1, (
        f"trigger {trigger!r} should refuse to waive:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert trigger in proc.stderr, proc.stderr
    assert "full rehearsal required" in proc.stderr, proc.stderr


@pytest.mark.unit
def test_must_stay_clean_a_diff_touching_no_trigger_is_waivable(repo: Path) -> None:
    """Must-stay-clean control: an unrelated file change stays waivable."""
    _tag(repo, "v0.5.0")
    _commit(repo, "docs/some-typo-fix.md", "changed", "docs only")

    proc = _run(repo, "patch_rehearsal_waivable v0.5.1")
    assert proc.returncode == 0, f"an unrelated diff must be waivable:\n{proc.stderr}"
    assert proc.stdout.strip(), "a waivable result must explain itself on stdout"


@pytest.mark.unit
def test_refuses_when_the_delta_is_not_a_patch(repo: Path) -> None:
    _tag(repo, "v0.5.0")
    _commit(repo, "docs/notes.md", "changed", "docs only")

    proc = _run(repo, "patch_rehearsal_waivable v0.6.0")
    assert proc.returncode == 1
    assert "not a patch" in proc.stderr, proc.stderr


@pytest.mark.unit
def test_refuses_with_no_base_tag(repo: Path) -> None:
    # `repo` fixture has one commit and zero tags.
    proc = _run(repo, "patch_rehearsal_waivable v0.1.0")
    assert proc.returncode == 1
    assert "no base tag" in proc.stderr, proc.stderr


@pytest.mark.unit
def test_refuses_on_an_empty_diff(repo: Path) -> None:
    """HEAD IS v0.5.0 — base..HEAD is empty. Refusing is 'no evidence', not 'clean'."""
    _tag(repo, "v0.5.0")

    proc = _run(repo, "patch_rehearsal_waivable v0.5.1")
    assert proc.returncode == 1
    assert "no evidence" in proc.stderr, proc.stderr


@pytest.mark.unit
def test_refuses_when_git_diff_itself_fails(tmp_path: Path, repo: Path) -> None:
    """Simulate `git diff` failing in the environment (not a re-implementation of
    the function): a shim `git` on PATH forwards every subcommand to the real
    binary except `diff`, which it fails outright. `patch_base_tag`/
    `ver_release_tags` (both plain `git tag --list`) still resolve normally
    through the shim, so this isolates the diff-failure branch specifically.
    """
    _tag(repo, "v0.5.0")
    _commit(repo, "a.txt", "1", "after the tag")

    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(
        "#!/bin/bash\n"
        # patch_rehearsal_waivable calls `git -C "$REPO_ROOT" diff ...` -- "diff"
        # is not necessarily $1, so scan every argument rather than just the first.
        'for arg in "$@"; do\n'
        '  if [ "$arg" = "diff" ]; then\n'
        '    echo "fatal: simulated diff failure" >&2\n'
        "    exit 128\n"
        "  fi\n"
        "done\n"
        f'exec {shlex.quote(GIT)} "$@"\n'
    )
    shim.chmod(0o755)

    script = f"""
set -uo pipefail
REPO_ROOT={shlex.quote(str(repo))}
DOCKERHUB_USERNAME=unused
export PATH={shlex.quote(str(shim_dir))}:$PATH
cd "$REPO_ROOT"
source {shlex.quote(str(PATCH_LIB_SH))}
patch_rehearsal_waivable v0.5.1
"""
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 1, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "git diff" in proc.stderr and "failed" in proc.stderr, proc.stderr
