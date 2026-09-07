"""`40-build.sh` must leave every bare `repo:vX.Y.Z` tag pointing at the HOST leg.

THE BUG

`docker buildx --load` cannot export a multi-arch manifest, so `40-build.sh` runs one build
per declared platform. `docker-build-push.sh` writes the bare `repo:$VERSION` tag on each of
those builds (`build_one_leg`'s local-mode `extra_tags`, and `build_tag_args` for
frontend/docs). Docker tags are not additive — the LAST writer wins — and
`list-platforms` emits `linux/amd64` before `linux/arm64`. So on this amd64 host the bare
tags for `frontend`, `docs` and `lite` all ended up naming the ARM64 leg.

Nothing downstream noticed, and three things depend on it:

* `test-fresh-install.sh:237-263` / `test-lite-mode.sh:243-252` reuse `repo:$VERSION` when it
  already exists locally, with **no architecture check** — so the rehearsal proving this
  release installs would have run a foreign image under QEMU or died with `exec format error`.
* `security-scan.sh`'s local resolution searches `repo:$VERSION-*` leg tags, but in local mode
  frontend/docs published no leg tag at all, so it dropped through to a Hub pull of a tag that
  is not published yet → COULD NOT SCAN.
* `40-build.sh`'s own baked-version check already knew the bare tag was ambiguous and worked
  around it by using the leg tag — for itself only.

APPROACH

Two halves, both against the REAL shipped shell:

1. `build_tag_args()` is extracted from `docker-build-push.sh` and run directly, proving
   frontend/docs now get an unambiguous `-multiarch-<arch>` alias in local mode and NOT in
   push mode (where a single multi-platform build produces one index and there is no per-arch
   artefact for such a name to describe).
2. The `# --- BEGIN bare-tag-host-arch ---` block is extracted from `40-build.sh` and run
   against a fake `docker` plus fake `list-platforms`/`list-repos` producers — so the control
   flow, the re-tag, and the read-the-architecture-back assertion are the real ones. A fake
   docker that reports the wrong architecture after a successful `docker tag` must fail the
   stage: `docker tag` exiting 0 says only that a name was written.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BUILD_SH = REPO_ROOT / "scripts" / "release" / "40-build.sh"
PUSH_SH = REPO_ROOT / "scripts" / "docker-build-push.sh"

pytestmark = pytest.mark.skipif(
    not BUILD_SH.exists() or not PUSH_SH.exists(),
    reason="scripts/release/40-build.sh or scripts/docker-build-push.sh not in this checkout",
)

VERSION = "v9.9.9"
HOST_PLATFORM = "linux/amd64"
HOST_ARCH = "amd64"

# component<TAB>capability<TAB>platforms — a stand-in for the real table, shaped like it:
# one host-only component, one two-platform component (the case the bug lived in), and
# blackwell (which must be skipped: it publishes :blackwell, never :vX.Y.Z).
FAKE_PLATFORMS = (
    "backend\tcuda\tlinux/amd64\n"
    "blackwell\tblackwell\tlinux/arm64\n"
    "frontend\tmultiarch\tlinux/amd64,linux/arm64\n"
    "lite\tcpu\tlinux/amd64,linux/arm64\n"
)
FAKE_REPOS = (
    "backend\ttest/ot-backend\n"
    "blackwell\ttest/ot-backend\n"
    "frontend\ttest/ot-frontend\n"
    "lite\ttest/ot-lite\n"
)


def _extract_function(script: Path, name: str) -> str:
    out = subprocess.run(
        ["sed", "-n", f"/^{name}()/,/^}}/p", str(script)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.strip(), f"{name}() not found in {script.name}"
    return out


def _extract_marked_block(script: Path, name: str) -> str:
    text = script.read_text(encoding="utf-8")
    start = text.index(f"# --- BEGIN {name} ---")
    end = text.index(f"# --- END {name} ---", start)
    return text[start : text.index("\n", end) + 1]


# ─────────────────────────────────────────── half 1: build_tag_args' local leg alias ──


def _run_build_tag_args(*, build_mode: str, platforms: str) -> list[str]:
    snippet = f"""
