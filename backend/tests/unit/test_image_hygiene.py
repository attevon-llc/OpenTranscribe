"""Published images must not carry the build machine's working tree.

v0.6.0's backend and lite images shipped ``/app/mutants`` (~90 MB of mutmut-mutated source),
``test.db``, ``celerybeat-schedule*`` and ``.import_linter_cache``: all gitignored, all present
in ``backend/`` on the build host, none excluded by ``backend/.dockerignore``, so
``COPY . .`` put them in the image.

Two halves are pinned here:
* the ``.dockerignore`` files exclude those paths (matched with Docker's own semantics, not a
  substring grep: ``*.db`` in a ``.dockerignore`` matches only at the context root);
* ``scripts/check-image-hygiene.sh`` fails closed on an image that contains one, and on an
  image it could not inspect.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check-image-hygiene.sh"

pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason="hygiene script not in this checkout")


def _rules(dockerignore: Path) -> list[tuple[bool, re.Pattern[str]]]:
    rules = []
    for raw in dockerignore.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        pat = line[1:] if negate else line
        pat = pat.rstrip("/")
        out = ""
        i = 0
        while i < len(pat):
            if pat.startswith("**/", i):
                out += "(?:.*/)?"
                i += 3
            elif pat.startswith("**", i):
                out += ".*"
                i += 2
            elif pat[i] == "*":
                out += "[^/]*"
                i += 1
            elif pat[i] == "?":
                out += "[^/]"
                i += 1
            else:
                out += re.escape(pat[i])
                i += 1
        rules.append((negate, re.compile(out)))
    return rules


def is_excluded(dockerignore: Path, path: str) -> bool:
    """Docker semantics: a rule matches a path or any parent dir; the last matching rule wins."""
    parts = path.split("/")
    candidates = ["/".join(parts[: n + 1]) for n in range(len(parts))]
    excluded = False
    for negate, rx in _rules(dockerignore):
        if any(rx.fullmatch(c) for c in candidates):
            excluded = not negate
    return excluded


def _denylist() -> list[str]:
    out = subprocess.run(
        ["bash", str(SCRIPT), "--list-patterns"], capture_output=True, text=True, check=True
    ).stdout
    return [ln for ln in out.splitlines() if ln]


BACKEND_IGNORE = REPO_ROOT / "backend" / ".dockerignore"


def test_matcher_has_docker_semantics():
    """Control: the matcher must be able to say no, and must model root-only globs."""
    assert is_excluded(BACKEND_IGNORE, "app/__pycache__") is False  # root-anchored in Docker
    assert is_excluded(BACKEND_IGNORE, "app/main.py") is False
    assert is_excluded(BACKEND_IGNORE, "scripts/celery_pool_healthcheck.py") is False  # re-included
    assert is_excluded(BACKEND_IGNORE, "scripts/other.py") is True


@pytest.mark.parametrize(
    "path",
    [
        "mutants",
        "mutants/app/main.py",
        "mutmut-stats.json",
        "test.db",
        "app/test.db",
        "celerybeat-schedule",
        "celerybeat-schedule-shm",
        "celerybeat-schedule-wal",
        "app/celerybeat-schedule",
        ".import_linter_cache",
        ".import_linter_cache/x",
    ],
)
def test_backend_dockerignore_excludes_the_v060_leaks(path: str):
    assert is_excluded(BACKEND_IGNORE, path), (
        f"backend/.dockerignore does not exclude {path!r}; `COPY . .` ships it in the "
        f"backend and lite images (v0.6.0 did exactly that)"
    )


def test_every_denylisted_artifact_is_excluded_from_the_backend_context():
    """Derived from the script's denylist, so a pattern added there cannot go unexcluded here."""
    missing = []
    for pat in _denylist():
        sample = pat.replace("*", "x")
        if not is_excluded(BACKEND_IGNORE, sample):
            missing.append(sample)
    assert not missing, (
        f"backend/.dockerignore lets these denylisted names into the image: {missing}"
    )


@pytest.mark.parametrize("ctx", ["frontend", "docs-site"])
@pytest.mark.parametrize(
    "path", ["node_modules", "x.db", "sub/x.db", "x.sqlite", ".env.local", ".git"]
)
def test_node_contexts_exclude_dev_artifacts(ctx: str, path: str):
    ignore = REPO_ROOT / ctx / ".dockerignore"
    assert is_excluded(ignore, path), f"{ctx}/.dockerignore does not exclude {path!r}"


def _fake_docker(tmp_path: Path, stdout: str, rc: int) -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "docker"
    exe.write_text(f"#!/bin/bash\nprintf '%s' {stdout!r}\nexit {rc}\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}


def _run(env: dict[str, str], image: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), image], env=env, capture_output=True, text=True, check=False
    )


BACKEND_IMG = "davidamacey/opentranscribe-backend:v9.9.9"


def test_script_fails_when_the_image_contains_an_artifact(tmp_path: Path):
    proc = _run(_fake_docker(tmp_path, "/app/test.db\n", 0), BACKEND_IMG)
    assert proc.returncode == 1
    assert "/app/test.db" in proc.stderr


def test_script_passes_a_clean_image(tmp_path: Path):
    proc = _run(_fake_docker(tmp_path, "", 0), BACKEND_IMG)
    assert proc.returncode == 0, proc.stderr


def test_script_cannot_check_is_not_a_pass(tmp_path: Path):
    proc = _run(_fake_docker(tmp_path, "", 125), BACKEND_IMG)
    assert proc.returncode == 2


def test_script_refuses_an_image_kind_it_does_not_know(tmp_path: Path):
    proc = _run(_fake_docker(tmp_path, "", 0), "someone/else:1")
    assert proc.returncode == 2


def test_script_asks_find_for_every_denylisted_name(tmp_path: Path):
    """The find expression is built from the denylist, one -name per entry."""
    log = tmp_path / "argv"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "docker"
    exe.write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" > {log}\n')
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    assert _run(env, BACKEND_IMG).returncode == 0
    argv = log.read_text().splitlines()
    assert "/app" in argv
    for pat in _denylist():
        assert pat in argv
