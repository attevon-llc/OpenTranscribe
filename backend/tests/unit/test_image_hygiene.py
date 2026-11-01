"""Published images must not carry the build machine's working tree.

v0.6.0's backend and lite images shipped ``/app/mutants`` (~90 MB of mutmut-mutated source),
``test.db``, ``celerybeat-schedule*`` and ``.import_linter_cache``: all gitignored, all present
in ``backend/`` on the build host, none excluded by ``backend/.dockerignore``, so
``COPY . .`` put them in the image.

Two halves are pinned here:
* the ``.dockerignore`` files exclude those paths (matched with a model of Docker's semantics;
  ``tests/integration/test_image_hygiene_real_docker.py`` cross-checks the model against a real
  BuildKit context transfer);
* ``scripts/check-image-hygiene.sh`` fails closed on an image that contains one, and on an
  image it could not inspect. It is driven here with a fake ``docker`` on PATH.
"""

from __future__ import annotations

import io
import os
import re
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check-image-hygiene.sh"
BACKEND_IGNORE = REPO_ROOT / "backend" / ".dockerignore"

pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason="hygiene script not in this checkout")


def _compile(pat: str) -> re.Pattern[str]:
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
    return re.compile(out)


def _rules(lines: list[str]) -> list[tuple[int, bool, re.Pattern[str]]]:
    rules = []
    for lineno, raw in enumerate(lines):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        pat = (line[1:] if negate else line).rstrip("/")
        rules.append((lineno, negate, _compile(pat)))
    return rules


def _excluded(rules, path: str) -> bool:
    """Docker semantics: a rule matches a path or any parent dir; the last matching rule wins."""
    parts = path.split("/")
    candidates = ["/".join(parts[: n + 1]) for n in range(len(parts))]
    excluded = False
    for _lineno, negate, rx in rules:
        if any(rx.fullmatch(c) for c in candidates):
            excluded = not negate
    return excluded


def is_excluded(dockerignore: Path, path: str) -> bool:
    return _excluded(_rules(dockerignore.read_text(encoding="utf-8").splitlines()), path)


def _denylist() -> list[str]:
    out = subprocess.run(
        ["bash", str(SCRIPT), "--list-patterns"], capture_output=True, text=True, check=True
    ).stdout
    return [ln for ln in out.splitlines() if ln]


def _samples() -> list[str]:
    """Each denylisted name at the context root AND nested, as `app/.coverage` once leaked."""
    out = []
    for pat in _denylist():
        name = pat.replace("*", "x")
        out += [name, f"app/{name}", f"app/services/deep/{name}"]
    return out


def test_matcher_has_docker_semantics():
    """Control: the matcher must be able to say no, and must model root-only globs."""
    assert is_excluded(BACKEND_IGNORE, "app/__pycache__") is False  # root-anchored in Docker
    assert is_excluded(BACKEND_IGNORE, "app/main.py") is False
    assert is_excluded(BACKEND_IGNORE, "scripts/celery_pool_healthcheck.py") is False  # re-included
    assert is_excluded(BACKEND_IGNORE, "scripts/other.py") is True


@pytest.mark.parametrize(
    "path",
    [
        "mutants/app/main.py",
        "mutmut-stats.json",
        "test.db",
        "app/test.db",
        "celerybeat-schedule",
        "celerybeat-schedule-shm",
        "celerybeat-schedule-wal",
        "app/celerybeat-schedule",
        ".import_linter_cache/x",
        "app/.pytest_cache/v/cache",
        "app/.coverage",
    ],
)
def test_backend_dockerignore_excludes_the_v060_leaks(path: str):
    assert is_excluded(BACKEND_IGNORE, path), (
        f"backend/.dockerignore does not exclude {path!r}; `COPY . .` ships it in the "
        f"backend and lite images (v0.6.0 did exactly that)"
    )


def test_every_denylisted_artifact_is_excluded_at_every_depth():
    """Derived from the script's denylist, which the script matches at ANY depth."""
    missing = [s for s in _samples() if not is_excluded(BACKEND_IGNORE, s)]
    assert not missing, (
        f"backend/.dockerignore lets these denylisted paths into the image: {missing}"
    )


