"""Issue #886: the release SBOM must attribute the project's licence to the component it
describes, and `scripts/lib/sbom_license.py` is what puts it there.

WHY A SEPARATE FILE FROM `test_first_party_license_declarations.py`. That one gates the
DECLARATION — every manifest and every production Dockerfile says the same SPDX id. This one
gates the DELIVERY — that the declaration survives into CycloneDX
`metadata.component.licenses`, which is the field a generic SBOM consumer reads and which
syft cannot populate itself (`syft config` offers source name/version/supplier and no licence).

The fixtures below are shaped like the real thing, because the real thing is where the traps
are: syft records image labels as `metadata.properties` entries named
`syft:image:labels:<label>` (measured, syft 1.33.0), and `metadata.component` also carries the
`name`/`version` that `scripts/release/release-assets.sh`'s
`release_assets_sbom_matches_version()` reads — the criterion 95-finish.sh gates a release on.
A promotion that disturbed either would fail every SBOM of every real release, so the
identity-preservation guard has its own test rather than being trusted to be narrow.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
HELPER_PATH = REPO_ROOT / "scripts" / "lib" / "sbom_license.py"
RELEASE_ASSETS_SH = REPO_ROOT / "scripts" / "release" / "release-assets.sh"


def _load_helper() -> Any:
    """Import `scripts/lib/sbom_license.py` by path — it is a script, not a package member."""
    spec = importlib.util.spec_from_file_location("sbom_license", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sbom_license = _load_helper()


def _syft_shaped_sbom(label_value: str | None) -> dict[str, Any]:
    """Build a CycloneDX document shaped the way syft 1.33.0 writes one.

    Args:
        label_value: The value to record for the image's OCI licence annotation, or None to
            simulate an image that declares none.

    Returns:
        A parsed-CycloneDX-shaped dict.
    """
    properties = [{"name": "syft:image:labels:org.opencontainers.image.title", "value": "x"}]
    if label_value is not None:
        properties.append(
            {
                "name": "syft:image:labels:org.opencontainers.image.licenses",
                "value": label_value,
            }
        )
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "metadata": {
            "component": {
                "bom-ref": "ae09cc7bad98c714",
                "type": "container",
                "name": "davidamacey/opentranscribe-backend",
                "version": "v0.5.0",
            },
            "properties": properties,
        },
        "components": [{"type": "library", "name": "musl", "version": "1.2.5-r3"}],
    }


def test_promotes_the_image_label_into_metadata_component_licenses() -> None:
    """The happy path: the label lands in the canonical CycloneDX field, in syft's own form."""
    doc = _syft_shaped_sbom("AGPL-3.0-only")

    promoted = sbom_license.promote_license(doc)

    assert promoted == "AGPL-3.0-only"
    assert doc["metadata"]["component"]["licenses"] == [{"license": {"id": "AGPL-3.0-only"}}]
    assert sbom_license.declared_component_license(doc) == "AGPL-3.0-only"


def test_promotes_whatever_the_image_declared_rather_than_a_hardcoded_identifier() -> None:
    """Guards against the helper carrying its own copy of the SPDX id.

    The image `LABEL` is the single source of truth; a helper that wrote "AGPL-3.0-only"
    regardless would pass the test above while being incapable of reporting a relicense.
    """
    doc = _syft_shaped_sbom("Apache-2.0")

    assert sbom_license.promote_license(doc) == "Apache-2.0"
    assert doc["metadata"]["component"]["licenses"] == [{"license": {"id": "Apache-2.0"}}]


def test_an_image_with_no_licence_label_gets_no_invented_licence() -> None:
    """No label means no attribution — never a default. A wrong licence is worse than none."""
    doc = _syft_shaped_sbom(None)

    assert sbom_license.image_license_label(doc) is None
    assert sbom_license.promote_license(doc) is None
    assert "licenses" not in doc["metadata"]["component"]


def test_promotion_leaves_the_release_gate_identity_fields_untouched() -> None:
    """`metadata.component.name`/`version` are what 95-finish.sh gates a release on."""
    doc = _syft_shaped_sbom("AGPL-3.0-only")
    before = copy.deepcopy(doc)

    sbom_license.promote_license(doc)

    assert doc["metadata"]["component"]["name"] == before["metadata"]["component"]["name"]
    assert doc["metadata"]["component"]["version"] == before["metadata"]["component"]["version"]
    assert doc["components"] == before["components"]
    assert doc["metadata"]["properties"] == before["metadata"]["properties"]


