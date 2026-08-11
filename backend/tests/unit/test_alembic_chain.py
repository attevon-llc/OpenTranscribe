"""Global integrity of the Alembic revision chain.

The per-revision ``test_vNNN_migration_consistency.py`` files each check one
revision's own links. Nothing checked the chain *as a whole* — so a second head
(the usual result of two branches both adding a migration) would only surface
when someone ran ``alembic upgrade head`` and got "Multiple head revisions are
present".

This also pins the shell helper the release harness depends on
(``scripts/release-tests/lib/alembic-head.py``) against alembic's own
``ScriptDirectory``. That helper re-implements head derivation with regex,
because it must run against an old release's worktree where alembic is not
installed. Two implementations of the same thing will drift unless something
compares them; this is that something.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent
VERSIONS_DIR = BACKEND_DIR / "alembic" / "versions"
HEAD_HELPER = REPO_ROOT / "scripts" / "release-tests" / "lib" / "alembic-head.py"


def _script_directory():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    # alembic.ini's script_location is cwd-relative; pin it for the test runner.
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return ScriptDirectory.from_config(config)


def test_chain_has_exactly_one_head():
    """Two heads means someone merged two branches that each added a migration."""
    heads = _script_directory().get_heads()
    assert len(heads) == 1, (
        f"expected exactly 1 Alembic head, found {len(heads)}: {sorted(heads)}. "
        "Repoint a down_revision, or add a merge migration."
    )


def test_chain_has_exactly_one_base():
    bases = _script_directory().get_bases()
    assert len(bases) == 1, f"expected exactly 1 base revision, found {sorted(bases)}"
    assert bases[0] == "v010_baseline"


def test_every_revision_is_reachable_from_head():
    """No orphans: walking down from head must visit every revision file."""
    scripts = _script_directory()
    head = scripts.get_heads()[0]

    walked = {rev.revision for rev in scripts.walk_revisions("base", head)}
    all_revisions = {rev.revision for rev in scripts.walk_revisions()}

    orphans = all_revisions - walked
    assert not orphans, (
        f"{len(orphans)} revision(s) not reachable from head {head}: {sorted(orphans)}"
    )


def test_filename_stem_matches_revision_id():
    """Repo convention (backend/alembic/CLAUDE.md): filename == revision id.

    Hand-authored ids make the chain readable; a mismatch means a file was
    renamed without updating the id, which makes it very hard to reason about.
    """
    scripts = _script_directory()
    by_path = {}
    for rev in scripts.walk_revisions():
        stem = Path(rev.path).stem
        if stem != rev.revision:
            by_path[stem] = rev.revision
    assert not by_path, f"filename/revision-id mismatches: {by_path}"


def test_no_duplicate_revision_ids():
    ids = [rev.revision for rev in _script_directory().walk_revisions()]
    duplicates = {i for i in ids if ids.count(i) > 1}
    assert not duplicates, f"duplicate revision ids: {sorted(duplicates)}"


@pytest.mark.skipif(not HEAD_HELPER.exists(), reason="release-test helper not present")
def test_shell_head_helper_agrees_with_alembic():
    """The regex helper and alembic must derive the same head.

    If these ever disagree, the release harness is validating a different chain
    than the application migrates, which is exactly the class of silent error
    the harness exists to prevent.
    """
    proc = subprocess.run(
        [sys.executable, str(HEAD_HELPER), str(BACKEND_DIR), "--json"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"alembic-head.py failed: {proc.stderr}"

    report = json.loads(proc.stdout)
    assert report["ok"], f"alembic-head.py reported problems: {report['problems']}"

    alembic_head = _script_directory().get_heads()[0]
    assert report["head"] == alembic_head, (
        f"alembic-head.py says {report['head']!r}, alembic ScriptDirectory says {alembic_head!r}"
    )

    expected_count = len(list(_script_directory().walk_revisions()))
    assert report["revision_count"] == expected_count, (
        f"helper counted {report['revision_count']} revisions, alembic sees {expected_count}"
    )
