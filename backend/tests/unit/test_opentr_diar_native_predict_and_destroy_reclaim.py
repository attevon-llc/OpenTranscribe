"""Two fixes on `feat/diar-native-e2e` (opentr.sh): the `--fresh` aux-recording vs.
auto-load ORDERING bug, and `fresh-destroy` reporting success after a silently
failed directory reclamation.

Both extract the REAL source out of `opentr.sh` and drive it via subprocess bash,
so a regression in the actual script fails here -- not a model of the script.
Neither touches a live checkout: everything runs in `tmp_path`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENTR = REPO_ROOT / "opentr.sh"

pytestmark = pytest.mark.skipif(
    not OPENTR.exists(), reason="opentr.sh not present in this checkout"
)


def _function_body(text: str, name: str) -> str:
    start = text.index(f"\n{name}() {{")
    end = text.index("\n}\n", start)
    return text[start : end + len("\n}\n")]


# ---------------------------------------------------------------------------
# Defect 1: an auto-loaded diar-native overlay must be decided (and therefore
# recordable in .aux) BEFORE fresh_write_aux runs, not after.
# ---------------------------------------------------------------------------


def _run_predict(tmp_path: Path, *, models_populated: bool, hf_token: str = "") -> dict[str, str]:
    """Drive the real `resolve_diar_native_models_dir` + `add_diar_native_overlay`
    bodies in `predict` mode, then run the exact aux-recording shape the --fresh
    block uses right after calling it. Returns the resulting env-like values.
    """
    source = OPENTR.read_text(encoding="utf-8")
    resolve_body = _function_body(source, "resolve_diar_native_models_dir")
    overlay_body = _function_body(source, "add_diar_native_overlay")
    assert "predict" in overlay_body, "predict mode removed from add_diar_native_overlay"

    models_dir = tmp_path / "diar-native-models"
    if models_populated:
        models_dir.mkdir(parents=True)
        (models_dir / "segmentation.onnx").write_bytes(b"x")

    script = f"""
set -e
{resolve_body}
{overlay_body}

DIAR_NATIVE_MODELS_DIR="{models_dir}"
export DIAR_NATIVE_MODELS_DIR
_aux_files=()

# The exact shape of the --fresh block: predict BEFORE fresh_write_aux.
add_diar_native_overlay predict
if [ -n "${{WITH_DIAR_NATIVE_FLAG:-}}" ]; then
  _aux_files+=("docker-compose.diar-native.yml")
fi