def test_identity_guard_refuses_to_write_when_name_or_version_moved() -> None:
    """The guard must actually fire — an assertion nothing can trip is not a guard."""
    before = _syft_shaped_sbom("AGPL-3.0-only")

    moved_version = copy.deepcopy(before)
    moved_version["metadata"]["component"]["version"] = "v0.5.0-scanleg-amd64"
    with pytest.raises(ValueError, match="metadata.component.version changed"):
        sbom_license._assert_identity_preserved(before, moved_version)

    moved_name = copy.deepcopy(before)
    moved_name["metadata"]["component"]["name"] = "something-else"
    with pytest.raises(ValueError, match="metadata.component.name changed"):
        sbom_license._assert_identity_preserved(before, moved_name)


def test_promote_file_rewrites_atomically_and_is_idempotent(tmp_path: Path) -> None:
    """Two runs over the same file produce one licence entry, not two, and valid JSON."""
    sbom_path = tmp_path / "backend-amd64-sbom.json"
    sbom_path.write_text(json.dumps(_syft_shaped_sbom("AGPL-3.0-only")), encoding="utf-8")

    assert sbom_license.promote_file(str(sbom_path)) == "AGPL-3.0-only"
    assert sbom_license.promote_file(str(sbom_path)) == "AGPL-3.0-only"

    doc = json.loads(sbom_path.read_text(encoding="utf-8"))
    assert doc["metadata"]["component"]["licenses"] == [{"license": {"id": "AGPL-3.0-only"}}]
    assert not list(tmp_path.glob(".sbom-license-*")), "temp file left behind"


def test_cli_exit_codes_distinguish_no_label_from_error(tmp_path: Path) -> None:
    """3 = the image said nothing; 1 = we could not do the job. Never the same branch.

    Same rule as `security-scan.sh`'s 1-vs-2 split (issue #681): "checked, nothing to report"
    and "could not check" are different outcomes, and `generate_sbom()` only warns on the
    former.
    """
    labelled = tmp_path / "ok.json"
    labelled.write_text(json.dumps(_syft_shaped_sbom("AGPL-3.0-only")), encoding="utf-8")
    unlabelled = tmp_path / "no-label.json"
    unlabelled.write_text(json.dumps(_syft_shaped_sbom(None)), encoding="utf-8")
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")

    def _run(path: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(HELPER_PATH), "promote", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )

    assert _run(labelled).returncode == sbom_license.EXIT_OK
    assert _run(labelled).stdout.strip() == "AGPL-3.0-only"
    assert _run(unlabelled).returncode == sbom_license.EXIT_NO_LABEL
    assert _run(corrupt).returncode == sbom_license.EXIT_ERROR
    assert _run(tmp_path / "absent.json").returncode == sbom_license.EXIT_ERROR


def test_promoted_sbom_still_satisfies_the_release_version_criterion(tmp_path: Path) -> None:
    """End to end against the REAL release gate function, not a re-implementation of it.

    `release_assets_sbom_matches_version()` is what 95-finish.sh's
    `sbom-describes-this-version` criterion calls. Running the real bash function over a
    promoted document is the only check that proves this change cannot block a release; a
    Python restatement of its logic would pass against a function that had since diverged.
    """
    sbom_path = tmp_path / "backend-amd64-sbom.json"
    sbom_path.write_text(json.dumps(_syft_shaped_sbom("AGPL-3.0-only")), encoding="utf-8")
    sbom_license.promote_file(str(sbom_path))

    def _matches(version: str) -> int:
        return subprocess.run(
            [
                "bash",
                "-c",
                f'source "{RELEASE_ASSETS_SH}"; '
                f'release_assets_sbom_matches_version "{sbom_path}" "{version}"',
            ],
            capture_output=True,
            text=True,
            check=False,
        ).returncode

    assert _matches("v0.5.0") == 0, "promotion broke the release SBOM version criterion"
    assert _matches("v0.4.1") == 1, "the criterion would pass on any version — it proves nothing"
