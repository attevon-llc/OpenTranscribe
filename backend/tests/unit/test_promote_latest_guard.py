"""`90-promote.sh` must never move `:latest` BACKWARDS (issue #784).

A hotfix cut from an old `release/<minor>` branch after a newer minor has
already published would, on the pre-#784 pipeline, silently downgrade
`:latest` for every existing user on their next pull — `docker buildx
imagetools create` is a manifest copy with no undo, so the guard has to run
before the copy loop, not react to a bad one afterward.

This drives the REAL `90-promote.sh` (copied into a scratch tree alongside the
real `criteria-lib.sh`, `published-repos.sh`, and `patch-lib.sh` — never a
re-implementation of any of them) against a real throwaway git repo for the
tag list, and a fake `docker` on `PATH` standing in for both Docker Hub
(`manifest inspect`) and the copy itself (`buildx imagetools create`/
`inspect`).

⚠️ The load-bearing assertion in the must-fire case is that `imagetools
create` was NEVER issued — inspected from the fake docker's own argument log,
not from a criterion the stage merely reports. A criterion recorded green
while the copy still ran would be a tick over the incident, not a fix for it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_DIR = REPO_ROOT / "scripts" / "release"
PROMOTE_SH = RELEASE_DIR / "90-promote.sh"
CRITERIA_LIB_SH = RELEASE_DIR / "criteria-lib.sh"
PUBLISHED_REPOS_SH = RELEASE_DIR / "published-repos.sh"
PATCH_LIB_SH = RELEASE_DIR / "patch-lib.sh"
VERSIONS_SH = REPO_ROOT / "scripts" / "release-tests" / "lib" / "versions.sh"

pytestmark = [
    pytest.mark.skipif(
        not all(
            p.exists()
            for p in (PROMOTE_SH, CRITERIA_LIB_SH, PUBLISHED_REPOS_SH, PATCH_LIB_SH, VERSIONS_SH)
        ),
        reason="a scripts/release/* dependency of 90-promote.sh is missing from this checkout",
    ),
    pytest.mark.skipif(shutil.which("git") is None, reason="needs git"),
]

GIT = shutil.which("git") or "git"
DOCKERHUB_USERNAME = "testhub"

_CRITERIA_YAML = """
version: 2
stages:
  promote:
    criteria:
      - id: latest-target-determined
        severity: blocking
      - id: latest-not-regressed
        severity: blocking
      - id: published-repo-list-derived
        severity: blocking
      - id: version-tag-published
        severity: blocking
      - id: latest-copied
        severity: blocking
      - id: latest-digest-matches-version
        severity: blocking
"""

_END = "<<<RECORD-END>>>"

_FAKE_DOCKER = f"""#!/bin/bash
{{
  for arg in "$@"; do printf '%s\\n' "$arg"; done
  printf '%s\\n' '{_END}'
}} >> "${{FAKE_DOCKER_LOG:?}}"

case "$1" in
  manifest)
    # manifest inspect <ref>
    ref="$3"
    if [[ ",${{FAKE_DOCKER_PUBLISHED_REFS:-}}," == *",$ref,"* ]]; then
      exit 0
    fi
    exit 1
    ;;
  buildx)
    case "$2" in
      imagetools)
        case "$3" in
          create) exit 0 ;;
          inspect) echo 'sha256:deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef'; exit 0 ;;
        esac
        ;;
    esac
    exit 0
    ;;