echo "WITH_DIAR_NATIVE_FLAG=${{WITH_DIAR_NATIVE_FLAG:-}}"
echo "COMPOSE_FILES=${{COMPOSE_FILES:-}}"
echo "AUX_FILES=${{_aux_files[*]:-}}"
"""
    full_env = {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "HUGGINGFACE_TOKEN": hf_token,
        "MODEL_CACHE_DIR": str(tmp_path / "models"),
    }
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env=full_env,
        capture_output=True,
        text=True,
        check=True,
    )
    out: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
    return out


def test_an_auto_loaded_sidecar_is_recorded_in_aux(tmp_path: Path):
    """The live bug: models present (or a token configured) -> the sidecar
    auto-loads -> it MUST land in the aux-files set computed at the same point,
    or fresh-destroy/stop/status will never learn it exists (issue #347 shape)."""
    result = _run_predict(tmp_path, models_populated=True)

    assert result["WITH_DIAR_NATIVE_FLAG"] == "auto"
    assert "docker-compose.diar-native.yml" in result["AUX_FILES"]


def test_predict_mode_never_touches_compose_files(tmp_path: Path):
    """predict must decide WITH_DIAR_NATIVE_FLAG without appending to
    $COMPOSE_FILES -- COMPOSE_FILES does not exist yet at the point in start_app
    where the --fresh block runs, so any append here would be silently
    clobbered by the real caller's later `COMPOSE_FILES="-f docker-compose.yml"`."""
    result = _run_predict(tmp_path, models_populated=True)

    assert result["WITH_DIAR_NATIVE_FLAG"] == "auto"
    assert result.get("COMPOSE_FILES", "") == ""


def test_no_auto_load_when_neither_models_nor_token_present(tmp_path: Path):
    """Control: nothing to auto-load -> nothing recorded."""
    result = _run_predict(tmp_path, models_populated=False, hf_token="")

    assert result.get("WITH_DIAR_NATIVE_FLAG", "") == ""
    assert "docker-compose.diar-native.yml" not in result.get("AUX_FILES", "")


def test_the_fresh_block_calls_predict_before_writing_aux():
    """Static ordering guard, named so a reordering regression fails loudly
    even if some future refactor changes the runtime behaviour above in a way
    that happens to still pass it by accident.

    `add_diar_native_overlay predict` must appear textually before
    `fresh_write_aux` within start_app's --fresh block, and the aux-append for
    WITH_DIAR_NATIVE_FLAG must sit between them.
    """
    source = OPENTR.read_text(encoding="utf-8")
    predict_idx = source.index("add_diar_native_overlay predict")
    aux_write_idx = source.index("fresh_write_aux ", predict_idx)
    append_idx = source.index('_aux_files+=("docker-compose.diar-native.yml")', predict_idx)

    assert predict_idx < append_idx < aux_write_idx, (
        "add_diar_native_overlay predict, its aux-file append, and fresh_write_aux "
        "must run in that order -- reordering reintroduces the #347 shape where an "
        "auto-loaded overlay is decided after .aux is already written"
    )


def test_the_predict_early_return_precedes_the_overlay_append(source_ordering_helper=None):
    """`predict` mode must return before appending the overlay to
    $COMPOSE_FILES, or a --fresh predict call would corrupt the compose chain
    for the real caller later in start_app."""
    source = OPENTR.read_text(encoding="utf-8")
    overlay_body = _function_body(source, "add_diar_native_overlay")
    predict_return_idx = overlay_body.index('if [ "$mode" = "predict" ]')
    compose_append_idx = overlay_body.index("Add the native diarization sidecar if requested")

    assert predict_return_idx < compose_append_idx


# ---------------------------------------------------------------------------
# Defect 2: fresh-destroy must not print success when the diar-native models
# directory could not actually be reclaimed.
# ---------------------------------------------------------------------------


def _extract_reclaim_snippet(source: str) -> str:
    start = source.index('  local diar_dir_left=""')
    success_line = source.index('"✅ Fresh deployment', start)
    # the snippet's own closing "fi" for the if/else, one line after the echo
    end = source.index("\n  fi\n", success_line) + len("\n  fi\n")
    return source[start:end]


def _run_reclaim(tmp_path: Path, *, rm_succeeds: bool, docker_available: bool) -> str:
    source = OPENTR.read_text(encoding="utf-8")
    snippet = _extract_reclaim_snippet(source)
    assert "diar_dir_left" in snippet

    diar_dir = tmp_path / "diar-native-models"
    diar_dir.mkdir(parents=True)
    (diar_dir / "segmentation.onnx").write_bytes(b"x")

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()

    if not rm_succeeds:
        # A fake `rm` that always reports failure and never removes anything --
        # simulating a root-owned tree this user cannot delete.
        (bin_dir / "rm").write_text("#!/bin/sh\nexit 1\n")
        (bin_dir / "rm").chmod(0o755)

    # A fake `docker` ALWAYS shadows the real one on PATH (this host has a real
    # docker daemon reachable from a bare `docker` on PATH -- this test must
    # never touch it). "succeeds" (exit 0) is a no-op fake chown, so the
    # underlying rm still can't remove the tree -- proving the code reports
    # failure honestly even when docker IS present but the reclaim still
    # doesn't work. "unavailable" makes it exit 127 like a real missing binary
    # would via `command -v`, without ever exposing the real one.
    (bin_dir / "docker").write_text(
        "#!/bin/sh\nexit 0\n" if docker_available else "#!/bin/sh\nexit 127\n"
    )
    (bin_dir / "docker").chmod(0o755)

    script = f'run_reclaim() {{\ndiar_dir="{diar_dir}"\n{snippet}\n}}\nrun_reclaim\n'
    # bin_dir FIRST so the fake docker/rm are found ahead of the real ones.
    env = {"HOME": str(tmp_path), "PATH": f"{bin_dir}:/usr/bin:/bin"}
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_a_failed_reclaim_reports_failure_not_success(tmp_path: Path):
    """The live bug, reproduced: `rm -rf` fails (root-owned tree), and the
    output must say so -- never the fixed '✅ ... destroyed' line regardless of
    outcome."""
    output = _run_reclaim(tmp_path, rm_succeeds=False, docker_available=True)

    assert "✅" not in output
    assert "INCOMPLETE" in output
    assert str(tmp_path / "diar-native-models") in output


def test_a_failed_reclaim_leaves_the_directory_and_its_contents_on_disk(tmp_path: Path):
    output = _run_reclaim(tmp_path, rm_succeeds=False, docker_available=True)
    assert "INCOMPLETE" in output  # sanity: same run as above

    diar_dir = tmp_path / "diar-native-models"
    assert diar_dir.is_dir()
    assert (diar_dir / "segmentation.onnx").is_file()


def test_a_successful_reclaim_still_reports_success(tmp_path: Path):
    """Control: the ordinary case (rm actually works) must still print the
    original success line, unregressed by the new failure-reporting path."""
    output = _run_reclaim(tmp_path, rm_succeeds=True, docker_available=True)

    assert "✅" in output
    assert "destroyed" in output
    assert "INCOMPLETE" not in output
    assert not (tmp_path / "diar-native-models").exists()


def test_the_reclaim_snippet_extractor_finds_the_real_block():
    """Must-fire control: proves the extractor is reading the actual fix, not
    an empty/missing region that would make every test above vacuous."""
    source = OPENTR.read_text(encoding="utf-8")
    snippet = _extract_reclaim_snippet(source)

    assert "docker run --rm" in snippet
    assert "busybox" in snippet
    assert 'diar_dir_left="$diar_dir"' in snippet