def test_no_denylist_rule_is_redundant():
    """A rule shadowed by another one is a rule whose deletion no test notices.

    The previous revision carried `mutants/` AND `**/mutants/`; deleting either left every
    assertion green. Each rule that excludes a denylisted sample must be the ONLY rule doing so
    for at least one sample.
    """
    lines = BACKEND_IGNORE.read_text(encoding="utf-8").splitlines()
    rules = _rules(lines)
    samples = _samples()
    baseline = {s: _excluded(rules, s) for s in samples}
    redundant = []
    for lineno, negate, rx in rules:
        if negate:
            continue
        hits = [s for s in samples if any(rx.fullmatch(c) for c in _prefixes(s))]
        if not hits:
            continue
        without = [r for r in rules if r[0] != lineno]
        if all(_excluded(without, s) == baseline[s] for s in hits):
            redundant.append(lines[lineno].strip())
    assert not redundant, f"backend/.dockerignore rules shadowed by another rule: {redundant}"


def test_redundancy_check_can_fire():
    """Control for the test above: a duplicated rule must be reported."""
    rules = _rules(["**/mutants", "mutants/"])
    without_second = [r for r in rules if r[0] != 1]
    assert _excluded(without_second, "mutants") == _excluded(rules, "mutants")


def _prefixes(path: str) -> list[str]:
    parts = path.split("/")
    return ["/".join(parts[: n + 1]) for n in range(len(parts))]


@pytest.mark.parametrize("ctx", ["frontend", "docs-site"])
@pytest.mark.parametrize(
    "path", ["node_modules", "x.db", "sub/x.db", "x.sqlite", ".env.local", ".git"]
)
def test_node_contexts_exclude_dev_artifacts(ctx: str, path: str):
    ignore = REPO_ROOT / ctx / ".dockerignore"
    assert is_excluded(ignore, path), f"{ctx}/.dockerignore does not exclude {path!r}"


# ── the script, with a fake docker ────────────────────────────────────────────────────────

BACKEND_IMG = "davidamacey/opentranscribe-backend:v9.9.9-cuda-amd64"
FRONTEND_IMG = "davidamacey/opentranscribe-frontend:v9.9.9-multiarch-arm64"


def _tar(path: Path, members: list[str]) -> Path:
    with tarfile.open(path, "w") as tf:
        for name in members:
            info = tarfile.TarInfo(name)
            if name.endswith("/"):
                info.type = tarfile.DIRTYPE
                tf.addfile(info)
            else:
                data = b"x"
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
    return path


