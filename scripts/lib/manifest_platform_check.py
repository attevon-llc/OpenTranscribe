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

TWO DOCUMENT SHAPES, AND THEY CARRY DIFFERENT FACTS. `docker buildx imagetools inspect
--raw` returns either:

  * an INDEX  — `{"manifests": [{"platform": {...}, "digest": ..., "size": ...}, ...]}`.
    It declares platforms. It carries NO `layers`, and its per-entry `size` is the size
    of the manifest BLOB (~2 KB), not of the image.
  * a MANIFEST — `{"config": {...}, "layers": [...]}`. It carries layers and therefore a
    real image size. It declares NO platform (that lives in the config blob).

So a size/layer comparison MUST be fed the per-platform manifest (resolve its digest out
of the index with `resolve-digest`, then inspect `repo@<digest>`), never the index. An
earlier revision fed the index straight into `check-ratio`; measured against the real
published `opentranscribe-backend:v0.4.1` index that reports `sizes a=0 b=0 ratio=inf`
and fails EVERY time, even comparing an index against itself.

EXIT CODES ARE THREE-VALUED ON PURPOSE. This repo's standing rule (issue #681) is that
"checked, result tolerable" and "never checked" must be different outcomes; the same
applies here. 0 = verified, 1 = verified and WRONG, 3 = could not check (malformed JSON,
a shape that carries none of the facts asked for). Both 1 and 3 are failures to the
caller — but only 1 is a statement about the artifact.
"""

from __future__ import annotations

import json
import sys

EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_MISUSE = 2
EXIT_CANNOT_CHECK = 3


class CannotCheck(Exception):
    """The document does not carry the fact being asked for.

    Distinct from "the fact is wrong": raised for a malformed/unreadable document, or for
    a correct document of the wrong SHAPE (an index has no layers; a single manifest has
    no platform). Never swallowed into a verdict — see the module docstring.
    """


def _manifests(doc: dict) -> list[dict]:
    """Normalize either a manifest-list doc or a single-manifest doc to a list of entries."""
    if 'manifests' in doc:
        return doc['manifests']
    return [doc]


def _require_platform_bearing(doc: dict) -> None:
    """Raise CannotCheck unless `doc` is a shape that DECLARES platforms.

    An index does (`manifests[].platform`). A bare image manifest does not — its
    architecture lives in the config blob, which `--raw` does not fetch — so reporting
    "declares 0 platforms" for one would be a verdict drawn from absent data.
    """
    if 'manifests' in doc:
        return
    if isinstance(doc.get('platform'), dict):
        return
    raise CannotCheck(
        'document declares no platform information: it is a single image manifest '
        '(keys: '
        + ', '.join(sorted(doc)[:6])
        + '), whose architecture lives in the config blob. Re-inspect the tag on a '
        'builder that pushes provenance (so the tag resolves to an index), or read '
        "the platform from `imagetools inspect --format '{{json .Image}}'`."
    )


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


def resolve_digest(doc: dict, platform: str) -> str:
    """The manifest digest an INDEX lists for `platform` ("linux/amd64").

    This is the step that turns "what the index declares" into "the manifest that
    actually has layers", so a size/layer comparison is fed the right document. Raises
    CannotCheck for a non-index doc; returns '' when the index simply has no such
    platform (a mismatch, which check_index reports properly).
    """
    if 'manifests' not in doc:
        raise CannotCheck(
            'cannot resolve a per-platform digest: this document is not a manifest '
            'index (no "manifests" key), so it lists no per-platform manifests'
        )
    for entry in doc['manifests']:
        if not _is_real_platform(entry):
            continue
        p = entry.get('platform', {})
        if f'{p.get("os", "")}/{p.get("architecture", "")}' == platform:
            digest = entry.get('digest', '')
            if not digest:
                raise CannotCheck(f'index entry for {platform} carries no digest')
            return str(digest)
    return ''


def check_image_config(doc: dict, expect_platform: str) -> tuple[bool, str]:
    """Verify a leg's platform from its CONFIG BLOB rather than its manifest.

    This is the path for a leg tag that resolves to a bare image manifest instead of an
    index — which is not hypothetical: MEASURED 2026-09-05 against the real published
    `davidamacey/diar-native:0.3.1-cpu`, whose `--raw` is
    `{config, layers, mediaType, schemaVersion}` with no platform anywhere, so
    `check-leg` on it can only ever answer CANNOT CHECK. Whether a leg lands in that
    shape depends on whether the push carried provenance attestations, which is a
    builder setting — so check (a) must not be permanently inconclusive whenever it is
    off. A check that can never pass is not a gate.

    Input is `docker buildx imagetools inspect <ref> --format '{{json .Image}}'`, i.e.
    the config blob: `{"os": "linux", "architecture": "amd64", ...}`. imagetools
    fetches it without pulling the image.
    """
    os_ = doc.get('os', '')
    arch = doc.get('architecture', '')
    if not os_ or not arch:
        raise CannotCheck(
            'image config declares no os/architecture (keys: '
            + ', '.join(sorted(doc)[:6])
            + ") — expected the output of `imagetools inspect --format '{{json .Image}}'`"
        )
    actual = f'{os_}/{arch}'
    if actual != expect_platform:
        return False, f"image config declares platform '{actual}'; expected '{expect_platform}'"
    return True, f'ok (from image config: {actual})'


def check_leg(doc: dict, expect_platform: str) -> tuple[bool, str]:
    """A LEG tag must declare EXACTLY ONE platform, and it must be the declared one."""
    _require_platform_bearing(doc)
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
    _require_platform_bearing(doc)
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
    """Total size (bytes) and layer count for a SINGLE-PLATFORM image manifest.

    Raises CannotCheck for any other shape. In particular an INDEX has no `layers` at
    all, so treating a missing key as "0 bytes, 0 layers" would manufacture a verdict —
    `0 == 0` even makes the layer-count half of the comparison PASS on no data. Resolve
    the per-platform digest first (`resolve-digest`) and inspect that.
    """
    if 'layers' not in doc and 'fsLayers' not in doc:
        raise CannotCheck(
            'document carries no layer list, so it has no image size to compare '
            '(keys: '
            + ', '.join(sorted(doc)[:6])
            + '). A manifest INDEX never does — resolve the per-platform digest with '
            '`resolve-digest` and inspect repo@<digest> instead.'
        )
    layers = doc.get('layers') or doc.get('fsLayers') or []
    if not layers:
        raise CannotCheck('document has an EMPTY layer list — nothing to compare')
    return sum(int(entry.get('size', 0)) for entry in layers), len(layers)


def equivalence_ratio(a: dict, b: dict) -> tuple[bool, float, str]:
    """(layer_counts_equal, size_ratio >= 1, message) between two single-platform
    manifests of the SAME capability. size_ratio is always >= 1 (larger/smaller)."""
    a_size, a_layers = _entry_size_and_layers(a)
    b_size, b_layers = _entry_size_and_layers(b)

    layers_equal = a_layers == b_layers

    if a_size == 0 or b_size == 0:
        raise CannotCheck(
            f'a side reports zero total size (a={a_size} b={b_size}) — the manifest '
            'lists layers but none of them declare a size, so no ratio exists'
        )

    ratio = max(a_size, b_size) / min(a_size, b_size)
    msg = (
        f'layers a={a_layers} b={b_layers} equal={layers_equal}; '
        f'sizes a={a_size} b={b_size} ratio={ratio:.2f}'
    )
    return layers_equal, ratio, msg


def check_equivalence(a: dict, b: dict, bound: float) -> tuple[bool, str]:
    layers_equal, ratio, msg = equivalence_ratio(a, b)
    if not layers_equal:
        return False, f'layer count mismatch: {msg}'
    if ratio > bound:
        return False, f'size ratio {ratio:.2f} exceeds bound {bound}: {msg}'
    return True, msg


def _load(path: str) -> dict:
    """Read a manifest document, mapping every unreadable/unparseable state to
    CannotCheck so it exits 3 (could not check) rather than 1 (checked, wrong).

    An uncaught JSONDecodeError would exit 1 and be indistinguishable, in the caller's
    log, from a genuine platform mismatch.
    """
    try:
        if path in ('-', '/dev/stdin'):
            raw = sys.stdin.read()
        else:
            with open(path) as handle:
                raw = handle.read()
    except OSError as exc:
        raise CannotCheck(f'cannot read {path}: {exc}') from exc
    if not raw.strip():
        raise CannotCheck(f'{path} is empty — nothing was inspected')
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise CannotCheck(f'{path} is not valid JSON: {exc}') from exc
    if not isinstance(doc, dict):
        raise CannotCheck(f'{path} is not a manifest object (got {type(doc).__name__})')
    return doc


def _dispatch(argv: list[str]) -> int:
    cmd = argv[1]
    if cmd == 'check-leg':
        # check-leg <manifest.json> <expect-platform>
        ok, msg = check_leg(_load(argv[2]), argv[3])
        print(msg)
        return EXIT_OK if ok else EXIT_MISMATCH

    if cmd == 'check-index':
        # check-index <manifest.json> <comma,separated,platforms>
        ok, msg = check_index(_load(argv[2]), set(argv[3].split(',')))
        print(msg)
        return EXIT_OK if ok else EXIT_MISMATCH

    if cmd == 'check-ratio':
        # check-ratio <a.json> <b.json> <bound>
        ok, msg = check_equivalence(_load(argv[2]), _load(argv[3]), float(argv[4]))
        print(msg)
        return EXIT_OK if ok else EXIT_MISMATCH

    if cmd == 'check-image-config':
        # check-image-config <image-config.json> <expect-platform>
        ok, msg = check_image_config(_load(argv[2]), argv[3])
        print(msg)
        return EXIT_OK if ok else EXIT_MISMATCH

    if cmd == 'resolve-digest':
        # resolve-digest <index.json> <platform>  -> digest on stdout
        digest = resolve_digest(_load(argv[2]), argv[3])
        if not digest:
            print(f'index declares no {argv[3]} manifest')
            return EXIT_MISMATCH
        print(digest)
        return EXIT_OK

    print(f'unknown command: {cmd}', file=sys.stderr)
    return EXIT_MISUSE


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(
            'usage: manifest_platform_check.py <check-leg|check-index|check-ratio'
            '|check-image-config|resolve-digest> ...',
            file=sys.stderr,
        )
        return EXIT_MISUSE
    try:
        return _dispatch(argv)
    except IndexError:
        print(f'missing argument(s) for {argv[1]}', file=sys.stderr)
        return EXIT_MISUSE
    except CannotCheck as exc:
        # NOT exit 1: the caller must be able to tell "this artifact is wrong" from
        # "nothing was actually checked". See the module docstring.
        print(f'CANNOT CHECK: {exc}')
        return EXIT_CANNOT_CHECK


if __name__ == '__main__':
    sys.exit(main(sys.argv))
