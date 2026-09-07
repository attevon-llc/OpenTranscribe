"""Real-tree execution coverage for the two chown scripts (issue #602).

``backend/tests/unit/test_chown_script_targets.py`` is entirely static — it reads the
scripts as text and never runs them. This file is the other half: it actually executes
``scripts/fix-model-permissions.sh`` against a throwaway directory tree and
``scripts/fix-shared-volume-perms.sh`` against a deliberately empty compose project, plus
one test that checks the assumption every one of these scripts is built on — that the
built image's ``appuser`` really is ``1000:999`` — against the real image rather than
trusting the constant.

⚠️ **Never point ``fix-shared-volume-perms.sh`` at anything but a guaranteed-empty
``COMPOSE_PROJECT_NAME``.** It runs ``docker run ... chown -R`` against
``${COMPOSE_PROJECT_NAME}_pipeline_scratch`` and friends with no confirmation prompt. The
one invocation in this file uses a fresh ``uuid4``-suffixed project name for exactly this
reason — do not add a second invocation, or a positive round-trip test, without the same
care. Deliberately no such test exists here: creating and chowning a real Docker named
volume isn't worth the blast radius next to volumes one typo away from the live
``transcribe-app_*`` / ``opentranscribe_*`` ones. The static volume-list derivation
(``test_chown_script_targets.py``'s ``test_shared_volume_list_matches_the_paths_the_image_reserves``)
plus the image-GID test below cover what can actually be wrong.

No ``RUN_*`` env-var gate (issue #431: a stale gate has hidden a real test here before).
``scripts/run-integration-tests.sh`` already globs ``tests/integration/``.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import uuid
from pathlib import Path

import pytest

from tests.unit.test_chown_script_targets import EXPECTED_OWNER

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available"),
]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MODEL_SH = _REPO_ROOT / "scripts" / "fix-model-permissions.sh"
_VOLUME_SH = _REPO_ROOT / "scripts" / "fix-shared-volume-perms.sh"

_EXPECTED_UID, _EXPECTED_GID = (int(part) for part in EXPECTED_OWNER.split(":"))


def _run(
    cmd: list[str], *, cwd: str | Path | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        cmd, capture_output=True, text=True, cwd=str(cwd) if cwd else None, env=env
    )


def _resolve_local_backend_image() -> str | None:
    """Prefer ``opentranscribe-backend:latest``; else the first locally-present image tag
    referenced by ``docker-compose*.yml``. Never builds or pulls anything."""
    candidates = ["opentranscribe-backend:latest"]
    for compose_file in sorted(_REPO_ROOT.glob("docker-compose*.yml")):
        for match in re.finditer(
            r"image:\s*(?:\$\{[^}]*:-)?(\S*opentranscribe-backend\S*?):?(?:\$\{[^}]*\})?[\"'}]?\s*$",
            compose_file.read_text(encoding="utf-8"),
            re.MULTILINE,
        ):
            candidates.append(match.group(1))
    seen: set[str] = set()
    for tag in candidates:
        if tag in seen:
            continue
        seen.add(tag)
        if _run(["docker", "image", "inspect", tag]).returncode == 0:
            return tag
    return None


def test_the_built_image_still_agrees_with_the_scripts_default_owner() -> None:
    """The whole premise of both scripts' default -- and of
    ``test_chown_script_targets.py``'s pins -- is that the built image's ``appuser`` is
    ``1000:999``. Verify that against the REAL image, not just the constant."""
    image = _resolve_local_backend_image()
    if image is None:
        pytest.skip("no local opentranscribe-backend image found (never built here)")
    assert image is not None  # narrows for mypy; pytest.skip() above never returns

    result = _run(["docker", "run", "--rm", "--entrypoint", "id", image, "appuser"])
    assert result.returncode == 0, (
        f"`docker run --entrypoint id {image} appuser` failed: {result.stderr}"
    )

    match = re.search(r"uid=(\d+)\(appuser\).*gid=(\d+)\(appuser\)", result.stdout)
    assert match, f"could not parse `id appuser` output: {result.stdout!r}"
    actual = f"{match.group(1)}:{match.group(2)}"

    assert actual == EXPECTED_OWNER, (
        f"the built image's appuser is now {actual}, not {EXPECTED_OWNER} -- the base image "
        "moved appuser's GID; update CONTAINER_UID_GID in scripts/common.sh, "
        "opentranscribe.sh, fix-model-permissions.sh and SHARED_VOLUME_OWNER in "
        "fix-shared-volume-perms.sh."
    )


def _make_fake_model_script_root(root: Path, *, model_cache_dir_value: str) -> None:
    """Lay out a throwaway ``<root>/scripts/fix-model-permissions.sh`` + ``<root>/.env``
    project, so the script's own ``PROJECT_ROOT`` (derived from its own path) is ``root``
    regardless of the caller's CWD."""
    scripts_dir = root / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    script_copy = scripts_dir / "fix-model-permissions.sh"
    script_copy.write_text(_MODEL_SH.read_text(encoding="utf-8"), encoding="utf-8")
    script_copy.chmod(script_copy.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    (root / ".env").write_text(f"MODEL_CACHE_DIR={model_cache_dir_value}\n", encoding="utf-8")


def _seed_tree(base: Path) -> None:
    (base / "a.bin").write_bytes(b"a")
    (base / "sub" / "deep").mkdir(parents=True)
    (base / "sub" / "b.bin").write_bytes(b"b")
    (base / "sub" / "deep" / "c.bin").write_bytes(b"c")


def test_model_permission_script_chowns_a_throwaway_tree_and_nothing_else(tmp_path: Path) -> None:
    """The docker-based chown path fixes ownership under MODEL_CACHE_DIR and touches
    nothing outside it -- run with a CWD that is neither the fake project root nor
    contains the target, exercising Bug B's fix (CWD must not matter)."""
    project_root = tmp_path / "proj"
    cache_dir = tmp_path / "proj" / "cache"
    cache_dir.mkdir(parents=True)
    _seed_tree(cache_dir)

    decoy = tmp_path / "decoy"
    decoy.mkdir()
    decoy_file = decoy / "keep.bin"
    decoy_file.write_bytes(b"decoy")
    decoy_before = decoy_file.stat()

    _make_fake_model_script_root(project_root, model_cache_dir_value=str(cache_dir))

    # Control: the tree starts explicitly WRONG (1000:1000 -- the pre-#580 shape), so a
    # no-op script can't pass this test by accident.
    control = _run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{cache_dir}:/m",
            "busybox:latest",
            "chown",
            "-R",
            "1000:1000",
            "/m",
        ]
    )
    assert control.returncode == 0, f"failed to seed wrong ownership: {control.stderr}"

    somewhere_else = tmp_path / "not_the_project_root"
    somewhere_else.mkdir()

    result = _run(
        ["bash", str(project_root / "scripts" / "fix-model-permissions.sh")], cwd=somewhere_else
    )
    assert result.returncode == 0, (
        f"fix-model-permissions.sh exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    for path in (cache_dir, *cache_dir.rglob("*")):
        st = path.stat()
        assert (st.st_uid, st.st_gid) == (_EXPECTED_UID, _EXPECTED_GID), (
            f"{path} is {st.st_uid}:{st.st_gid}"
        )
        mode = stat.S_IMODE(st.st_mode)
        if path.is_dir():
            assert mode == 0o755, f"{path} has mode {oct(mode)}, expected 0o755"
        else:
            assert mode == 0o644, f"{path} has mode {oct(mode)}, expected 0o644"

    decoy_after = decoy_file.stat()
    assert (decoy_after.st_uid, decoy_after.st_gid) == (decoy_before.st_uid, decoy_before.st_gid), (
        "the decoy sibling directory's ownership changed -- the script touched something "
        "outside MODEL_CACHE_DIR"
    )


def test_model_permission_script_does_not_chown_a_cwd_relative_directory(tmp_path: Path) -> None:
    """Bug B regression: with the shipped RELATIVE default (`MODEL_CACHE_DIR=./models`),
    the script must anchor to the project root, not whatever the caller's CWD happens to
    contain -- even when the CWD has its own, unrelated `models/` directory."""
    project_root = tmp_path / "proj"
    _make_fake_model_script_root(project_root, model_cache_dir_value="./models")

    real_target = project_root / "models"
    real_target.mkdir(parents=True)
    _seed_tree(real_target)
    real_before = (real_target / "a.bin").stat()
    assert real_before.st_gid != _EXPECTED_GID, "test setup: file already had the target GID"

    cwd = tmp_path / "elsewhere"
    decoy_target = cwd / "models"
    decoy_target.mkdir(parents=True)
    _seed_tree(decoy_target)
    decoy_before = (decoy_target / "a.bin").stat()

    result = _run(["bash", str(project_root / "scripts" / "fix-model-permissions.sh")], cwd=cwd)
    assert result.returncode == 0, (
        f"fix-model-permissions.sh exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    real_after = (real_target / "a.bin").stat()
    assert (real_after.st_uid, real_after.st_gid) == (_EXPECTED_UID, _EXPECTED_GID), (
        f"the fake project root's models/ was NOT chowned: {real_after.st_uid}:{real_after.st_gid}"
    )

    decoy_after = (decoy_target / "a.bin").stat()
    assert (decoy_after.st_uid, decoy_after.st_gid) == (decoy_before.st_uid, decoy_before.st_gid), (
        "the CWD-relative decoy models/ directory was touched -- MODEL_CACHE_DIR was resolved "
        "against the CWD instead of the project root (Bug B regression)"
    )


def test_shared_volume_script_exits_nonzero_when_the_project_matches_nothing() -> None:
    """Bug A regression: a COMPOSE_PROJECT_NAME that owns no volumes must fail loudly, not
    report "repaired 0 volume(s)" and exit 0.

    ⚠️ The project name below is a fresh uuid4 -- guaranteed to own no Docker volumes on
    this or any host. NEVER run fix-shared-volume-perms.sh against a real project name in
    a test: it chowns real Docker named volumes with no confirmation prompt.
    """
    fake_project = f"otchowntest-{uuid.uuid4().hex[:12]}"
    env = dict(os.environ)
    env["COMPOSE_PROJECT_NAME"] = fake_project

    result = _run(["bash", str(_VOLUME_SH)], env=env)

    assert result.returncode != 0, (
        f"fix-shared-volume-perms.sh exited 0 against a guaranteed-empty project "
        f"'{fake_project}' -- stdout:\n{result.stdout}"
    )
    assert fake_project in result.stdout, (
        f"expected the resolved project name '{fake_project}' to appear in the script's "
        f"own output so a caller can tell what went wrong -- got:\n{result.stdout}"
    )
