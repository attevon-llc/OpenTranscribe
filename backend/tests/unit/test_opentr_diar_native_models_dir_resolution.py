"""`opentr.sh`'s `resolve_diar_native_models_dir` must prefer a POPULATED models
directory, not merely an EXISTING one.

Docker auto-creates an empty directory at a bind-mount source that doesn't exist
yet. `docker-compose.diar-native.yml` mounts
``${DIAR_NATIVE_MODELS_DIR:-${MODEL_CACHE_DIR:-./models}/diar-native}:/models:ro``,
so the very first `docker compose up` of the overlay (or a rebuild that recreated
the volume) leaves an EMPTY `./models/diar-native` on disk forever after. The
resolver used to check only `[ -d "$standard" ]`, so that empty directory was then
silently preferred over a populated legacy export
(`/mnt/nvm/repos/diar-native/models_folded`) on every later invocation --
reproduced live on 2026-08-31: diar-native restart-looped on "File at
/models/segmentation-3.0.onnx does not exist" the moment that empty directory
existed, even though the real weights were still on disk at the legacy path.

The fix matches `opentranscribe.sh`'s own non-emptiness check
(`[ -d ... ] && [ -n "$(ls -A ...)" ]`).

This test extracts the real function source (not a reimplementation) and drives
it via subprocess bash, so a regression in the actual script fails here -- not a
model of the script.
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

_LEGACY_PATH_LITERAL = "/mnt/nvm/repos/diar-native/models_folded"


def _function_body(text: str, name: str) -> str:
    """Source of one top-level ``name() { ... }`` block, closing brace included."""
    start = text.index(f"\n{name}() {{")
    end = text.index("\n}\n", start)
    return text[start : end + len("\n}\n")]


def _resolve(tmp_path: Path, *, env: dict[str, str]) -> str:
    """Run the REAL `resolve_diar_native_models_dir` body via subprocess bash.

    The hardcoded legacy literal is substituted for a controllable temp directory
    so the "legacy path populated" branch can be exercised without depending on
    (or touching) this workstation's real diar-native export. `MODEL_CACHE_DIR` is
    always set to an absolute path so the resolved value is directly comparable —
    the function's own `./models` fallback is relative-to-cwd by design, not a bug.
    """
    fake_legacy = tmp_path / "fake-legacy"
    body = _function_body(OPENTR.read_text(encoding="utf-8"), "resolve_diar_native_models_dir")
    assert _LEGACY_PATH_LITERAL in body, "legacy path literal moved -- update this test"
    body = body.replace(_LEGACY_PATH_LITERAL, str(fake_legacy))

    full_env = {"MODEL_CACHE_DIR": str(tmp_path / "models"), **env, "PATH": "/usr/bin:/bin"}
    script = f'{body}\nresolve_diar_native_models_dir\necho "$DIAR_NATIVE_MODELS_DIR"\n'
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env=full_env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip().splitlines()[-1]


def test_an_empty_standard_directory_no_longer_wins_over_a_populated_legacy_one(tmp_path: Path):
    """The exact live bug: Docker's auto-created empty mount point must not be
    preferred over a real, populated export elsewhere."""
    standard = tmp_path / "models" / "diar-native"
    standard.mkdir(parents=True)  # exists, but EMPTY -- exactly what Docker leaves behind

    legacy = tmp_path / "fake-legacy"
    legacy.mkdir(parents=True)
    (legacy / "segmentation-3.0.onnx").write_bytes(b"onnx")

    resolved = _resolve(tmp_path, env={"HOME": str(tmp_path)})

    assert resolved == str(legacy), (
        f"expected the populated legacy path to win over the empty standard "
        f"directory, got {resolved!r}"
    )


def test_a_populated_standard_directory_is_still_preferred(tmp_path: Path):
    """Control: the ordinary self-hosted case (standard path has the export)
    must not regress -- legacy is irrelevant here and never even created."""
    standard = tmp_path / "models" / "diar-native"
    standard.mkdir(parents=True)
    (standard / "segmentation-3.0.onnx").write_bytes(b"onnx")

    resolved = _resolve(tmp_path, env={"HOME": str(tmp_path)})

    assert resolved == str(standard)


def test_neither_populated_falls_back_to_the_standard_path(tmp_path: Path):
    """Neither location has an export: the function must still produce a value
    (the standard path), never an empty string or a crash."""
    resolved = _resolve(tmp_path, env={"HOME": str(tmp_path)})

    assert resolved == str(tmp_path / "models" / "diar-native")


def test_an_explicit_env_var_always_wins_regardless_of_directory_contents(tmp_path: Path):
    """An operator-set DIAR_NATIVE_MODELS_DIR must never be second-guessed by the
    presence-probing logic, even if it points at something empty or missing."""
    explicit = tmp_path / "wherever-the-operator-said"

    resolved = _resolve(
        tmp_path, env={"HOME": str(tmp_path), "DIAR_NATIVE_MODELS_DIR": str(explicit)}
    )

    assert resolved == str(explicit)
