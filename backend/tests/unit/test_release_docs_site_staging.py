"""Staging `docs-site/` into a rehearsal tree must not copy its build artifacts.

WHAT WAS HAPPENING

`docker-compose.prod.yml` declares `build: context: ./docs-site` for the docs service, and the
staged rehearsal trees force `pull_policy: never`, so `test-upgrade.sh` copies `docs-site/`
into each staged tree or `docker compose up` cannot prepare the build context (issue #618's
shape: the whole `up` batch aborts, not just docs).

It did that with a bare `cp -r`. MEASURED on this checkout: `docs-site` is 1.1 GB across
40,164 files, of which `node_modules` alone is 918 MB / 39,171 files. Two stagings per hop
(after-tree in phase 07, rollback tree in phase 14) x `OT_UPGRADE_SOURCE_MINORS=2` hops =
~4.4 GB and ~160,000 file creations per rehearsal, for a directory whose only job is to exist
and validate.

Measured after: 45 MB / 287 files per staging; the copy itself 3.02 s -> 0.14 s (warm cache).

THE OTHER HALF, WHICH A `.dockerignore` ALONE DOES NOT FIX

`docs-site/Dockerfile` does `COPY package*.json ./` -> `npm ci` -> `COPY . .`. With
`node_modules` in the build context that last COPY overwrites the layer `npm ci` just built,
with a tree installed for the HOST's platform rather than the build stage's node:26-alpine.
So `docs-site/.dockerignore` is a correctness fix, not a size one — and it does nothing for
the `cp` above, because `cp` has never heard of it. Both are required, and this file asserts
they stay in step: a directory excluded from one and not the other is a silent regression of
whichever half was forgotten.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_PATCH = REPO_ROOT / "scripts" / "release-tests" / "lib" / "compose-patch.sh"
UPGRADE_SH = REPO_ROOT / "scripts" / "release-tests" / "test-upgrade.sh"
DOCS_DOCKERFILE = REPO_ROOT / "docs-site" / "Dockerfile"
DOCS_DOCKERIGNORE = REPO_ROOT / "docs-site" / ".dockerignore"

pytestmark = pytest.mark.skipif(
    not COMPOSE_PATCH.exists() or not UPGRADE_SH.exists(),
    reason="release-test harness not present in this checkout",
)

# What a real docs-site looks like, in miniature: the three regenerated artifact dirs plus
# the files the compose build context genuinely needs.
_TREE = {
    "node_modules/some-pkg/index.js": "host-built, wrong platform",
    "node_modules/.package-lock.json": "{}",
    "build/index.html": "<html></html>",
    ".docusaurus/registry.js": "//",
    "Dockerfile": "FROM node:26-alpine\n",
    "nginx.conf": "server {}\n",
    "package.json": "{}",
    "package-lock.json": "{}",
    "docusaurus.config.ts": "export default {}\n",
    "docs/intro.md": "# hi\n",
    "src/pages/index.tsx": "export default () => null\n",
    "static/img/logo.png": "PNG",
}


def _build_tree(root: Path) -> None:
    for rel, body in _TREE.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def _stage(src: Path, dst: Path, *, force_tar: bool = False) -> None:
    """Run the REAL cp_stage_docs_context.

    `force_tar` hides rsync behind a PATH with nothing in it but coreutils, so the fallback
    branch is exercised too — an untested fallback is the branch that runs on the one host
    that lacks rsync.
    """
    no_rsync = (
        'command() { if [ "$1" = "-v" ] && [ "$2" = "rsync" ]; then return 1; fi; '
        'builtin command "$@"; }\n'
        if force_tar
        else ""
    )
    snippet = f"""
