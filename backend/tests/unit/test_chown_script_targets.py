"""Static shape checks for the two chown scripts (issue #602).

These read ``scripts/fix-model-permissions.sh`` and ``scripts/fix-shared-volume-perms.sh``
as text — they never execute a script or touch Docker — so they run in milliseconds and
belong in the fast unit suite. The real-tree execution tests (throwaway directories, a real
build image, and a deliberately-empty compose project) live in
``backend/tests/integration/test_chown_scripts_real_tree.py``.

Tests 4-7 pin the fixes for four real bugs found alongside the coverage gap issue #602 was
filed for:

  - Bug A: ``fix-shared-volume-perms.sh`` reports "repaired 0 volume(s)" and exits 0 even
    when it resolved the wrong compose project and touched nothing.
  - Bug B: ``fix-model-permissions.sh`` resolves a relative ``MODEL_CACHE_DIR`` against the
    CWD instead of the project root, so running it from anywhere else either silently no-ops
    or chowns an unrelated directory that happens to share the relative name.
  - Bug C: the ``.env`` parse for ``MODEL_CACHE_DIR`` is unanchored (matches
    ``EXTRA_MODEL_CACHE_DIR`` too) and truncates any path containing ``=``.
  - Bug D: an already-exported ``MODEL_CACHE_DIR`` is clobbered by whatever ``.env`` says,
    instead of winning (every other script in the repo lets the environment win over
    ``.env``).

Each of tests 4-7 is written to fail against the script as it stood before this issue's
fix and pass after -- run them before applying the fix to see the red, per root CLAUDE.md's
"a test you have not seen fail is not evidence" rule.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MODEL_SH = _REPO_ROOT / "scripts" / "fix-model-permissions.sh"
_VOLUME_SH = _REPO_ROOT / "scripts" / "fix-shared-volume-perms.sh"
_DOCKERFILE = _REPO_ROOT / "backend" / "Dockerfile.prod"

#: The container user's actual ownership (appuser: `useradd -u 1000` but `groupadd -r`, which
#: lands the GID at 999 in the built image -- issue #580). Exported so
#: ``test_chown_scripts_real_tree.py`` verifies the BUILT IMAGE against this same value
#: rather than a second, independently-spelled literal.
EXPECTED_OWNER = "1000:999"

#: The container mount points the image reserves for the pipeline's shared volumes
#: (backend/Dockerfile.prod, the "Reserve the pipeline's shared-volume mount points" RUN).
_RESERVED_PATHS_RE = re.compile(r"^RUN mkdir -p (/scratch\S*(?:\s+/\S+)*) &&", re.MULTILINE)

#: A compose ``- <volume>:<container path>`` mapping line.
_COMPOSE_VOLUME_RE = re.compile(r"^\s*-\s*([\w.-]+):(/[\w./-]+)")

#: A real ownership-changing ``chown -R <owner> <target>`` invocation (as opposed to a
#: comment that merely mentions the word "chown").
_CHOWN_TARGET_RE = re.compile(r"chown\s+-R\s+\S+\s+(\S+)")


def _reserved_container_paths() -> set[str]:
    text = _DOCKERFILE.read_text(encoding="utf-8")
    match = _RESERVED_PATHS_RE.search(text)
    assert match, "Dockerfile.prod no longer has the expected mkdir -p reservation line"
    return set(match.group(1).split())


def _compose_volumes_for(paths: set[str]) -> set[str]:
    volumes: set[str] = set()
    for compose_file in sorted(_REPO_ROOT.glob("docker-compose*.yml")):
        for line in compose_file.read_text(encoding="utf-8").splitlines():
            match = _COMPOSE_VOLUME_RE.match(line)
            if match and match.group(2) in paths:
                volumes.add(match.group(1))
    return volumes


def _script_volume_list() -> set[str]:
    text = _VOLUME_SH.read_text(encoding="utf-8")
    match = re.search(r"VOLUMES=\(([^)]*)\)", text)
    assert match, f"{_VOLUME_SH} no longer declares a VOLUMES=(...) array"
    return set(match.group(1).split())


def _chown_targets(text: str) -> list[str]:
    """Return the (quote-stripped) target of every real ``chown -R`` line."""
    targets = []
    for line in text.splitlines():
        match = _CHOWN_TARGET_RE.search(line)
        if match:
            targets.append(match.group(1).strip("\"'"))
    return targets


def test_shared_volume_list_matches_the_paths_the_image_reserves() -> None:
    """The script's VOLUMES array must name exactly the volumes compose mounts at the
    paths the Dockerfile reserves -- neither more (a volume this script can't help with)
    nor fewer (a volume the image reserves that this script silently ignores).

    Issue #661 E2 dropped this from 3 to 1: the pipeline consolidated onto ONE volume,
    pipeline_scratch (three namespaces: <file_uuid>/, engine/, diar/), removing the separate
    transcription-temp and diar-native-tmp volumes/mount points entirely. The count went from
    3 to 1 because two volumes were REMOVED, not because a parser broke -- the guard below
    (a synthetic two-path Dockerfile fragment) proves the parser still finds every path when
    there is more than one, so a real regression back to multiple volumes would still be
    caught.
    """
    reserved_paths = _reserved_container_paths()
    compose_volumes = _compose_volumes_for(reserved_paths)
    script_volumes = _script_volume_list()

    # Guard: a parser that matched nothing would report a vacuous "equal" pass.
    assert len(reserved_paths) == 1, f"expected 1 reserved path, parsed {reserved_paths}"
    assert len(compose_volumes) == 1, (
        f"expected 1 compose-mounted volume at the reserved path, parsed {compose_volumes}"
    )
    assert len(script_volumes) == 1, f"expected 1 volume in VOLUMES=(...), parsed {script_volumes}"

    assert compose_volumes == script_volumes, (
        f"fix-shared-volume-perms.sh's VOLUMES=(...) ({script_volumes}) does not match the "
        f"volumes compose actually mounts at the Dockerfile's reserved paths ({compose_volumes})"
    )


def test_reserved_paths_and_compose_volume_parsers_still_find_multiple_entries() -> None:
    """Must-fire guard for the len==1 assertions above (issue #661 E2): feed the two parsers
    synthetic two-path input and confirm both still parse every entry, so a real regression
    back to multiple reserved volumes would be caught rather than masked by a parser that
    silently stopped after the first match."""
    synthetic_dockerfile = (
        "RUN mkdir -p /scratch/opentranscribe /scratch/other-ns &&\n"
        "    chown appuser:appuser /scratch/opentranscribe /scratch/other-ns\n"
    )
    match = _RESERVED_PATHS_RE.search(synthetic_dockerfile)
    assert match is not None
    parsed_paths = set(match.group(1).split())
    assert parsed_paths == {"/scratch/opentranscribe", "/scratch/other-ns"}

    synthetic_compose = (
        "services:\n"
        "  backend:\n"
        "    volumes:\n"
        "      - pipeline_scratch:/scratch/opentranscribe\n"
        "      - other_volume:/scratch/other-ns\n"
    )
    volumes: set[str] = set()
    for line in synthetic_compose.splitlines():
        m = _COMPOSE_VOLUME_RE.match(line)
        if m and m.group(2) in parsed_paths:
            volumes.add(m.group(1))
    assert volumes == {"pipeline_scratch", "other_volume"}


def test_shared_volume_script_only_chowns_the_container_mount_point() -> None:
    """Every chown in the script must target the throwaway container's own mount point,
    never a host path or a bare/unexpected variable."""
    targets = _chown_targets(_VOLUME_SH.read_text(encoding="utf-8"))
    assert targets, "expected at least one chown line in fix-shared-volume-perms.sh"
    for target in targets:
        assert target == "/v", f"unexpected chown target in {_VOLUME_SH.name}: {target!r}"


def test_model_script_only_chowns_the_container_mount_point_or_the_resolved_cache_dir() -> None:
    """Every chown (real invocation or printed instruction) must target either the
    docker-mounted ``/models`` mount point or the resolved ``$MODEL_CACHE_DIR`` -- never a
    literal host path or anything else."""
    targets = _chown_targets(_MODEL_SH.read_text(encoding="utf-8"))
    assert targets, "expected at least one chown line in fix-model-permissions.sh"
    allowed = {"/models", "$MODEL_CACHE_DIR"}
    for target in targets:
        assert target in allowed, f"unexpected chown target in {_MODEL_SH.name}: {target!r}"


def test_model_cache_dir_is_anchored_to_project_root() -> None:
    """Bug B: a relative MODEL_CACHE_DIR (the shipped .env.example default, `./models`) must
    be anchored to PROJECT_ROOT, not resolved against whatever the caller's CWD happens to
    be -- otherwise a script run from anywhere else either silently no-ops or chowns an
    unrelated directory that happens to share the relative name."""
    text = _MODEL_SH.read_text(encoding="utf-8")
    assert re.search(r'case\s+"\$MODEL_CACHE_DIR"\s+in', text), (
        "fix-model-permissions.sh does not anchor a relative MODEL_CACHE_DIR to "
        '$PROJECT_ROOT (expected a `case "$MODEL_CACHE_DIR" in /*) ... esac` guard)'
    )
    assert "$PROJECT_ROOT/$MODEL_CACHE_DIR" in text, (
        "fix-model-permissions.sh's anchoring branch must rewrite MODEL_CACHE_DIR to "
        '"$PROJECT_ROOT/$MODEL_CACHE_DIR" when it is not already absolute'
    )


def test_model_script_env_var_wins_over_dotenv() -> None:
    """Bug D: an already-exported MODEL_CACHE_DIR must win over whatever .env says, matching
    every other script in the repo's `${VAR:-default}` convention -- not be clobbered by the
    .env read."""
    text = _MODEL_SH.read_text(encoding="utf-8")
    assert re.search(r'MODEL_CACHE_DIR="\$\{MODEL_CACHE_DIR:-\$\(', text), (
        "fix-model-permissions.sh must assign MODEL_CACHE_DIR with "
        '`MODEL_CACHE_DIR="${MODEL_CACHE_DIR:-$(grep ...)}"` so an already-exported value '
        "wins over the .env file, not a bare clobbering assignment"
    )


def test_dotenv_parse_is_anchored() -> None:
    """Bug C. Originally closed via python-dotenv (issue #590), but env_reader.py is a
    dev/CI-only helper never shipped to a standalone install (it is absent from
    release-manifest.txt and setup-opentranscribe.sh's curl list), so a real end-user
    install called a file that does not exist on disk -- issue #590/#581. Re-closed via
    scripts/common.sh's read_env_value(), which IS shipped (release-manifest.txt) and is
    the same helper opentranscribe.sh's shipped backup/restore arm already uses. It still
    matches the exact key via `^KEY=` anchoring (can never match `EXTRA_MODEL_CACHE_DIR`)
    and never truncates a value containing '=' (`cut -d= -f2-`, not `-f2`)."""
    text = _MODEL_SH.read_text(encoding="utf-8")
    assert re.search(r"read_env_value\s+MODEL_CACHE_DIR\s+\"\$PROJECT_ROOT/\.env\"", text), (
        "fix-model-permissions.sh must read MODEL_CACHE_DIR via "
        "scripts/common.sh's read_env_value() (shipped), not scripts/lib/env_reader.py "
        "(dev/CI-only, not in release-manifest.txt)"
    )
    assert not re.search(r"python3[^\n]*env_reader\.py", text), (
        "fix-model-permissions.sh must not CALL scripts/lib/env_reader.py -- it ships to "
        "end users and env_reader.py does not (issue #590/#581). (Explanatory comments "
        "naming the file are fine; an actual `python3 ... env_reader.py` invocation is not.)"
    )


def test_shared_volume_script_fails_loudly_when_it_repairs_nothing() -> None:
    """Bug A: when the loop ends having repaired zero volumes, the script must not silently
    exit 0 -- it must warn (naming the resolved PROJECT) and exit non-zero, since "repaired 0
    volume(s)" against a project that resolved wrong looks identical to a project with no
    volumes yet."""
    text = _VOLUME_SH.read_text(encoding="utf-8")
    assert re.search(r"if\s*\[\s*\"\$fixed\"\s*-eq\s*0\s*\]", text), (
        "fix-shared-volume-perms.sh must branch on `fixed -eq 0` to detect a no-op repair"
    )
    assert re.search(r"exit\s+[1-9]", text), (
        "fix-shared-volume-perms.sh must exit non-zero when it repaired nothing"
    )


def test_the_scripts_are_actually_read() -> None:
    """A suite that silently read an empty string would pass every test above."""
    for path in (_MODEL_SH, _VOLUME_SH):
        assert path.exists(), f"{path} does not exist"
        text = path.read_text(encoding="utf-8")
        assert len(text.splitlines()) > 20, f"{path} looks suspiciously short"

    tracked = subprocess.run(
        [
            "git",
            "-C",
            str(_REPO_ROOT),
            "ls-files",
            "--error-unmatch",
            "--",
            *[str(p.relative_to(_REPO_ROOT)) for p in (_MODEL_SH, _VOLUME_SH)],
        ],
        capture_output=True,
        text=True,
    )
    assert tracked.returncode == 0, f"expected both scripts to be git-tracked: {tracked.stderr}"
