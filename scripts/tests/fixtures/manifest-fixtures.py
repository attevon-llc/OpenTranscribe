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


def index(platforms: list[dict], with_attestation: bool = False) -> dict:
    manifests = list(platforms)
    if with_attestation:
        manifests.append({'platform': {'os': 'unknown', 'architecture': 'unknown'}})
    return {'manifests': manifests}


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
}


def main() -> int:
    name = sys.argv[1]
    json.dump(FIXTURES[name], sys.stdout)
    return 0


if __name__ == '__main__':
    sys.exit(main())