set -euo pipefail
TEST_PROJECT_NAME=ot-selftest
TEST_LABEL=ot=selftest
source "{COMPOSE_PATCH}"
{no_rsync}
cp_stage_docs_context "{src}" "{dst}"
"""
    proc = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True)
    assert proc.returncode == 0, f"cp_stage_docs_context failed:\n{proc.stdout}\n{proc.stderr}"


def _relpaths(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


@pytest.mark.unit
@pytest.mark.parametrize("force_tar", [False, True], ids=["rsync", "tar-fallback"])
def test_artifacts_are_excluded_and_everything_else_is_copied(
    tmp_path: Path, force_tar: bool
) -> None:
    src = tmp_path / "docs-site"
    dst = tmp_path / "staged" / "docs-site"
    _build_tree(src)

    _stage(src, dst, force_tar=force_tar)

    copied = _relpaths(dst)
    expected = {
        rel for rel in _TREE if not rel.startswith(("node_modules/", "build/", ".docusaurus/"))
    }
    assert copied == expected, (
        f"the staged tree must contain the build context and nothing regenerable; "
        f"missing={expected - copied} unexpected={copied - expected}"
    )
    # nginx.conf and Dockerfile are the two files the image itself consumes — losing either
    # turns the `pull_policy: never` fallback build into the #618 whole-batch abort.
    assert (dst / "nginx.conf").is_file() and (dst / "Dockerfile").is_file(), copied


@pytest.mark.unit
def test_restaging_over_an_existing_tree_leaves_no_stale_files(tmp_path: Path) -> None:
    """The `cp -r` it replaces was preceded by `rm -rf`; the helper must keep that."""
    src = tmp_path / "docs-site"
    dst = tmp_path / "staged" / "docs-site"
    _build_tree(src)
    dst.mkdir(parents=True)
    (dst / "leftover-from-a-previous-hop.md").write_text("stale", encoding="utf-8")

    _stage(src, dst)

    assert not (dst / "leftover-from-a-previous-hop.md").exists(), sorted(_relpaths(dst))


@pytest.mark.unit
def test_both_upgrade_staging_sites_use_the_helper() -> None:
    """Two call sites, and only one being fixed is the failure mode worth guarding.

    Phase 07 stages the after-tree and phase 14 stages the rollback tree; they were byte-alike
    `cp -r` blocks, so a fix applied to the one an author happened to be reading leaves the
    other copying 1.1 GB with a comment beside it claiming the problem is solved.
    """
    text = UPGRADE_SH.read_text(encoding="utf-8")

    calls = [
        line for line in text.splitlines() if line.strip().startswith("cp_stage_docs_context ")
    ]
    assert len(calls) == 2, (
        f"expected exactly two docs-site staging sites in test-upgrade.sh, found {calls}"
    )
    assert not re.search(r"^\s*cp -r .*docs-site", text, re.M), (
        "a bare `cp -r` of docs-site is back — it copies 918 MB of node_modules"
    )


@pytest.mark.unit
def test_dockerignore_excludes_what_the_copy_excludes() -> None:
    """Anti-drift between the two exclusion lists.

    They govern different copies of the same tree (`docker build`'s context upload vs the
    rehearsal's `cp`), and neither mechanism can see the other's list.
    """
    assert DOCS_DOCKERIGNORE.exists(), (
        "docs-site/.dockerignore is missing — the Dockerfile's `COPY . .` will overwrite the "
        "node_modules that `npm ci` built, with a host-platform tree"
    )
    ignored = {
        line.strip().rstrip("/")
        for line in DOCS_DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    shell = COMPOSE_PATCH.read_text(encoding="utf-8")
    match = re.search(r"CP_DOCS_STAGE_EXCLUDES=\(([^)]*)\)", shell)
    assert match, "CP_DOCS_STAGE_EXCLUDES not found in compose-patch.sh"
    staged_excludes = set(match.group(1).split())

    assert staged_excludes <= ignored, (
        f"every directory the rehearsal copy skips must also be out of the docker build "
        f"context; missing from .dockerignore: {sorted(staged_excludes - ignored)}"
    )
    assert "node_modules" in staged_excludes, staged_excludes


@pytest.mark.unit
def test_the_dockerfile_still_relies_on_the_exclusion() -> None:
    """The exclusion is load-bearing only while `COPY . .` follows `npm ci`.

    If someone reorders the Dockerfile this assertion should be revisited deliberately, not
    silently — and if they DON'T reorder it, deleting .dockerignore is a live regression.
    """
    text = DOCS_DOCKERFILE.read_text(encoding="utf-8")
    npm_ci = text.index("npm ci")
    copy_all = text.index("COPY . .")

    assert npm_ci < copy_all, (
        "the dependency layer must be installed before the source copy, or there is no "
        "cached layer for a stray node_modules to clobber and this guard means nothing"
    )
