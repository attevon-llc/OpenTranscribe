#!/usr/bin/env python3
"""Promote an image's OCI licence annotation into CycloneDX `metadata.component.licenses`
(issue #886).

WHY THIS EXISTS — three facts, each measured against syft 1.33.0 rather than assumed:

1. **The final `nginx:*-alpine` stages of `frontend/Dockerfile.prod` and
   `docs-site/Dockerfile` contain ZERO `package.json` files.** Verified by
   `find / -name package.json` inside the published `davidamacey/opentranscribe-frontend:v0.5.0`
   and `-docs:v0.5.0` images: count 0 in both. Only the built static output and `nginx.conf`
   are copied. So the `license` field those manifests now declare cannot reach an image scan.

2. **syft catalogues NOTHING from a `pyproject.toml`.** Control experiment: a `pyproject.toml`
   with `[project] name/version/license` scanned both as `dir:` and inside an image produced
   an EMPTY artifact list either way. (The npm control is the opposite — a bare `package.json`
   in an image IS catalogued, licence and all.) So the backend/lite/blackwell images could not
   be fixed by shipping their manifest even if we wanted to: syft's python catalogers read
   `*.dist-info`/`*.egg-info`/`requirements.txt`/lockfiles, never the project's own metadata.
   `backend/pyproject.toml` is not in the image at all today, which makes it moot twice over.

   Together, 1 and 2 rule out "ship the manifest into the image" as a mechanism: it fixes 2 of
   5 components, does nothing for the other 3, and would inject a phantom npm *component* into
   the SBOM for grype to CVE-match against.

3. **syft DOES surface image labels**, as `metadata.properties` entries named
   `syft:image:labels:<label>`. That makes `LABEL org.opencontainers.image.licenses` — the
   canonical, tool-agnostic OCI annotation, which registries, Trivy and dockle also read —
   the one mechanism that works uniformly for all five production images.

   But `syft:image:*` is a **vendor extension**. A generic CycloneDX consumer reads
   `metadata.component.licenses`, and syft has no way to populate it: `syft config` exposes
   `source.name` / `source.version` / `source.supplier` and no licence field. That last gap is
   what this script closes, and nothing else.

WHAT IT DOES NOT DO. It never invents a licence. The value is read back out of the very
document being amended, where syft put it after reading it off the image — so the image's
`LABEL` is the single source of truth and there is no second place to type the identifier.
No label in the image means no promotion (exit 3), never a default.

⚠️ `metadata.component` is the object `scripts/release/release-assets.sh`'s
`release_assets_sbom_matches_version()` reads `name`/`version` out of, and 95-finish.sh's
`sbom-describes-this-version` criterion gates a release on it. This script adds a `licenses`
key beside them and **mutates neither** — asserted by `_assert_identity_preserved()` before
anything is written, because a silent edit there fails every SBOM of every real release.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from typing import Any

#: The OCI annotation that declares an image's licence, and the syft property name that
#: carries it through into CycloneDX output.
OCI_LICENSE_LABEL = 'org.opencontainers.image.licenses'
SYFT_LABEL_PROPERTY = f'syft:image:labels:{OCI_LICENSE_LABEL}'

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NO_LABEL = 3


class LicenseLabelMissing(ValueError):
    """The scanned image declared no `org.opencontainers.image.licenses` label.

    Deliberately its own type, and deliberately NOT the type raised when the identity
    check trips: "the image said nothing" (exit 3) and "the rewrite corrupted the field a
    release gate reads" (exit 1) are different outcomes and must not share a branch.
    """


def image_license_label(doc: dict[str, Any]) -> str | None:
    """Return the licence syft read off the image's OCI label, or None if it declared none.

    Args:
        doc: A parsed CycloneDX-JSON document as syft writes it.

    Returns:
        The label's value with surrounding whitespace stripped, or None when the property
        is absent or empty.
    """
    properties = (doc.get('metadata') or {}).get('properties') or []
    if not isinstance(properties, list):
        return None
    for prop in properties:
        if isinstance(prop, dict) and prop.get('name') == SYFT_LABEL_PROPERTY:
            value = str(prop.get('value') or '').strip()
            return value or None
    return None


def declared_component_license(doc: dict[str, Any]) -> str | None:
    """Return the SPDX id already on `metadata.component.licenses`, if any.

    Args:
        doc: A parsed CycloneDX-JSON document.

    Returns:
        The first `licenses[].license.id`, or None when the field is absent or not an id.
    """
    licenses = ((doc.get('metadata') or {}).get('component') or {}).get('licenses') or []
    if not isinstance(licenses, list):
        return None
    for entry in licenses:
        if isinstance(entry, dict):
            license_id = (entry.get('license') or {}).get('id')
            if license_id:
                return str(license_id)
    return None


def promote_license(doc: dict[str, Any]) -> str | None:
    """Copy the image's licence label onto `metadata.component.licenses`, in place.

    Uses CycloneDX's `{"license": {"id": ...}}` form — the same encoding syft itself emits
    for a component whose declared licence is a known SPDX identifier — rather than the
    `{"expression": ...}` form, which is for compound expressions.

    Args:
        doc: A parsed CycloneDX-JSON document. Mutated in place when a label is present.

    Returns:
        The promoted licence id, or None when the image declared no licence label.
    """
    license_id = image_license_label(doc)
    if license_id is None:
        return None

    component = (doc.setdefault('metadata', {})).setdefault('component', {})
    component['licenses'] = [{'license': {'id': license_id}}]
    return license_id


def _assert_identity_preserved(before: dict[str, Any], after: dict[str, Any]) -> None:
    """Raise unless `metadata.component`'s name/version survived the promotion untouched.

    Those two fields are the release pipeline's `sbom-describes-this-version` gate. A
    promotion that moved either of them would fail every release SBOM, so this refuses to
    write rather than trusting the edit above to have been narrow.

    Args:
        before: The document as read from disk.
        after: The document about to be written.

    Raises:
        ValueError: If either field differs.
    """
    for field in ('name', 'version'):
        was = ((before.get('metadata') or {}).get('component') or {}).get(field)
        now = ((after.get('metadata') or {}).get('component') or {}).get(field)
        if was != now:
            raise ValueError(
                f'refusing to write: metadata.component.{field} changed {was!r} -> {now!r}'
            )


def promote_file(path: str) -> str:
    """Promote the licence label in the SBOM at `path`, writing atomically.

    The rewrite goes to a temp file in the same directory and is `os.replace`d over the
    original only after it has been re-parsed as JSON, so a crash mid-write can never leave
    a truncated SBOM where the release pipeline expects a readable one.

    Args:
        path: Path to a CycloneDX-JSON SBOM.

    Returns:
        The promoted licence id.

    Raises:
        ValueError: If the image declared no licence label, or the identity check failed.
        OSError, json.JSONDecodeError: On unreadable or malformed input.
    """
    with open(path, encoding='utf-8') as handle:
        original = json.load(handle)

    amended = json.loads(json.dumps(original))
    license_id = promote_license(amended)
    if license_id is None:
        raise LicenseLabelMissing(f'no {OCI_LICENSE_LABEL} label recorded in {path}')
    _assert_identity_preserved(original, amended)

    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix='.sbom-license-', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(amended, handle, indent=2)
            handle.write('\n')
        with open(tmp_path, encoding='utf-8') as handle:
            json.load(handle)
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    return license_id


def main(argv: list[str]) -> int:
    """CLI entry point. Usage: ``sbom_license.py promote <sbom.json>``."""
    if len(argv) != 3 or argv[1] != 'promote':
        print(f'usage: {argv[0]} promote <sbom.json>', file=sys.stderr)
        return EXIT_ERROR
    try:
        license_id = promote_file(argv[2])
    except LicenseLabelMissing as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_NO_LABEL
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f'could not amend {argv[2]}: {exc}', file=sys.stderr)
        return EXIT_ERROR
    print(license_id)
    return EXIT_OK


if __name__ == '__main__':
    sys.exit(main(sys.argv))