def _fake_docker(
    tmp_path: Path,
    export_tar: Path | None,
    *,
    create_rc: int = 0,
    export_rc: int = 0,
) -> dict[str, str]:
    """`create` prints a cid (and stderr noise), `export` streams a tar, every call is logged."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "docker.log"
    exe = bindir / "docker"
    tar_arg = str(export_tar) if export_tar else "/nonexistent"
    exe.write_text(
        "#!/bin/bash\n"
        f'echo "$*" >> {log}\n'
        'case "$1" in\n'
        "  create)\n"
        "    echo 'WARNING: The requested image platform does not match the host' >&2\n"
        "    echo '/app/test.db looks like an artifact but is stderr' >&2\n"
        f"    [[ {create_rc} -ne 0 ]] && {{ echo 'No such image' >&2; exit {create_rc}; }}\n"
        "    echo cafe1234; exit 0 ;;\n"
        "  export)\n"
        "    echo '/app/mutants on stderr' >&2\n"
        f"    [[ {export_rc} -ne 0 ]] && exit {export_rc}\n"
        f"    cat {tar_arg}; exit 0 ;;\n"
        "  rm) exit 0 ;;\n"
        '  *) echo "unexpected docker $1" >&2; exit 97 ;;\n'
        "esac\n"
    )
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}


def _run(env: dict[str, str], *images: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *images], env=env, capture_output=True, text=True, check=False
    )


def _calls(tmp_path: Path) -> list[str]:
    return (tmp_path / "docker.log").read_text().splitlines()


CLEAN_BACKEND = ["app/", "app/main.py", "app/app/services/x.py", "etc/test.db", "root/.cache/"]
CLEAN_FRONTEND = ["usr/share/nginx/html/", "usr/share/nginx/html/index.html", "etc/nginx/x.log"]


def test_clean_image_passes_despite_stderr_noise(tmp_path: Path):
    """Warnings on stderr naming artifact paths must not become findings."""
    env = _fake_docker(tmp_path, _tar(tmp_path / "t.tar", CLEAN_BACKEND))
    proc = _run(env, BACKEND_IMG)
    assert proc.returncode == 0, proc.stderr
    assert "ok   " + BACKEND_IMG in proc.stdout


def test_artifacts_outside_the_payload_root_are_ignored(tmp_path: Path):
    env = _fake_docker(tmp_path, _tar(tmp_path / "t.tar", CLEAN_FRONTEND))
    assert _run(env, FRONTEND_IMG).returncode == 0


@pytest.mark.parametrize(
    "dirty",
    ["app/mutants/", "app/test.db", "app/app/.pytest_cache/v", "app/x/.coverage", "app/.env"],
)
def test_dirty_image_fails_naming_the_path(tmp_path: Path, dirty: str):
    env = _fake_docker(tmp_path, _tar(tmp_path / "t.tar", [*CLEAN_BACKEND, dirty]))
    proc = _run(env, BACKEND_IMG)
    assert proc.returncode == 1, proc.stderr
    assert "/" + dirty.rstrip("/") in proc.stderr


@pytest.mark.parametrize("pat", _denylist() if SCRIPT.exists() else [])
def test_every_denylist_entry_fires_nested(tmp_path: Path, pat: str):
    name = pat.replace("*", "x")
    env = _fake_docker(
        tmp_path, _tar(tmp_path / "t.tar", [*CLEAN_FRONTEND, f"usr/share/nginx/html/a/{name}"])
    )
    assert _run(env, FRONTEND_IMG).returncode == 1


def test_missing_image_is_could_not_check(tmp_path: Path):
    env = _fake_docker(tmp_path, None, create_rc=1)
    proc = _run(env, BACKEND_IMG)
    assert proc.returncode == 2
    assert not any(c.startswith("rm") for c in _calls(tmp_path)), (
        "removed a container it never created"
    )
    assert "--pull never" in _calls(tmp_path)[0]


def test_export_failure_is_could_not_check_and_still_removes_its_container(tmp_path: Path):
    env = _fake_docker(tmp_path, None, export_rc=1)
    proc = _run(env, BACKEND_IMG)
    assert proc.returncode == 2
    assert "rm cafe1234" in _calls(tmp_path)


def test_image_without_payload_root_is_could_not_check(tmp_path: Path):
    env = _fake_docker(tmp_path, _tar(tmp_path / "t.tar", ["etc/", "etc/passwd"]))
    assert _run(env, BACKEND_IMG).returncode == 2


def test_container_removed_and_images_never_touched(tmp_path: Path):
    env = _fake_docker(tmp_path, _tar(tmp_path / "t.tar", [*CLEAN_BACKEND, "app/test.db"]))
    _run(env, BACKEND_IMG)
    verbs = [c.split()[0] for c in _calls(tmp_path)]
    assert verbs == ["create", "export", "rm"], verbs


def test_could_not_check_outranks_a_finding(tmp_path: Path):
    env = _fake_docker(tmp_path, _tar(tmp_path / "t.tar", [*CLEAN_BACKEND, "app/test.db"]))
    assert _run(env, BACKEND_IMG, "someone/else:1").returncode == 2


@pytest.mark.parametrize(
    "ref",
    [
        "localhost:5000/davidamacey/opentranscribe-backend:v1",
        "registry.example:443/opentranscribe-backend-lite",
        "davidamacey/opentranscribe-backend@sha256:" + "a" * 64,
        "localhost:5000/opentranscribe-backend:v1@sha256:" + "b" * 64,
    ],
)
def test_registry_ports_and_digests_resolve_the_repo(tmp_path: Path, ref: str):
    env = _fake_docker(tmp_path, _tar(tmp_path / "t.tar", CLEAN_BACKEND))
    proc = _run(env, ref)
    assert proc.returncode == 0, proc.stderr


def test_unknown_repository_is_could_not_check(tmp_path: Path):
    env = _fake_docker(tmp_path, None)
    assert _run(env, "localhost:5000/someone/else:1").returncode == 2
