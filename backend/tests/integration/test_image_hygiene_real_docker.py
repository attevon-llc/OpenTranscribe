"""Real-docker half of the image-hygiene checks (unit half: tests/unit/test_image_hygiene.py).

1. ``backend/.dockerignore`` is cross-checked against BuildKit itself: a synthetic context
   holding every denylisted name at the root and nested is sent with the real ignore file and
   exported as a tar (``--output type=tar``, no image is created), and nothing denylisted may
   arrive. This is what the unit test's matcher only models.
2. ``scripts/check-image-hygiene.sh`` runs against two tiny ``FROM scratch`` images built
   here, one with a leaked nested file, under a ``host:port`` registry name. The images carry
   ``ot-test-artifact=1`` plus a per-test ``ot-test-build`` label and are removed by that label
   in ``finally``.
"""

from __future__ import annotations

import shutil
import subprocess
import tarfile
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check-image-hygiene.sh"
BACKEND_IGNORE = REPO_ROOT / "backend" / ".dockerignore"
_DOCKER_TIMEOUT = 180

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("docker") is None, reason="needs a docker CLI and daemon"),
]


def _denylist() -> list[str]:
    out = subprocess.run(
        ["bash", str(SCRIPT), "--list-patterns"], capture_output=True, text=True, check=True
    ).stdout
    return [ln for ln in out.splitlines() if ln]


def _write(root: Path, rel: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")


def test_buildkit_context_drops_every_denylisted_path(tmp_path: Path):
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    shutil.copy(BACKEND_IGNORE, ctx / ".dockerignore")
    leaks = []
    for pat in _denylist():
        name = pat.replace("*", "x")
        for rel in (name, f"app/{name}", f"app/services/deep/{name}"):
            # A directory-shaped name gets a file inside, so the directory itself is sent.
            _write(ctx, f"{rel}/inner")
            leaks.append(rel)
    kept = ["app/main.py", "scripts/celery_pool_healthcheck.py", "requirements.txt"]
    for rel in kept:
        _write(ctx, rel)
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\nCOPY . /ctx/\n")
    out = tmp_path / "out.tar"
    proc = subprocess.run(
        [
            "docker",
            "buildx",
            "build",
            "--output",
            f"type=tar,dest={out}",
            "-f",
            str(dockerfile),
            str(ctx),
        ],
        capture_output=True,
        text=True,
        timeout=_DOCKER_TIMEOUT,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    with tarfile.open(out) as tf:
        names = {n.removeprefix("./").rstrip("/") for n in tf.getnames()}
    for rel in kept:
        assert f"ctx/{rel}" in names, (
            f"control: {rel} should reach the build ({sorted(names)[:20]})"
        )
    survived = [rel for rel in leaks if f"ctx/{rel}" in names]
    assert not survived, f"backend/.dockerignore let BuildKit send: {survived}"


def _build(ctx: Path, tag: str, build_id: str) -> None:
    (ctx / "Dockerfile.hyg").write_text("FROM scratch\nCOPY app /app/\n")
    proc = subprocess.run(
        [
            "docker",
            "build",
            "-q",
            "--label",
            "ot-test-artifact=1",
            "--label",
            f"ot-test-build={build_id}",
            "-t",
            tag,
            "-f",
            str(ctx / "Dockerfile.hyg"),
            str(ctx),
        ],
        capture_output=True,
        text=True,
        timeout=_DOCKER_TIMEOUT,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]


def _remove_images(build_id: str) -> None:
    ids = subprocess.run(
        ["docker", "image", "ls", "-q", "--filter", f"label=ot-test-build={build_id}"],
        capture_output=True,
        text=True,
        timeout=_DOCKER_TIMEOUT,
        check=False,
    ).stdout.split()
    if ids:
        subprocess.run(
            ["docker", "rmi", "-f", *sorted(set(ids))],
            capture_output=True,
            timeout=_DOCKER_TIMEOUT,
            check=False,
        )


def _containers_of(build_id: str) -> list[str]:
    return subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=ot-test-build={build_id}"],
        capture_output=True,
        text=True,
        timeout=_DOCKER_TIMEOUT,
        check=False,
    ).stdout.split()


def test_script_against_real_images(tmp_path: Path):
    build_id = uuid.uuid4().hex[:12]
    base = f"localhost:5999/ot-hygiene-{build_id}/opentranscribe-backend"
    clean_ctx, dirty_ctx = tmp_path / "clean", tmp_path / "dirty"
    _write(clean_ctx, "app/main.py")
    _write(dirty_ctx, "app/main.py")
    _write(dirty_ctx, "app/app/.pytest_cache/v/cache/lastfailed")
    try:
        _build(clean_ctx, f"{base}:clean", build_id)
        _build(dirty_ctx, f"{base}:dirty", build_id)

        clean = subprocess.run(
            ["bash", str(SCRIPT), f"{base}:clean"],
            capture_output=True,
            text=True,
            timeout=_DOCKER_TIMEOUT,
        )
        assert clean.returncode == 0, clean.stderr

        dirty = subprocess.run(
            ["bash", str(SCRIPT), f"{base}:dirty"],
            capture_output=True,
            text=True,
            timeout=_DOCKER_TIMEOUT,
        )
        assert dirty.returncode == 1, dirty.stderr
        assert "/app/app/.pytest_cache" in dirty.stderr

        absent = subprocess.run(
            ["bash", str(SCRIPT), f"{base}:never-built"],
            capture_output=True,
            text=True,
            timeout=_DOCKER_TIMEOUT,
        )
        assert absent.returncode == 2, absent.stderr

        assert _containers_of(build_id) == [], "the script left a container behind"
    finally:
        _remove_images(build_id)
