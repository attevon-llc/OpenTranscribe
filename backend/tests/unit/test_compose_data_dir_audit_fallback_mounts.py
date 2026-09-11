"""Mount coverage for `DATA_DIR` and `AUDIT_LOG_FALLBACK_PATH` (issues #870/#877/#878).

Before this fix, **zero** compose file mounted either path:

  - `DATA_DIR` (default `/app/data`) holds the GDPR erasure journal
    (`gdpr/erasure-journal.jsonl`, `app/services/erasure_ledger_service.py`), append-only.
  - `AUDIT_LOG_FALLBACK_PATH` (default `/var/log/opentranscribe/audit-fallback.jsonl`) is
    where audit events land when OpenSearch is unreachable
    (`app/auth/audit.py:_write_fallback_log`).

Dev was accidentally durable for the first one only — `docker-compose.override.yml`'s
`./backend:/app` bind happens to cover `/app/data` too — but every other deployment shape
(prod, prod+nginx, prod+pki, lite, offline) had nothing, so a routine
`docker compose up -d --force-recreate` silently destroyed both files. The fix adds two named
volumes (`app_data`, `audit_fallback_logs`) to the base `docker-compose.yml`, mounted on
`backend` and `celery-cpu-worker` (the two services whose code paths actually touch these
files — see `app/core/celery.py`'s `task_routes`: the GDPR reconciliation sweep and the
audit-writing sweeps all route to the `utility`/`cpu` queues `celery-cpu-worker` consumes).

This module runs the REAL `docker compose ... config` for each deployment shape that matters
— never a manual YAML-merge simulation. `test_compose_sentence_splitter_mounts.py` already
documents why: compose's `volumes` merge is by TARGET across files, not "the last file with a
`volumes:` key wins", and a hand-rolled merge simulation got that backwards once already.

A second, independent hazard (issue #878): the moment `DATA_DIR` is backed by a named volume
in the BASE file, `docker-compose.bench.yml` inherits it like everything else does — same
volume NAME, and bench does not isolate `COMPOSE_PROJECT_NAME` either — so a benchmark run
would silently share the live/dev deployment's GDPR journal and audit fallback log. Fixed by
giving bench its own `app_bench_data`/`audit_fallback_bench_data` volumes, the same pattern
already used for postgres/minio/redis/opensearch/flower there. This module asserts the bench
volume NAMES actually differ from the non-bench ones, not just that a mount exists.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker CLI not present in this checkout"
)

#: The two container paths this fix must keep durable, by target.
MOUNT_TARGETS = ("/app/data", "/var/log/opentranscribe")

#: Every compose overlay combination whose `backend`/`celery-cpu-worker` resolution must
#: include a volume mount at both `MOUNT_TARGETS`. Labeled with the deployment shape it
#: represents; mirrors the combos `opentr.sh` actually assembles (`docker-compose.lite.yml`
#: is never loaded standalone — it has no image/build for `docs`/`frontend` on its own, so
#: opentr.sh always adds it on top of `docker-compose.prod.yml`).
NON_BENCH_COMBOS: dict[str, tuple[str, ...]] = {
    "dev": ("docker-compose.yml", "docker-compose.override.yml"),
    "prod": ("docker-compose.yml", "docker-compose.prod.yml"),
    "prod+nginx": ("docker-compose.yml", "docker-compose.prod.yml", "docker-compose.nginx.yml"),
    "prod+nginx+pki": (
        "docker-compose.yml",
        "docker-compose.prod.yml",
        "docker-compose.nginx.yml",
        "docker-compose.pki.yml",
    ),
    "prod+lite": ("docker-compose.yml", "docker-compose.prod.yml", "docker-compose.lite.yml"),
    "prod+lite+nginx": (
        "docker-compose.yml",
        "docker-compose.prod.yml",
        "docker-compose.lite.yml",
        "docker-compose.nginx.yml",
    ),
    "offline": ("docker-compose.yml", "docker-compose.offline.yml"),
}

#: Services whose real code paths touch DATA_DIR / AUDIT_LOG_FALLBACK_PATH — see the module
#: docstring for the queue-routing evidence.
RELEVANT_SERVICES = ("backend", "celery-cpu-worker")

_CONFIG_TIMEOUT_S = 60


def _resolve(overlays: tuple[str, ...]) -> dict:
    """Run the real `docker compose ... config` for a chain of overlay files.

    Returns the parsed YAML document. Any overlay missing from this checkout is a hard
    failure, not a skip — a coverage test that silently shrinks its own combo list on a
    stale checkout would stop meaning what it says.
    """
    yaml = pytest.importorskip("yaml")
    missing = [name for name in overlays if not (REPO_ROOT / name).is_file()]
    assert not missing, f"compose overlay(s) not found in this checkout: {missing}"

    args = ["docker", "compose"]
    for name in overlays:
        args += ["-f", name]
    args.append("config")

    result = subprocess.run(
        args,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=_CONFIG_TIMEOUT_S,
        check=False,
    )
    assert result.returncode == 0, (
        f"docker compose config failed for {overlays}:\nstdout={result.stdout}\n"
        f"stderr={result.stderr}"
    )
    document: dict = yaml.safe_load(result.stdout)
    return document


def _mount_sources_by_target(service: dict) -> dict[str, str]:
    """`{target: source}` for every resolved volume mount on a service.

    Reads the STRUCTURED form `docker compose config` emits (a list of
    `{type, source, target, ...}` mappings) rather than the short `source:target` string
    form the raw YAML files use — `config` always normalizes to the structured form, and
    reading it directly avoids re-deriving compose's own `${VAR:-default}` interpolation.
    """
    sources: dict[str, str] = {}
    for volume in service.get("volumes") or []:
        if isinstance(volume, dict) and "target" in volume:
            sources[volume["target"]] = volume.get("source", "")
    return sources


@pytest.mark.parametrize("combo_label", sorted(NON_BENCH_COMBOS))
def test_deployment_shape_mounts_both_durable_paths(combo_label: str) -> None:
    """Every deployment shape must keep both DATA_DIR and AUDIT_LOG_FALLBACK_PATH durable."""
    document = _resolve(NON_BENCH_COMBOS[combo_label])
    services = document.get("services") or {}

    missing: list[str] = []
    for service_name in RELEVANT_SERVICES:
        assert service_name in services, (
            f"{combo_label}: expected service {service_name!r} in the resolved config"
        )
        mounted = _mount_sources_by_target(services[service_name])
        for target in MOUNT_TARGETS:
            if target not in mounted:
                missing.append(f"{service_name} is missing a mount at {target}")

    assert not missing, (
        f"{combo_label} lost a durable-state mount added for issues #870/#877: {missing}"
    )


def test_bench_overlay_isolates_both_volumes_from_the_live_deployment() -> None:
    """Issue #878: bench must never resolve to the SAME volume name as a live deployment.

    A shared name is a shared Docker volume regardless of the different container_names
    and image tags bench already uses elsewhere in this file — `docker volume` identity is
    the compose project name plus the volume name, and bench does not isolate
    COMPOSE_PROJECT_NAME the way `--fresh` deployments do.
    """
    prod = _resolve(("docker-compose.yml", "docker-compose.prod.yml"))
    bench = _resolve(("docker-compose.yml", "docker-compose.bench.yml"))

    prod_services = prod.get("services") or {}
    bench_services = bench.get("services") or {}

    collisions: list[str] = []
    missing: list[str] = []
    for service_name in RELEVANT_SERVICES:
        prod_mounts = _mount_sources_by_target(prod_services[service_name])
        bench_mounts = _mount_sources_by_target(bench_services[service_name])
        for target in MOUNT_TARGETS:
            if target not in bench_mounts:
                missing.append(f"{service_name} (bench) is missing a mount at {target}")
                continue
            if bench_mounts[target] == prod_mounts.get(target):
                collisions.append(
                    f"{service_name}: bench and prod both resolve {target} to volume "
                    f"{bench_mounts[target]!r}"
                )

    assert not missing, f"bench overlay lost a durable-state mount: {missing}"
    assert not collisions, (
        f"bench overlay shares a live-deployment volume name — a benchmark run can write "
        f"into the real GDPR erasure journal / audit fallback log: {collisions}"
    )


def test_the_mount_source_extractor_can_actually_detect_a_missing_mount() -> None:
    """Guard on the guard: prove `_mount_sources_by_target` distinguishes present from absent.

    Without this, a helper that silently returned `{}` for every service would make every
    assertion above vacuously pass by reporting everything "missing" — which would fail
    loudly rather than pass silently, but a helper that always returns something non-empty
    could just as easily hide a real gap. Exercise both directions directly.
    """
    present = {
        "volumes": [
            {"type": "volume", "source": "app_data", "target": "/app/data"},
            {"type": "bind", "source": "/host/path", "target": "/var/log/opentranscribe"},
        ]
    }
    absent = {"volumes": [{"type": "volume", "source": "other", "target": "/scratch/x"}]}
    empty: dict = {"volumes": []}

    mounted = _mount_sources_by_target(present)
    for target in MOUNT_TARGETS:
        assert target in mounted, "extractor failed to find a mount that is genuinely present"

    for service in (absent, empty):
        mounted = _mount_sources_by_target(service)
        for target in MOUNT_TARGETS:
            assert target not in mounted, "extractor reported a mount that does not exist"


def test_the_combo_list_still_resolves_to_the_files_on_disk() -> None:
    """Guard on the guard: every overlay this module names must exist in this checkout.

    A silently-shrinking combo list (a renamed or deleted compose file quietly dropped from
    the dict) would make the parametrized test above pass by testing fewer shapes, not by
    the fix actually holding.
    """
    named = {name for overlays in NON_BENCH_COMBOS.values() for name in overlays}
    named |= {"docker-compose.bench.yml"}
    missing = sorted(name for name in named if not (REPO_ROOT / name).is_file())
    assert not missing, f"compose overlay(s) named by this module do not exist: {missing}"