esac
exit 0
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run([GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _tag_repo(repo: Path, tags: list[str]) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    for i, tag in enumerate(tags):
        f = repo / f"f{i}.txt"
        f.write_text(str(i))
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"commit {i}")
        _git(repo, "tag", tag)


def _scaffold(tmp_path: Path, tags: list[str], repos_tsv: str) -> Path:
    repo = tmp_path / "repo"
    _tag_repo(repo, tags)

    release_dir = repo / "scripts" / "release"
    release_dir.mkdir(parents=True, exist_ok=True)
    for src, name in (
        (PROMOTE_SH, "90-promote.sh"),
        (CRITERIA_LIB_SH, "criteria-lib.sh"),
        (PUBLISHED_REPOS_SH, "published-repos.sh"),
        (PATCH_LIB_SH, "patch-lib.sh"),
    ):
        dst = release_dir / name
        shutil.copy(src, dst)
        dst.chmod(0o755)
    (release_dir / "release-criteria.yaml").write_text(_CRITERIA_YAML)

    lib_dir = repo / "scripts" / "release-tests" / "lib"
    lib_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(VERSIONS_SH, lib_dir / "versions.sh")

    scan_stub = repo / "scripts" / "security-scan.sh"
    scan_stub.write_text(
        f'#!/bin/bash\nif [ "$1" = "list-repos" ]; then\n  printf \'{repos_tsv}\'\nfi\n'
    )
    scan_stub.chmod(0o755)

    return repo


def _run_promote(
    repo: Path, version: str, tmp_path: Path, published_refs: str
) -> tuple[subprocess.CompletedProcess, list[list[str]]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(_FAKE_DOCKER)
    fake_docker.chmod(0o755)

    docker_log = tmp_path / "docker.log"
    docker_log.write_text("")

    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "RELEASE_VERSION": version,
        "JSON_OUT": "true",
        "DOCKERHUB_USERNAME": DOCKERHUB_USERNAME,
        "FAKE_DOCKER_LOG": str(docker_log),
        "FAKE_DOCKER_PUBLISHED_REFS": published_refs,
    }
    proc = subprocess.run(
        [str(repo / "scripts" / "release" / "90-promote.sh"), version],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    records: list[list[str]] = []
    current: list[str] = []
    for line in docker_log.read_text().splitlines():
        if line == _END:
            records.append(current)
            current = []
        else:
            current.append(line)
    return proc, records


def _issued_imagetools_create(records: list[list[str]]) -> bool:
    return any(
        len(r) >= 2 and r[0] == "buildx" and r[1] == "imagetools" and "create" in r for r in records
    )


def _criteria_json_line(stdout: str) -> str:
    lines = [line for line in stdout.strip().splitlines() if line.startswith("{")]
    assert lines, f"no JSON line in stdout:\n{stdout}"
    return lines[-1]


@pytest.mark.unit
def test_backport_is_a_pass_and_never_issues_imagetools_create(tmp_path: Path) -> None:
    """Must-fire: v0.5.1 promoted after v0.6.0 already published must be a PASS
    that leaves :latest alone -- and, above all, must never call
    `imagetools create`. Checked from the fake docker's own argument log, not
    from any criterion the stage reports about itself.
    """
    repo = _scaffold(
        tmp_path,
        tags=["v0.5.0", "v0.5.1", "v0.6.0"],
        repos_tsv="comp1\\ttesthub/comp1-repo\\n",
    )
    published = ",".join(
        [
            f"{DOCKERHUB_USERNAME}/opentranscribe-backend:v0.6.0",
            f"{DOCKERHUB_USERNAME}/opentranscribe-frontend:v0.6.0",
            "testhub/comp1-repo:v0.5.1",
        ]
    )

    proc, records = _run_promote(repo, "v0.5.1", tmp_path, published)

    assert proc.returncode == 0, (
        f"a backport must be a PASS, not a failure:\n{proc.stdout}\n{proc.stderr}"
    )
    assert not _issued_imagetools_create(records), (
        f"imagetools create was issued on a backport -- :latest may have moved backwards. "
        f"records={records}"
    )

    criteria = {c["id"]: c for c in json.loads(_criteria_json_line(proc.stdout))["criteria"]}
    assert criteria["latest-target-determined"]["status"] == "pass", criteria
    assert criteria["latest-not-regressed"]["status"] == "pass", criteria
    assert criteria["version-tag-published"]["status"] == "pass", criteria
    assert criteria["latest-copied"]["outcome"] == "not-measured", criteria["latest-copied"]
    assert criteria["latest-copied"]["severity"] == "warn", criteria["latest-copied"]
    assert criteria["latest-digest-matches-version"]["outcome"] == "not-measured", criteria


@pytest.mark.unit
def test_must_stay_clean_forward_promote_issues_imagetools_create(tmp_path: Path) -> None:
    """Control: promoting the actual newest release must still move :latest —
    the guard must not turn INTO a blanket refusal to ever promote anything.
    """
    repo = _scaffold(
        tmp_path,
        tags=["v0.5.0", "v0.5.1", "v0.6.0"],
        repos_tsv="comp1\\ttesthub/comp1-repo\\n",
    )
    published = ",".join(
        [
            f"{DOCKERHUB_USERNAME}/opentranscribe-backend:v0.6.0",
            f"{DOCKERHUB_USERNAME}/opentranscribe-frontend:v0.6.0",
            "testhub/comp1-repo:v0.6.0",
        ]
    )

    proc, records = _run_promote(repo, "v0.6.0", tmp_path, published)

    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert _issued_imagetools_create(records), (
        f"the newest release must still promote normally: records={records}"
    )

    criteria = {c["id"]: c for c in json.loads(_criteria_json_line(proc.stdout))["criteria"]}
    assert criteria["latest-not-regressed"]["status"] == "pass", criteria
    assert criteria["latest-copied"]["outcome"] == "pass", criteria["latest-copied"]
    assert criteria["latest-digest-matches-version"]["outcome"] == "pass", criteria


@pytest.mark.unit
def test_lookup_failure_fails_closed_and_never_issues_imagetools_create(tmp_path: Path) -> None:
    """Docker Hub unreachable / nothing resolvable: exit 3 (precondition), and
    'I don't know' must never be read as licence to move :latest.
    """
    repo = _scaffold(
        tmp_path,
        tags=["v0.5.0", "v0.5.1"],
        repos_tsv="comp1\\ttesthub/comp1-repo\\n",
    )
    # No refs at all are "published" -- newest_published_release() cannot resolve.
    proc, records = _run_promote(repo, "v0.5.1", tmp_path, published_refs="")

    assert proc.returncode == 3, f"{proc.stdout}\n{proc.stderr}"
    assert not _issued_imagetools_create(records), f"records={records}"

    criteria = {c["id"]: c for c in json.loads(_criteria_json_line(proc.stdout))["criteria"]}
    assert criteria["latest-target-determined"]["status"] == "fail", criteria
    assert criteria["latest-not-regressed"]["outcome"] == "not-measured", criteria
