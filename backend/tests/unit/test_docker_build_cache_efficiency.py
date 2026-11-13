"""Guards for the backend build-cache layout (WASTE_AUDIT W2/W4/W5/W8/W13/W14a).

Each property is a cache or startup cost that regresses silently: nothing fails, the build
just gets slower. Docker docs: "Optimize cache usage" (cache mounts, layer order),
Compose spec (healthcheck.start_interval, pull_policy).
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
DOCKERFILES = [ROOT / "backend" / "Dockerfile.prod", ROOT / "backend" / "Dockerfile.lite"]


def _code(path: Path) -> str:
    """Dockerfile text with comments removed and line continuations joined."""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"\\\n", " ", text)
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _runtime(code: str) -> str:
    return code[code.rindex("\nFROM ") :]


@pytest.mark.parametrize("path", DOCKERFILES, ids=lambda p: p.name)
def test_every_pip_install_uses_a_cache_mount(path: Path) -> None:
    pip_runs = [ln for ln in _code(path).splitlines() if re.match(r"RUN .*pip install", ln)]
    assert pip_runs
    for ln in pip_runs:
        assert "--mount=type=cache,target=/root/.cache/pip" in ln, ln
        assert "--no-cache-dir" not in ln, ln


@pytest.mark.parametrize("path", DOCKERFILES, ids=lambda p: p.name)
def test_pip_upgrade_precedes_the_requirements_copy(path: Path) -> None:
    code = _code(path)
    assert code.index("--upgrade pip") < code.index("COPY requirements")


@pytest.mark.parametrize("path", DOCKERFILES, ids=lambda p: p.name)
def test_build_identity_args_sit_below_the_last_run(path: Path) -> None:
    rt = _runtime(_code(path))
    assert rt.rindex("\nRUN ") < rt.index("ARG GIT_SHA")
    assert rt.rindex("\nRUN ") < rt.index("ARG BUILD_TIME")


@pytest.mark.parametrize("path", DOCKERFILES, ids=lambda p: p.name)
def test_apt_security_refresh_sits_below_the_builder_copy(path: Path) -> None:
    rt = _runtime(_code(path))
    assert rt.index("COPY --from=builder") < rt.index("ARG APT_SECURITY_REFRESH")


def _compose(name: str) -> dict[str, dict[str, Any]]:
    doc: dict[str, Any] = yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))
    services: dict[str, dict[str, Any]] = doc["services"]
    return services


def test_dev_backend_image_is_built_by_two_services() -> None:
    services = _compose("docker-compose.override.yml")
    backend_image = services["backend"]["image"]
    shared = {n: s for n, s in services.items() if s.get("image") == backend_image}
    assert len(shared) > 5
    builders = [n for n, s in shared.items() if "build" in s]
    # `migrate` keeps an identical build: test_compose_migrate_service.py pins it to backend's.
    assert sorted(builders) == ["backend", "migrate"]
    for name, svc in shared.items():
        if name not in builders:
            assert svc.get("pull_policy") == "never", name


@pytest.mark.parametrize(
    "name", ["docker-compose.yml", "docker-compose.override.yml", "docker-compose.gpu-scale.yml"]
)
def test_backend_celery_and_frontend_healthchecks_set_start_interval(name: str) -> None:
    checked = 0
    for svc_name, svc in _compose(name).items():
        hc = svc.get("healthcheck")
        if not hc or "start_period" not in hc:
            continue
        if svc_name == "backend" or svc_name == "frontend" or svc_name.startswith("celery"):
            checked += 1
            assert hc.get("start_interval") == "5s", f"{name}:{svc_name}"
    assert checked
