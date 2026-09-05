#!/usr/bin/env python3
"""Fixture manifest JSON generators for scripts/tests/test-publish-platforms.sh.

Sizes are the REAL measured numbers from issue #680 (backend v0.4.1 amd64/arm64) and
issue #667's plan pass (frontend amd64/arm64) — not invented — so assertions 3/4 are
checked against real, previously-observed data rather than a synthetic pair chosen to
pass.
"""

import json
import sys


def _layers(total_size: int, count: int) -> list[dict]:
    # Evenly split total_size across `count` layers — the ratio/layer-count check
    # only cares about the sum and the count, not per-layer sizes.
    base = total_size // count
    layers = [{'size': base} for _ in range(count)]
    layers[-1]['size'] += total_size - base * count
    return layers


def single_platform(os_: str, arch: str, total_size: int, layer_count: int) -> dict:
    return {
        'platform': {'os': os_, 'architecture': arch},
        'layers': _layers(total_size, layer_count),
    }


def image_manifest(total_size: int, layer_count: int) -> dict:
    """The REAL shape of `imagetools inspect repo@<digest> --raw`.

    Verified against the published davidamacey/opentranscribe-backend:v0.4.1 amd64
    manifest: keys are exactly config/layers/mediaType/schemaVersion — there is NO
    `platform` key (architecture lives in the config blob, which --raw does not
    fetch). This is what a size/layer comparison must be fed.
    """
    return {
        'schemaVersion': 2,
        'mediaType': 'application/vnd.oci.image.manifest.v1+json',
        'config': {'mediaType': 'application/vnd.oci.image.config.v1+json', 'size': 12345},
        'layers': _layers(total_size, layer_count),
    }


def index(platforms: list[dict], with_attestation: bool = False) -> dict:
    manifests = list(platforms)
    if with_attestation:
        manifests.append({'platform': {'os': 'unknown', 'architecture': 'unknown'}})
    return {'manifests': manifests}


def real_index(entries: list[tuple[str, str]], with_attestation: bool = True) -> dict:
    """The REAL shape of `imagetools inspect repo:tag --raw` on a published tag.

    Verified against opentranscribe-backend:v0.4.1: each entry carries
    digest/mediaType/platform/size, where `size` is the ~2.4 KB MANIFEST BLOB — there
    is no `layers` key anywhere. Feeding this document to check-ratio is what produced
    `sizes a=0 b=0 ratio=inf` before the resolve-digest step existed.
    """
    manifests = [
        {
            'mediaType': 'application/vnd.oci.image.manifest.v1+json',
            'digest': 'sha256:' + (arch.encode().hex() * 16)[:64],
            'size': 2393,
            'platform': {'os': os_, 'architecture': arch},
        }
        for os_, arch in entries
    ]
    if with_attestation:
        manifests.append(
            {
                'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                'digest': f'sha256:{"a" * 64}',
                'size': 564,
                'annotations': {'vnd.docker.reference.type': 'attestation-manifest'},
                'platform': {'os': 'unknown', 'architecture': 'unknown'},
            }
        )
    return {
        'schemaVersion': 2,
        'mediaType': 'application/vnd.oci.image.index.v1+json',
        'manifests': manifests,
    }


FIXTURES = {
    # Real v0.4.1 backend measurement (issue #680): amd64 4,454 MB total,
    # arm64 765 MB total, 11 layers each. Ratio = 4670571948 / 802291513 = 5.82.
    'backend-v041-amd64': single_platform('linux', 'amd64', 4_670_571_948, 11),
    'backend-v041-arm64': single_platform('linux', 'arm64', 802_291_513, 11),
    # Real frontend measurement (issue #667's plan pass): near-identical, 12 layers.
    'frontend-amd64': single_platform('linux', 'amd64', 50_205_619, 12),
    'frontend-arm64': single_platform('linux', 'arm64', 50_037_923, 12),
    # A well-formed lite index: exactly amd64+arm64, no attestation noise stripped yet.
    'lite-index-correct': index(
        [
            {'platform': {'os': 'linux', 'architecture': 'amd64'}},
            {'platform': {'os': 'linux', 'architecture': 'arm64'}},
        ]
    ),
    'lite-index-missing-arm64': index([{'platform': {'os': 'linux', 'architecture': 'amd64'}}]),
    'lite-index-extra-riscv64': index(
        [
            {'platform': {'os': 'linux', 'architecture': 'amd64'}},
            {'platform': {'os': 'linux', 'architecture': 'arm64'}},
            {'platform': {'os': 'linux', 'architecture': 'riscv64'}},
        ]
    ),
    'lite-index-with-attestation': index(
        [
            {'platform': {'os': 'linux', 'architecture': 'amd64'}},
            {'platform': {'os': 'linux', 'architecture': 'arm64'}},
        ],
        with_attestation=True,
    ),
    'backend-leg-amd64-only': single_platform('linux', 'amd64', 100, 1),
    'backend-leg-two-platforms': index(
        [
            {'platform': {'os': 'linux', 'architecture': 'amd64'}},
            {'platform': {'os': 'linux', 'architecture': 'arm64'}},
        ]
    ),
    # --- REAL document shapes (see the two builders above) ---------------------
    # The published index: declares platforms, carries NO layers. Feeding this to
    # check-ratio must be CANNOT CHECK, never a ratio verdict.
    'real-index-amd64-arm64': real_index([('linux', 'amd64'), ('linux', 'arm64')]),
    # The per-platform manifests the index points at: real v0.4.1 measurements,
    # carrying layers and NO platform key.
    'real-manifest-amd64': image_manifest(4_670_571_948, 11),
    'real-manifest-arm64': image_manifest(802_291_513, 11),
    # A near-equivalent pair, the shape a healthy lite/frontend index resolves to.
    'real-manifest-cpu-a': image_manifest(50_205_619, 12),
    'real-manifest-cpu-b': image_manifest(50_037_923, 12),
    # The CONFIG BLOB, i.e. `imagetools inspect <ref> --format '{{json .Image}}'`.
    # This is the ONLY place a bare (attestation-less) leg tag states its platform —
    # verified 2026-09-05 against the real published davidamacey/diar-native:0.3.1-cpu,
    # which reports os=linux architecture=amd64 there while its --raw declares none.
    'image-config-amd64': {
        'os': 'linux',
        'architecture': 'amd64',
        'rootfs': {'type': 'layers'},
    },
    'image-config-arm64': {
        'os': 'linux',
        'architecture': 'arm64',
        'rootfs': {'type': 'layers'},
    },
    # A config blob with no platform fields at all — must be CANNOT CHECK, never a pass.
    'image-config-empty': {'rootfs': {'type': 'layers'}},
}


def main() -> int:
    name = sys.argv[1]
    json.dump(FIXTURES[name], sys.stdout)
    return 0


if __name__ == '__main__':
    sys.exit(main())