set -e
VERSION_FULL="{VERSION}"
BUILD_MODE="{build_mode}"
PUSH_LATEST=false
PLATFORMS="{platforms}"
declare -A COMPONENT_CAPABILITY=([frontend]=multiarch [docs]=multiarch)
{_extract_function(PUSH_SH, "build_platforms")}
{_extract_function(PUSH_SH, "build_leg_tag")}
{_extract_function(PUSH_SH, "build_tag_args")}
build_tag_args "test/ot-frontend" "frontend"
"""
    proc = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True)
    assert proc.returncode == 0, f"build_tag_args failed:\n{proc.stdout}\n{proc.stderr}"
    return [line for line in proc.stdout.splitlines() if line and line != "--tag"]


@pytest.mark.unit
def test_local_mode_gives_frontend_docs_an_unambiguous_leg_tag() -> None:
    tags = _run_build_tag_args(build_mode="local", platforms="linux/arm64")

    assert f"test/ot-frontend:{VERSION}" in tags, tags
    assert f"test/ot-frontend:{VERSION}-multiarch-arm64" in tags, (
        f"a per-arch alias is what security-scan.sh's leg search and 40-build.sh's re-tag "
        f"both need; without it the bare tag is the only handle and it is ambiguous: {tags}"
    )


@pytest.mark.unit
def test_push_mode_adds_no_per_arch_alias() -> None:
    """Must-stay-clean: in push mode one build produces one multi-platform index.

    An `-multiarch-amd64` name on an index spanning two architectures would be a lie, and
    80-publish.sh's check (a) deliberately skips leg tags for `multiarch` components.
    """
    tags = _run_build_tag_args(build_mode="push", platforms="linux/amd64,linux/arm64")

    assert tags == [f"test/ot-frontend:{VERSION}"], (
        f"push mode must publish the bare tag only; got {tags}"
    )


# ───────────────────────────────────────── half 2: 40-build.sh's re-tag + assertion ──


def _fake_docker(bin_dir: Path, *, arch_after_tag: str, tag_log: Path) -> None:
    """A `docker` that records `tag` calls and answers `image inspect --format`.

    `arch_after_tag` is what `image inspect` reports for a bare tag AFTER it has been
    tagged — the knob that expresses "docker tag succeeded but the image is the wrong
    architecture", which is the state the real bug produced and which a `docker tag` exit
    code cannot detect.
    """
    script = bin_dir / "docker"
    script.write_text(
        "#!/bin/bash\n"
        f'TAG_LOG="{tag_log}"\n'
        'if [ "$1" = "tag" ]; then\n'
        '  case "$2" in\n'
        # A leg tag only exists if its architecture was actually built. Every leg in the
        # fake table is "built" except a deliberately-absent one used by a test below.
        '    *"-absent-"*) exit 1 ;;\n'
        "  esac\n"
        '  echo "$2 -> $3" >> "$TAG_LOG"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then\n'
        f'  printf "%s" "{arch_after_tag}"\n'
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


def _run_retag_block(
    tmp_path: Path,
    *,
    arch_after_tag: str = HOST_ARCH,
    platforms: str = FAKE_PLATFORMS,
) -> tuple[str, list[str], list[list[str]]]:
    """Run the REAL bare-tag block. Returns (output, `docker tag` calls, `record` rows)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tag_log = tmp_path / "docker-tag.log"
    tag_log.touch()
    _fake_docker(bin_dir, arch_after_tag=arch_after_tag, tag_log=tag_log)

    fake_repo = tmp_path / "repo"
    (fake_repo / "scripts").mkdir(parents=True)
    for name, payload in (
        ("docker-build-push.sh", f'[ "$1" = "list-platforms" ] && printf %s "{platforms}"'),
        ("security-scan.sh", f'[ "$1" = "list-repos" ] && printf %s "{FAKE_REPOS}"'),
    ):
        stub = fake_repo / "scripts" / name
        stub.write_text(f"#!/bin/bash\n{payload}\nexit 0\n", encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    record_log = tmp_path / "records.log"
    snippet = f"""
set -uo pipefail
RED=''; GREEN=''; NC=''
VERSION="{VERSION}"
HOST_PLATFORM="{HOST_PLATFORM}"
record() {{
    printf '%s\\t%s\\t%s\\n' "$1" "${{2:-}}" "${{3:-}}" >> "{record_log}"
}}
build_fail_out() {{ echo "FAIL_OUT=$1"; exit "$1"; }}

cd "{fake_repo}" || exit 2
{_extract_marked_block(BUILD_SH, "bare-tag-host-arch")}
echo "RC=0"
"""
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    proc = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, env=env)
    out = proc.stdout + proc.stderr
    tags = [line for line in tag_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    records = [
        line.split("\t")
        for line in (
            record_log.read_text(encoding="utf-8").splitlines() if record_log.exists() else []
        )
        if line.strip()
    ]
    return out, tags, records


@pytest.mark.unit
def test_every_host_arch_component_bare_tag_is_repointed(tmp_path: Path) -> None:
    out, tags, records = _run_retag_block(tmp_path)

    assert "RC=0" in out, f"the block must not fail on a healthy build:\n{out}"
    assert sorted(tags) == sorted(
        [
            f"test/ot-backend:{VERSION}-cuda-amd64 -> test/ot-backend:{VERSION}",
            f"test/ot-frontend:{VERSION}-multiarch-amd64 -> test/ot-frontend:{VERSION}",
            f"test/ot-lite:{VERSION}-cpu-amd64 -> test/ot-lite:{VERSION}",
        ]
    ), f"expected every host-arch component's bare tag re-pointed at its amd64 leg: {tags}"
    assert not any("blackwell" in t for t in tags), (
        f"blackwell publishes :blackwell, never :vX.Y.Z — it must be skipped: {tags}"
    )
    outcomes = {r[0]: r[1] for r in records}
    assert outcomes.get("bare-tag-is-host-arch") == "pass", records


@pytest.mark.unit
def test_a_tag_that_still_reports_the_wrong_arch_fails_the_stage(tmp_path: Path) -> None:
    """`docker tag` exiting 0 says a NAME was written, not which image it names.

    This is the exact state the bug produced — a bare tag that exists and resolves to the
    wrong architecture — so the assertion has to read the artefact, not the exit code.
    """
    out, _, records = _run_retag_block(tmp_path, arch_after_tag="arm64")

    assert "FAIL_OUT=1" in out, f"a wrong-architecture bare tag must stop the stage:\n{out}"
    version_records = [r for r in records if r[0] == "bare-tag-is-host-arch"]
    assert version_records and version_records[0][1] == "fail", records
    assert "arm64" in version_records[0][2], (
        f"the detail must name what the tag actually reports:\n{records}"
    )


@pytest.mark.unit
def test_no_host_arch_leg_anywhere_is_not_measured_never_a_pass(tmp_path: Path) -> None:
    """Zero components checked is COULD NOT CHECK — the same empty-set rule this stage
    already applies to an empty platform table. A pass here would let every rehearsal on
    such a host run a foreign image with a green build stage behind it."""
    arm_only = "frontend\tmultiarch\tlinux/arm64\nlite\tcpu\tlinux/arm64\n"

    out, tags, records = _run_retag_block(tmp_path, platforms=arm_only)

    assert "FAIL_OUT" not in out, f"an absent host leg is not a failure of the build:\n{out}"
    assert tags == [], f"nothing should have been re-tagged: {tags}"
    version_records = [r for r in records if r[0] == "bare-tag-is-host-arch"]
    assert version_records and version_records[0][1] == "not-measured", records


@pytest.mark.unit
def test_a_missing_leg_tag_fails_rather_than_leaving_the_bare_tag_alone(tmp_path: Path) -> None:
    """If the host leg's tag is absent, the bare tag is whatever the last leg left there.

    Skipping quietly would preserve exactly the state this check exists to prevent.
    """
    missing = "frontend\tabsent\tlinux/amd64\n"

    out, tags, records = _run_retag_block(tmp_path, platforms=missing)

    assert "FAIL_OUT=1" in out, f"a missing host leg tag must stop the stage:\n{out}"
    assert tags == [], tags
    version_records = [r for r in records if r[0] == "bare-tag-is-host-arch"]
    assert version_records and version_records[0][1] == "fail", records
    assert "no test/ot-frontend" in version_records[0][2], records
