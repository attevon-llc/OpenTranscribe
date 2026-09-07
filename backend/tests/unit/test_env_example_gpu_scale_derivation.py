"""Issue #656 Step 8: the shipped .env.example must never oversubscribe the diar-native
sidecar out of the box.

``docker-compose.gpu-scale.yml`` derives ``GPU_SCALE_WORKERS`` from
``DIAR_NATIVE_MAX_INFLIGHT`` when the former is unset — but an explicit, UNCOMMENTED
``GPU_SCALE_WORKERS=4`` in the shipped template overrides that derivation and reproduces the
exact 4-vs-2 contention the derivation exists to prevent (docker-compose.gpu-scale.yml:33-35's
own comment says as much). This parses the real repo-root ``.env.example`` — a hardcoded
literal here would not catch a regression to the file itself.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"


def _parse_env_example() -> dict[str, str]:
    """Uncommented ``KEY=value`` lines only — a commented-out ``#KEY=value`` must not count as
    set, since that is exactly the "leave it to the derivation" state Step 8 requires.
    """
    values: dict[str, str] = {}
    for raw_line in _ENV_EXAMPLE.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


class TestGpuScaleWorkersDerivation:
    def test_env_example_exists(self):
        assert _ENV_EXAMPLE.is_file()

    def test_gpu_scale_workers_is_not_set_uncommented(self):
        """An uncommented GPU_SCALE_WORKERS overrides docker-compose.gpu-scale.yml's
        ``${GPU_SCALE_WORKERS:-${DIAR_NATIVE_MAX_INFLIGHT:-2}}`` derivation outright — today's
        shipped ``GPU_SCALE_WORKERS=4`` against ``DIAR_NATIVE_MAX_INFLIGHT=2`` is exactly the
        contention issue #656 Step 8 removes.
        """
        values = _parse_env_example()
        assert "GPU_SCALE_WORKERS" not in values, (
            "GPU_SCALE_WORKERS must be commented out in the shipped .env.example so the "
            f"compose derivation from DIAR_NATIVE_MAX_INFLIGHT applies; found "
            f"GPU_SCALE_WORKERS={values.get('GPU_SCALE_WORKERS')!r} uncommented"
        )

    def test_effective_workers_never_exceeds_diar_native_max_inflight(self):
        """Simulates the compose derivation directly: with GPU_SCALE_WORKERS unset, the
        effective worker count IS DIAR_NATIVE_MAX_INFLIGHT — never more."""
        values = _parse_env_example()
        max_inflight = int(values["DIAR_NATIVE_MAX_INFLIGHT"])
        effective_workers = int(values.get("GPU_SCALE_WORKERS", max_inflight))
        assert effective_workers <= max_inflight
