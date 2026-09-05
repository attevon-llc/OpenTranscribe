#!/usr/bin/env python3
"""Pure functions for verifying published multi-arch manifest structure (issue #680).

No network, no Docker — every function takes a parsed `docker manifest inspect` (or
`docker buildx imagetools inspect --raw`) JSON document (or a list of them) and returns
a verdict. The CLI wrapper at the bottom is what 80-publish.sh and
scripts/tests/test-publish-platforms.sh both call, so the parsing/comparison logic has
exactly one implementation.

Three checks, matching issue #680's "suggested fix" #2 and this repo's tag grammar:

  leg_platform_set(manifest)      -> the set of "os/arch" platform strings a manifest
                                      declares. A LEG tag (repo:vX.Y.Z-<cap>-<arch>) must
                                      have exactly one.
  index_platform_set(manifest)    -> same, for an INDEX tag (repo:vX.Y.Z) — must equal
                                      the component's DECLARED platform set exactly:
                                      missing a platform is #680's original bug, an EXTRA
                                      platform is just as wrong (a leg published nobody
                                      asked for), so both directions are checked.
  equivalence_ratio(a, b)         -> (layer_count_equal, size_ratio) between two
                                      SINGLE-PLATFORM manifest entries of the SAME
                                      purpose/capability (e.g. lite-amd64 vs lite-arm64).
                                      Never call this across purposes (full vs lite,
                                      backend vs frontend) — there is no reason for their
                                      sizes to track and #680's whole point is that this
                                      comparison is only meaningful within one capability.

`architecture: "unknown"` platform entries are BuildKit attestation manifests
(provenance/SBOM), not real image platforms, and must be ignored by every function here
— counting them as a "platform" would make an index look like it declares more
architectures than it does.
"""

from __future__ import annotations

import json
import sys


def _manifests(doc: dict) -> list[dict]:
    """Normalize either a manifest-list doc or a single-manifest doc to a list of entries."""
    if 'manifests' in doc:
        return doc['manifests']
    return [doc]


def _is_real_platform(entry: dict) -> bool:
    platform = entry.get('platform', entry)
    arch = platform.get('architecture', '')
    # BuildKit attestation manifests declare architecture "unknown" — not a real
    # platform leg. See module docstring.
    return bool(arch) and arch != 'unknown'


def platform_set(doc: dict) -> set[str]:
    """The set of "os/arch" strings a manifest (list or single) actually declares,
    excluding attestation entries."""
    out = set()
    for entry in _manifests(doc):
        platform = entry.get('platform', entry)
        if not _is_real_platform(entry):
            continue
        os_ = platform.get('os', '')
        arch = platform.get('architecture', '')
        out.add(f'{os_}/{arch}')
    return out


def check_leg(doc: dict, expect_platform: str) -> tuple[bool, str]:
    """A LEG tag must declare EXACTLY ONE platform, and it must be the declared one."""
    platforms = platform_set(doc)
    if len(platforms) != 1:
        return (
            False,
            f'leg tag declares {len(platforms)} platform(s): {sorted(platforms)}; expected exactly 1',
        )
    (only,) = platforms
    if only != expect_platform:
        return False, f"leg tag declares platform '{only}'; expected '{expect_platform}'"
    return True, 'ok'


def check_index(doc: dict, expect_platforms: set[str]) -> tuple[bool, str]:
    """An INDEX tag must declare EXACTLY the declared platform set — no fewer
    (issue #680's original bug: a degraded/missing arch published anyway) and no
    more (a leg nobody declared)."""
    actual = platform_set(doc)
    missing = expect_platforms - actual
    extra = actual - expect_platforms
    if missing or extra:
        parts = []
        if missing:
            parts.append(f'missing: {sorted(missing)}')
        if extra:
            parts.append(f'extra: {sorted(extra)}')
        return False, '; '.join(parts)
    return True, 'ok'


def _entry_size_and_layers(doc: dict) -> tuple[int, int]:
    """Total size (bytes) and layer count for a SINGLE-PLATFORM manifest doc
    (i.e. the `docker manifest inspect <repo>:<tag>` of a leg tag, or a manifest
    entry that has already been resolved to its own layer list)."""
    layers = doc.get('layers') or doc.get('fsLayers') or []
    size = sum(int(entry.get('size', 0)) for entry in layers)
    if size == 0 and 'config' in doc:
        # imagetools --raw output nests size at layer level too; fall back to
        # summing manifest-level `Size` fields if present (docker manifest inspect -v shape).
        size = int(doc.get('size', 0))
    return size, len(layers)


def equivalence_ratio(a: dict, b: dict) -> tuple[bool, float, str]:
    """(layer_counts_equal, size_ratio >= 1, message) between two single-platform
    manifests of the SAME capability. size_ratio is always >= 1 (larger/smaller)."""
    a_size, a_layers = _entry_size_and_layers(a)
    b_size, b_layers = _entry_size_and_layers(b)

    layers_equal = a_layers == b_layers

    if a_size == 0 or b_size == 0:
        return (
            layers_equal,
            float('inf'),
            'one side reports zero total size — cannot compute a ratio',
        )

    ratio = max(a_size, b_size) / min(a_size, b_size)
    msg = f'layers a={a_layers} b={b_layers} equal={layers_equal}; sizes a={a_size} b={b_size} ratio={ratio:.2f}'
    return layers_equal, ratio, msg


def check_equivalence(a: dict, b: dict, bound: float) -> tuple[bool, str]:
    layers_equal, ratio, msg = equivalence_ratio(a, b)
    if not layers_equal:
        return False, f'layer count mismatch: {msg}'
    if ratio > bound:
        return False, f'size ratio {ratio:.2f} exceeds bound {bound}: {msg}'
    return True, msg


def _load(path: str) -> dict:
    if path == '-':
        return json.load(sys.stdin)
    with open(path) as f:
        return json.load(f)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(
            'usage: manifest_platform_check.py <check-leg|check-index|check-ratio> ...',
            file=sys.stderr,
        )
        return 2

    cmd = argv[1]
    if cmd == 'check-leg':
        # check-leg <manifest.json> <expect-platform>
        doc = _load(argv[2])
        ok, msg = check_leg(doc, argv[3])
        print(msg)
        return 0 if ok else 1

    if cmd == 'check-index':
        # check-index <manifest.json> <comma,separated,platforms>
        doc = _load(argv[2])
        expect = set(argv[3].split(','))
        ok, msg = check_index(doc, expect)
        print(msg)
        return 0 if ok else 1

    if cmd == 'check-ratio':
        # check-ratio <a.json> <b.json> <bound>
        a = _load(argv[2])
        b = _load(argv[3])
        bound = float(argv[4])
        ok, msg = check_equivalence(a, b, bound)
        print(msg)
        return 0 if ok else 1

    print(f'unknown command: {cmd}', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv))
