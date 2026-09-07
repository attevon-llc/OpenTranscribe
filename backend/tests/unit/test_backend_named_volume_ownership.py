"""Every backend-image named volume must be pre-owned by appuser (diar-native perms bug).

A Docker-managed NAMED volume (unlike a host bind mount) has no path to `chown` from
outside the image: Docker initializes its ownership from whatever the image already has
at that mount point *at build time*. `Dockerfile.prod` / `Dockerfile.lite` reserve
`/scratch/opentranscribe` this way — `RUN mkdir -p ... && chown appuser:appuser ...`
before `USER appuser` — so a freshly created volume is writable by the non-root worker
that owns the pipeline's stage-to-stage WAV handoff. Issue #661 E2 consolidated what used
to be three separate volumes/mount points (`pipeline_scratch`, `transcription-temp` at
`/tmp/transcription`, `diar-native-tmp` at `/tmp/diar-native`) onto this one volume with
three internal namespaces.

A volume mounted at a new path without a matching Dockerfile reservation was reproduced
live on a fresh `./opentr.sh reset dev` for the original `diar-native-tmp` addition: the
volume was created root-owned, and every real transcription through the diar-native
sidecar failed with a permission error on its first write. Nothing in the suite caught it
— every other test runs against an already-provisioned stack, and this bug only manifests
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

_INTERPOLATION = re.compile(r"\$\{[^}]*\}")

#: A named-volume mount TARGET under one of these prefixes is a pipeline scratch
#: path — this is what actually distinguishes it from a data-store volume, not
#: which volume name happens to be declared. A hardcoded volume-name allowlist
#: was tried first and does not catch the bug class this test exists for: a
#: *new* scratch volume, added the same way `diar-native-tmp` was, would not be
#: in the allowlist either. Postgres/OpenSearch/MinIO/Redis data dirs
#: (`/var/lib/postgresql/data`, `/usr/share/opensearch/data`, `/data`, ...) all
#: fall outside both prefixes — they are owned by their own image's entrypoint,
#: not `appuser`, and are correctly out of scope.
_PIPELINE_SCRATCH_PREFIXES = ("/scratch/", "/tmp/")  # noqa: S108  # nosec B108


def _compose_services(path: Path) -> dict[str, dict]:
    yaml = pytest.importorskip("yaml", reason="PyYAML parses the compose file")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(document.get("services") or {})


def _compose_named_volumes(path: Path) -> set[str]:
    yaml = pytest.importorskip("yaml", reason="PyYAML parses the compose file")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return set((document.get("volumes") or {}).keys())


def _named_volume_mount_targets(service: dict, named_volumes: set[str]) -> set[str]:
    """Pipeline-scratch mount targets on *service* whose source is a named volume.

    Not filtered by which image the service builds from: the base
    `docker-compose.yml` sets neither `image:` nor `build:` on `backend` at all
    (an overlay supplies it — dev's `docker-compose.override.yml`, prod's
    `docker-compose.prod.yml`), so resolving "is this a backend-image service"
    from the base file alone is unreliable. A bind mount (source starting with
    `/`, `.`, or `${VAR}`) is never in `named_volumes`, so the model-cache mounts
    under `/home/appuser/.cache/...` are excluded without needing their own rule.
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
        if source in named_volumes and target.startswith(_PIPELINE_SCRATCH_PREFIXES):
            targets.add(target)
    return targets


def _all_backend_named_volume_targets() -> set[str]:
    """Walk EVERY docker-compose*.yml, not a fixed subset.

    A future overlay that declares and mounts its own scratch volume — the
    exact shape `docker-compose.diar-native.yml` added — must not be able to
    sit outside whatever short list this test happened to enumerate.
    """
    targets: set[str] = set()
    for path in sorted(REPO_ROOT.glob("docker-compose*.yml")):
        named_volumes = _compose_named_volumes(path)
        for service in _compose_services(path).values():
            targets |= _named_volume_mount_targets(service, named_volumes)
    return targets


def _dockerfile_reserved_paths(dockerfile: Path) -> set[str]:
    """Paths covered by a `RUN mkdir -p <paths> && chown appuser:appuser <paths>` block."""
    text = dockerfile.read_text(encoding="utf-8")
    # Union across every match, not just the first: a second such block earlier in
    # the file would otherwise silently shadow this one. The regex requires the
    # exact single-line `mkdir -p ... && \` + `chown appuser:appuser ...` shape this
    # repo's Dockerfiles currently use — a reformat onto per-path continuation
    # lines fails closed (reports the path as unreserved rather than silently
    # passing), but with a misleading "does not chown" message, so that
    # possibility is called out here and in the assertion text below.
    reserved: set[str] = set()
    for match in re.finditer(
        r"RUN mkdir -p ([^\n]+?) && \\\s*\n\s*chown appuser:appuser ([^\n]+)", text
    ):
        mkdir_paths = set(match.group(1).split())
        chown_paths = set(match.group(2).split())
        # Only a path both created AND chowned is actually reserved.
        reserved |= mkdir_paths & chown_paths
    return reserved


def test_the_compose_walk_finds_the_known_volumes() -> None:
    """Guard on the guard: a walk matching nothing would pass every assertion below.

    Issue #661 E2 consolidated the pipeline onto ONE named volume (``pipeline_scratch``,
    mounted at ``/scratch/opentranscribe``) with three internal namespaces
    (``<file_uuid>/``, ``engine/``, ``diar/``) — the separate ``diar-native-tmp`` volume this
    test used to also assert on (mounted at ``/tmp/diar-native``) no longer exists in any
    overlay, so that half of the guard is retired rather than failing forever. The
    non-vacuousness property this test protects — "the walk actually finds something, not
    nothing" — is now carried DELIBERATELY by
    ``test_a_brand_new_unreserved_scratch_volume_is_caught`` below (a synthetic new-volume
    case, independent of which real volumes exist today) plus the assertion added here that
    the walk visited more than one compose file, so a walk silently scoped to a single file
    would still fail loudly.
    """
    compose_files = sorted(REPO_ROOT.glob("docker-compose*.yml"))
    assert len(compose_files) > 1, (
        "expected multiple docker-compose*.yml files — the walk must span all of them, "
        "not silently narrow to one"
    )
    targets = _all_backend_named_volume_targets()
    assert "/scratch/opentranscribe" in targets, "The compose walk found no named volumes at all."


def test_the_reserved_path_parser_can_actually_fail() -> None:
    """Guard on the guard: a regex that always matches everything is not a check."""
    assert "/nonexistent/path" not in _dockerfile_reserved_paths(DOCKERFILE_PROD)


def test_a_brand_new_unreserved_scratch_volume_is_caught() -> None:
    """Guard on the guard: reproduces the bug class this file exists for.

    An earlier version of this test scoped the walk to a hardcoded set of
    already-known volume names (`{"pipeline_scratch", "transcription-temp",
    "diar-native-tmp"}`). That would not have caught `diar-native-tmp` itself
    at the moment it was introduced — a NEW volume is, by definition, not in a
    list of volumes someone already remembered to add. This asserts the
    prefix-based rule directly: any named volume newly mounted under
    `/scratch/` or `/tmp/` is found, with no dependency on its declared name.
    """
    service = {"volumes": ["some-brand-new-volume:/tmp/some-brand-new-path"]}
    targets = _named_volume_mount_targets(service, named_volumes={"some-brand-new-volume"})
    assert targets == {"/tmp/some-brand-new-path"}  # noqa: S108  # nosec B108


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
