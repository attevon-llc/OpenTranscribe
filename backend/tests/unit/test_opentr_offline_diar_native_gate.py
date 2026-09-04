"""`scripts/opentr-offline.sh`'s native-diarization sidecar overlay must respect
the SAME engine/deployment-mode configuration the other two front ends
(`opentr.sh`, `opentranscribe.sh`) already honour.

Before this fix, `build_compose_files()` loaded `docker-compose.diar-native.yml`
whenever the sidecar's weights were present ON DISK -- full stop. It never
checked `ENGINE_DIARIZER_BACKEND` or `DEPLOYMENT_MODE`, so an operator who
explicitly configured `ENGINE_DIARIZER_BACKEND=pyannote` (or an offline package
built with `DEPLOYMENT_MODE=lite`, which has no diar-native provisioning
toolchain at all) still got the sidecar loaded -- the documented rollback for
that config was defeated. Reproduced with the exact `.env` shape from issue
#655's fix item 5: `ENGINE_DIARIZER_BACKEND=pyannote, DEPLOYMENT_MODE=lite`,
weights present -> sidecar loaded anyway.

This extracts the REAL `build_compose_files` body and drives it via subprocess
bash, so a regression in the actual script fails here -- not a model of it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "opentr-offline.sh"

pytestmark = pytest.mark.skipif(
    not SCRIPT.exists(), reason="scripts/opentr-offline.sh not present in this checkout"
)


def _function_body(text: str, name: str) -> str:
    start = text.index(f"\n{name}() {{")
    end = text.index("\n}\n", start)
    return text[start : end + len("\n}\n")]


def _build(
    tmp_path: Path,
    *,
    engine_diarizer_backend: str | None,
    deployment_mode: str | None,
    models_populated: bool = True,
) -> str:
    """Run the real `build_compose_files` body against a throwaway install dir
    and return the resulting $COMPOSE_FILES string."""
    source = SCRIPT.read_text(encoding="utf-8")
    body = _function_body(source, "build_compose_files")

    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / "docker-compose.yml").touch()
    (install_dir / "docker-compose.offline.yml").touch()
    (install_dir / "docker-compose.diar-native.yml").touch()
    (install_dir / "docker-compose.diar-native-gpu.yml").touch()

    models_dir = install_dir / "models" / "diar-native"
    if models_populated:
        models_dir.mkdir(parents=True)
        (models_dir / "segmentation.onnx").write_bytes(b"x")

    env_lines = []
    if engine_diarizer_backend is not None:
        env_lines.append(f"ENGINE_DIARIZER_BACKEND={engine_diarizer_backend}")
    if deployment_mode is not None:
        env_lines.append(f"DEPLOYMENT_MODE={deployment_mode}")
    (install_dir / ".env").write_text("\n".join(env_lines) + "\n")

    # Stub the print_* helpers so the snippet is self-contained (they're defined
    # elsewhere in the file and just echo with color codes -- irrelevant here).
    stubs = "\n".join(
        f"{name}() {{ :; }}" for name in ("print_info", "print_warning", "print_error")
    )

    script = f"""
set -e
INSTALL_DIR="{install_dir}"
DIAR_NATIVE_COMPOSE_FILE="$INSTALL_DIR/docker-compose.diar-native.yml"
DIAR_NATIVE_GPU_COMPOSE_FILE="$INSTALL_DIR/docker-compose.diar-native-gpu.yml"
BASE_COMPOSE_FILE="$INSTALL_DIR/docker-compose.yml"
OFFLINE_COMPOSE_FILE="$INSTALL_DIR/docker-compose.offline.yml"
GPU_SCALE_COMPOSE_FILE="$INSTALL_DIR/docker-compose.gpu-scale.yml"
{stubs}
{body}
build_compose_files false
echo "COMPOSE_FILES=$COMPOSE_FILES"
"""
    # No nvidia-smi on PATH -> GPU reservation branch never taken, keeping the
    # assertions below about the base overlay's presence/absence unambiguous.
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    line = next(line for line in result.stdout.splitlines() if line.startswith("COMPOSE_FILES="))
    return line.partition("=")[2]


def test_lite_deployment_mode_skips_the_sidecar_even_with_weights_present(tmp_path: Path):
    """The exact issue #655 reproduction: pyannote backend + lite mode + weights
    present must NOT load the sidecar."""
    result = _build(
        tmp_path, engine_diarizer_backend="pyannote", deployment_mode="lite", models_populated=True
    )
    assert "docker-compose.diar-native.yml" not in result


def test_non_native_engine_backend_skips_the_sidecar(tmp_path: Path):
    """A deployment that explicitly configured pyannote must not get the sidecar
    just because old weights happen to still be on disk."""
    result = _build(
        tmp_path, engine_diarizer_backend="pyannote", deployment_mode="full", models_populated=True
    )
    assert "docker-compose.diar-native.yml" not in result


def test_native_engine_with_weights_present_still_loads_the_sidecar(tmp_path: Path):
    """Control: the ordinary case (native backend, weights present, not lite)
    must still load the sidecar -- the fix must not regress the happy path."""
    result = _build(
        tmp_path, engine_diarizer_backend="native", deployment_mode="full", models_populated=True
    )
    assert "docker-compose.diar-native.yml" in result


def test_default_engine_backend_is_native(tmp_path: Path):
    """ENGINE_DIARIZER_BACKEND unset must default to native (matching
    opentr.sh's `${ENGINE_DIARIZER_BACKEND:-native}` predicate), not silently
    skip the sidecar for every install missing that one .env line."""
    result = _build(
        tmp_path, engine_diarizer_backend=None, deployment_mode="full", models_populated=True
    )
    assert "docker-compose.diar-native.yml" in result


def test_lite_with_native_backend_and_preseeded_weights_loads_the_sidecar(tmp_path: Path):
    """Issue #654: lite lacks the export TOOLCHAIN, not the diar-server BINARY
    (backend/Dockerfile.lite ships it, #660), and an offline install never
    provisions weights at install time regardless of mode -- they only ever
    arrive pre-seeded in the package. So a lite install with weights already
    on disk and an explicit native backend must load the sidecar, not skip it
    just because DEPLOYMENT_MODE=lite."""
    result = _build(
        tmp_path, engine_diarizer_backend="native", deployment_mode="lite", models_populated=True
    )
    assert "docker-compose.diar-native.yml" in result


def test_lite_with_no_preseeded_weights_still_skips_the_sidecar(tmp_path: Path):
    """Control: lite with NO pre-seeded weights has no way to ever provision
    them (no export toolchain, no network route to HuggingFace in an offline
    install) and must still skip."""
    result = _build(
        tmp_path, engine_diarizer_backend="native", deployment_mode="lite", models_populated=False
    )
    assert "docker-compose.diar-native.yml" not in result


def test_no_weights_still_skips_the_sidecar_regardless_of_engine(tmp_path: Path):
    """Control: the pre-existing weights-presence gate must still function."""
    result = _build(
        tmp_path, engine_diarizer_backend="native", deployment_mode="full", models_populated=False
    )
    assert "docker-compose.diar-native.yml" not in result


def test_the_offline_compose_file_always_loads_last():
    """Static invariant this fix must not disturb: offline.yml overrides
    everything, so it must remain the last -f in the resolved chain."""
    source = SCRIPT.read_text(encoding="utf-8")
    body = _function_body(source, "build_compose_files")
    assert body.rstrip().endswith('COMPOSE_FILES="$COMPOSE_FILES -f $OFFLINE_COMPOSE_FILE"\n}')
