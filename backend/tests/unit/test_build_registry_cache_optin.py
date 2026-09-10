"""The registry build cache is OPT-IN, and `--cache-to` never fires in local mode.

WHY THE CACHE EXISTS

`40-build.sh` builds every declared leg on the LOCAL builder and `80-publish.sh` rebuilds the
same amd64 legs from the same tree on `opentranscribe-multiarch`. Two docker-container
builders means two BuildKit caches, and before this change no `--cache-from`/`--cache-to`
appeared anywhere in `docker-build-push.sh` — `CACHE_FLAG` only ever held "" or `--no-cache`.

WHY IT IS OFF BY DEFAULT, AND WHY THAT IS TESTED

`type=registry` cache is a WRITE to Docker Hub. `BUILD_MODE=local` exists so a stage can
build while publishing nothing — that is 40-build.sh's stated contract and the reason the
scan/rehearse stages can run before anything reaches the registry. A `--cache-to` leaking
into local mode would quietly break it, and nothing else in the pipeline would notice: the
image tags would all be correct.

The default-off assertion matters for the same reason: this is an unmeasured optimisation
(nobody has yet timed a second build of an unchanged tree on a recreated builder), and an
optimisation switched on by default is one nobody has to justify.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PUSH_SH = REPO_ROOT / "scripts" / "docker-build-push.sh"

pytestmark = pytest.mark.skipif(
    not PUSH_SH.exists(), reason="scripts/docker-build-push.sh not present in this checkout"
)


def _extract_function(name: str) -> str:
    out = subprocess.run(
        ["sed", "-n", f"/^{name}()/,/^}}/p", str(PUSH_SH)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.strip(), f"{name}() not found in {PUSH_SH.name}"
    return out


def _cache_args(*, enabled: str, build_mode: str, platforms: str) -> list[str]:
    snippet = f"""
set -e
BUILD_CACHE_REGISTRY="{enabled}"
BUILD_MODE="{build_mode}"
{_extract_function("build_cache_args")}
build_cache_args "test/ot-lite" "{platforms}"
"""
    proc = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True)
    assert proc.returncode == 0, f"build_cache_args failed:\n{proc.stdout}\n{proc.stderr}"
    return proc.stdout.split()


@pytest.mark.unit
@pytest.mark.parametrize("build_mode", ["local", "push"])
def test_default_off_emits_nothing(build_mode: str) -> None:
    assert _cache_args(enabled="false", build_mode=build_mode, platforms="linux/amd64") == []


@pytest.mark.unit
def test_push_mode_reads_and_writes_the_cache() -> None:
    args = _cache_args(enabled="true", build_mode="push", platforms="linux/amd64")

    assert args == [
        "--cache-from",
        "type=registry,ref=test/ot-lite:buildcache-amd64",
        "--cache-to",
        "type=registry,ref=test/ot-lite:buildcache-amd64,mode=max",
    ], args


@pytest.mark.unit
def test_local_mode_never_writes_to_the_registry() -> None:
    """BUILD_MODE=local's whole contract is "publishes nothing" (40-build.sh line 2)."""
    args = _cache_args(enabled="true", build_mode="local", platforms="linux/amd64")

    assert "--cache-to" not in args, (
        f"a registry cache export is a PUSH; local mode must only read: {args}"
    )
    assert args == ["--cache-from", "type=registry,ref=test/ot-lite:buildcache-amd64"], args


@pytest.mark.unit
def test_the_cache_ref_is_scoped_to_the_platform_set() -> None:
    """One cache ref per platform set, so a per-leg build cannot clobber its sibling's cache.

    A cache manifest describes the platforms it was written for; sharing one ref across the
    amd64 and arm64 legs would make each leg's export overwrite the other's.
    """
    single = _cache_args(enabled="true", build_mode="push", platforms="linux/amd64")
    other = _cache_args(enabled="true", build_mode="push", platforms="linux/arm64")
    multi = _cache_args(enabled="true", build_mode="push", platforms="linux/amd64,linux/arm64")

    assert single[1] != other[1], f"per-leg caches must not share a ref: {single} vs {other}"
    assert "buildcache-amd64-arm64" in multi[1], multi
    assert multi[1] not in (single[1], other[1]), multi


@pytest.mark.unit
def test_every_release_built_component_passes_cache_args_to_buildx() -> None:
    """A helper nothing calls is a setting that silently does nothing.

    Checked structurally rather than by running a build: the four buildx invocations live in
    build_one_leg (backend + lite), build_frontend and build_docs. `blackwell` is excluded on
    purpose — it is never built by `all`/`auto` and never published.
    """
    text = PUSH_SH.read_text(encoding="utf-8")
    for func in ("build_one_leg", "build_frontend", "build_docs"):
        body = _extract_function(func)
        assert "build_cache_args" in body, f"{func}() never derives cache args"
        assert '"${cache_args[@]}"' in body, (
            f"{func}() derives cache args but never passes them to buildx"
        )
    assert 'BUILD_CACHE_REGISTRY="${BUILD_CACHE_REGISTRY:-false}"' in text, (
        "the opt-in must default to false at the one place it is declared"
    )
