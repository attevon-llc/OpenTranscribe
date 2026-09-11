"""Issue #886: no package manifest declared the project's licence, so an SBOM generated
from these manifests attributed no licence to the first-party components.

Verified 2026-09-09 sweep, re-verified here: the repo root carries a full AGPL-3.0 `LICENSE`
(GNU AFFERO GENERAL PUBLIC LICENSE, Version 3) and `README.md` states "GNU Affero General
Public License v3.0 (AGPL-3.0)" with no "or later" qualifier — so the SPDX identifier is
``AGPL-3.0-only``, not ``AGPL-3.0-or-later``. Before this fix, `rg -in "license"` returned
zero matches in every first-party `package.json`/`pyproject.toml` in the tree.

⚠️ **What this test does NOT prove.** Manually forcing syft's `javascript-package-cataloger`
onto a directory scan (`syft dir:frontend --select-catalogers +javascript-package-cataloger`)
confirms it reads a `package.json`'s own `license` field correctly. But that cataloger's
default tag scope is `image, installed`, and — measured directly — neither
`frontend/Dockerfile.prod` nor `docs-site/Dockerfile` copies `package.json` into their final
`nginx:*-alpine` stage (only the built static output is copied), so the manifest this test
checks never reaches the image `scripts/security-scan.sh generate_sbom()` actually scans for
those two components. On the Python side, syft's `python-package-cataloger` /
`python-installed-package-cataloger` were measured (with a control POETRY project carrying
both `pyproject.toml` and `poetry.lock`) to catalog exactly ZERO self-describing components
from a source-tree `pyproject.toml` of any flavour — that cataloger enumerates declared/
installed third-party dependencies, not the project's own metadata, so `backend/pyproject.toml`
declaring a licence does not yet make it appear in an image-scanned SBOM either. Making the
SBOM tool see it would need backend to be an actually-`pip install`ed package producing real
`dist-info` metadata in the image — a build-pipeline change out of this fix's scope. This test
therefore only pins the parity acceptance criterion (#886's #1/#2): every first-party manifest
declares the SAME identifier, and a new one added without it fails here.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The SPDX identifier every first-party manifest must declare, derived from LICENSE +
#: README.md (see module docstring), not asserted from thin air.
EXPECTED_SPDX_ID = "AGPL-3.0-only"

#: Directories that hold third-party/vendored files, never first-party manifests.
_EXCLUDED_DIR_NAMES = frozenset(
    {"node_modules", "venv", ".venv", ".svelte-kit", "dist", "build", ".git"}
)


def _is_excluded(path: Path) -> bool:
    return any(part in _EXCLUDED_DIR_NAMES for part in path.parts)


def _first_party_package_jsons() -> list[Path]:
    return sorted(
        p for p in REPO_ROOT.rglob("package.json") if not _is_excluded(p.relative_to(REPO_ROOT))
    )


def _first_party_pyproject_tomls() -> list[Path]:
    return sorted(
        p for p in REPO_ROOT.rglob("pyproject.toml") if not _is_excluded(p.relative_to(REPO_ROOT))
    )


def test_expected_manifest_set_is_what_the_886_sweep_found() -> None:
    """Guards the guard: if new first-party manifests appear, this sees them too.

    Pins the exact set the 2026-09-09 sweep enumerated, so a manifest silently added
    outside that set cannot be missed by a scanner whose glob quietly matched nothing.
    """
    package_jsons = {str(p.relative_to(REPO_ROOT)) for p in _first_party_package_jsons()}
    pyproject_tomls = {str(p.relative_to(REPO_ROOT)) for p in _first_party_pyproject_tomls()}

    assert package_jsons == {
        "package.json",
        "docs-site/package.json",
        "frontend/package.json",
        "frontend/ffmpeg-wasm-build/test/package.json",
    }
    assert pyproject_tomls == {"pyproject.toml", "backend/pyproject.toml"}


def test_every_first_party_package_json_declares_the_licence() -> None:
    """A `package.json` missing `license`, or declaring a different SPDX id, fails here."""
    offenders: list[str] = []
    for path in _first_party_package_jsons():
        manifest = json.loads(path.read_text(encoding="utf-8"))
        license_id = manifest.get("license")
        if license_id != EXPECTED_SPDX_ID:
            offenders.append(f"{path.relative_to(REPO_ROOT)}: license={license_id!r}")

    assert not offenders, (
        "every first-party package.json must declare "
        f"license={EXPECTED_SPDX_ID!r} — offenders: {offenders}"
    )


def test_every_first_party_pyproject_declares_the_licence() -> None:
    """A `pyproject.toml` missing `[project].license`, or a different SPDX id, fails here."""
    offenders: list[str] = []
    for path in _first_party_pyproject_tomls():
        with path.open("rb") as fh:
            manifest = tomllib.load(fh)
        license_id = manifest.get("project", {}).get("license")
        if license_id != EXPECTED_SPDX_ID:
            offenders.append(f"{path.relative_to(REPO_ROOT)}: license={license_id!r}")

    assert not offenders, (
        "every first-party pyproject.toml [project] table must declare "
        f"license={EXPECTED_SPDX_ID!r} — offenders: {offenders}"
    )
