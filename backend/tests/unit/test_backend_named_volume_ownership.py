"""Every backend-image named volume must be pre-owned by appuser (diar-native perms bug).

A Docker-managed NAMED volume (unlike a host bind mount) has no path to `chown` from
outside the image: Docker initializes its ownership from whatever the image already has
at that mount point *at build time*. `Dockerfile.prod` / `Dockerfile.lite` reserve
`/scratch/opentranscribe` and `/tmp/transcription` this way — `RUN mkdir -p ... && chown
appuser:appuser ...` before `USER appuser` — so a freshly created volume is writable by
the non-root worker that owns the pipeline's stage-to-stage WAV handoff.

`/tmp/diar-native` (docker-compose.diar-native.yml's `diar-native-tmp` volume) was added
without that reservation. Reproduced live on a fresh `./opentr.sh reset dev`: the volume
was created root-owned, and every real transcription through the diar-native sidecar
failed with a permission error on its first write. Nothing in the suite caught it —
every other test runs against an already-provisioned stack, and this bug only manifests
on a volume's FIRST creation. This test would have caught it: it derives, from the
compose files themselves, every named volume a backend-image service mounts, and checks
each target against Dockerfile.prod's actual reserved-path list.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE_PROD = REPO_ROOT / "backend" / "Dockerfile.prod"
DOCKERFILE_LITE = REPO_ROOT / "backend" / "Dockerfile.lite"

#: Every compose file that can mount a volume onto a backend-image service.
#: `docker-compose.diar-native.yml` is an aux overlay (--with-diar-native) and is
#: not merged into the base file's own volume list.
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.diar-native.yml")

_INTERPOLATION = re.compile(r"\$\{[^}]*\}")


def _compose_services(path: Path) -> dict[str, dict]:
    yaml = pytest.importorskip("yaml", reason="PyYAML parses the compose file")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(document.get("services") or {})


#: The compose-declared volume names this test cares about — the pipeline's own
#: cross-worker scratch volumes, same set `scripts/fix-shared-volume-perms.sh`
#: retrofits. Deliberately NOT every named volume in the compose files: those
#: also declare `postgres_data`/`opensearch_data`/`minio_data`/etc, which are
#: owned by their OWN image's entrypoint (postgres/opensearch/minio all run
#: their own uid, unrelated to `appuser`) and are out of scope here.
PIPELINE_SCRATCH_VOLUMES = {"pipeline_scratch", "transcription-temp", "diar-native-tmp"}


def _compose_named_volumes(path: Path) -> set[str]:
    yaml = pytest.importorskip("yaml", reason="PyYAML parses the compose file")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return set((document.get("volumes") or {}).keys()) & PIPELINE_SCRATCH_VOLUMES


def _named_volume_mount_targets(service: dict, named_volumes: set[str]) -> set[str]:
    """Mount targets on *service* whose source is a compose-declared named volume.

    Not filtered by which image the service builds from: the base
    `docker-compose.yml` sets neither `image:` nor `build:` on `backend` at all
    (an overlay supplies it — dev's `docker-compose.override.yml`, prod's
    `docker-compose.prod.yml`), so resolving "is this a backend-image service"
    from the base file alone is unreliable. These three volumes
    (`pipeline_scratch`/`transcription-temp`/`diar-native-tmp`) exist for
    exactly one purpose — the pipeline's cross-worker WAV handoff — and only a
    backend-image service ever has a reason to mount one.
    """
    targets: set[str] = set()
    for volume in service.get("volumes") or []:
        if not isinstance(volume, str):
            continue
        clean = _INTERPOLATION.sub("", volume).split("#")[0].strip()
        parts = clean.split(":")
        if len(parts) < 2:
            continue
        source, target = parts[0].strip(), parts[1].strip()
        if source in named_volumes:
            targets.add(target)
    return targets


def _all_backend_named_volume_targets() -> set[str]:
    targets: set[str] = set()
    for filename in COMPOSE_FILES:
        path = REPO_ROOT / filename
        if not path.is_file():
            continue
        named_volumes = _compose_named_volumes(path)
        for service in _compose_services(path).values():
            targets |= _named_volume_mount_targets(service, named_volumes)
    return targets


def _dockerfile_reserved_paths(dockerfile: Path) -> set[str]:
    """Paths covered by a `RUN mkdir -p <paths> && chown appuser:appuser <paths>` block."""
    text = dockerfile.read_text(encoding="utf-8")
    match = re.search(r"RUN mkdir -p ([^\n]+?) && \\\s*\n\s*chown appuser:appuser ([^\n]+)", text)
    if not match:
        return set()
    mkdir_paths = set(match.group(1).split())
    chown_paths = set(match.group(2).split())
    # Only a path both created AND chowned is actually reserved.
    return mkdir_paths & chown_paths


def test_the_compose_walk_finds_the_known_volumes() -> None:
    """Guard on the guard: a walk matching nothing would pass every assertion below."""
    targets = _all_backend_named_volume_targets()
    assert "/scratch/opentranscribe" in targets, "The compose walk found no named volumes at all."
    # Compared against parsed compose YAML, never used for file I/O — same false positive
    # docker-compose.yml's own transcription-temp mount line already annotates.
    assert "/tmp/diar-native" in targets, (  # noqa: S108  # nosec B108
        "docker-compose.diar-native.yml's diar-native-tmp volume was not found — "
        "either the walk broke or the overlay stopped declaring it."
    )


def test_the_reserved_path_parser_can_actually_fail() -> None:
    """Guard on the guard: a regex that always matches everything is not a check."""
    assert "/nonexistent/path" not in _dockerfile_reserved_paths(DOCKERFILE_PROD)


@pytest.mark.parametrize("dockerfile", [DOCKERFILE_PROD, DOCKERFILE_LITE], ids=lambda p: p.name)
def test_every_backend_named_volume_is_reserved_with_correct_ownership(
    dockerfile: Path,
) -> None:
    """Every named volume a backend-image service mounts must be pre-chowned.

    A named volume Docker has never created before inherits ownership from the
    image's own directory at that path — set nowhere means root-owned, and the
    non-root `appuser` worker (or the diar-native sidecar, same image) cannot
    write to it on first use.
    """
    mounted = _all_backend_named_volume_targets()
    reserved = _dockerfile_reserved_paths(dockerfile)

    missing = sorted(mounted - reserved)
    assert not missing, (
        f"{dockerfile.name} does not `mkdir -p ... && chown appuser:appuser ...` these "
        f"named-volume mount points before USER appuser, so a freshly created volume "
        f"lands root-owned and unwritable by the non-root worker: {missing}"
    )
