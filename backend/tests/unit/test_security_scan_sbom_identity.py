"""The SBOM `security-scan.sh` writes must name the RELEASE TAG, not the scan alias.

THE BUG THIS PINS

`resolve_platform_image()` cannot hand `generate_sbom` the plain `repo:vX.Y.Z` reference. Two
legs of one version have to coexist in the local daemon or the second `docker pull` overwrites
the first and both scans examine the same image, so it re-tags each leg
`repo:vX.Y.Z-scanleg-<arch>` and returns THAT.

Syft derives `metadata.component.version` from the reference it is given. So an unqualified
`syft "$image"` wrote::

    metadata.component.version = "v0.5.0-scanleg-amd64"

and `95-finish.sh`'s `sbom-describes-this-version` criterion — `release-assets.sh`'s
`release_assets_sbom_matches_version()`, an EXACT string comparison against `v0.5.0` — would
have failed every SBOM on every real release, in the last stage of the pipeline, after the
images were already published and `:latest` already moved.

MEASURED with syft 1.33.0 against a local image tagged `repo:v0.5.0-scanleg-amd64`: the bare
invocation reports `v0.5.0-scanleg-amd64`; adding `--source-version v0.5.0` reports `v0.5.0`.

WHY `test_release_finish_assets.py` COULD NOT SEE IT

That file synthesises its SBOM fixtures with `_sbom_json(name, version)` — it hands the gate
documents that already carry the plain version, so it exercises the READER and never the
WRITER. The defect lives entirely in what the writer puts in the field.

APPROACH — close the loop, don't grep

A grep for `--source-version` would pass against a version that names the flag and passes the
wrong value (the scan alias is right there in `$image`, and passing it would look correct).
So this file runs the REAL `generate_sbom()` extracted from the shipped script against a fake
`syft` that reproduces the one behaviour that matters — version comes from `--source-version`
when given, else from the reference's tag — and then feeds the produced file to the REAL
`release_assets_sbom_matches_version()` from `release-assets.sh`. Removing the flag from
`generate_sbom` turns `test_sbom_written_for_a_scanleg_ref_names_the_release_tag` red, and the
`_control` test proves the fake would have reproduced the original failure.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SECURITY_SCAN_SH = REPO_ROOT / "scripts" / "security-scan.sh"
RELEASE_ASSETS_SH = REPO_ROOT / "scripts" / "release" / "release-assets.sh"

pytestmark = pytest.mark.skipif(
    not SECURITY_SCAN_SH.exists() or not RELEASE_ASSETS_SH.exists(),
    reason="scripts/security-scan.sh or scripts/release/release-assets.sh not in this checkout",
)

REPO = "davidamacey/opentranscribe-backend"
TAG = "v0.5.0"
SCAN_ALIAS = f"{REPO}:{TAG}-scanleg-amd64"

# A fake `syft`. It reproduces exactly one real behaviour: metadata.component.version comes
# from --source-version when supplied, and otherwise from the tag of the reference it was
# handed. Everything else about a CycloneDX document is irrelevant to the criterion under
# test. It also records its full argv so the "one walk, not two" assertion is measurable.
_FAKE_SYFT = r"""#!/usr/bin/env python3
import json
import os
import sys

argv = sys.argv[1:]
with open(os.environ["SYFT_ARGV_LOG"], "a", encoding="utf-8") as handle:
    handle.write("\x1f".join(argv) + "\n")

ref = argv[0]
name = ref.rsplit(":", 1)[0]
version = ref.rsplit(":", 1)[1] if ":" in ref else ""
outputs = []
i = 1
while i < len(argv):
    if argv[i] == "--source-name":
        name = argv[i + 1]
        i += 2
    elif argv[i] == "--source-version":
        version = argv[i + 1]
        i += 2
    elif argv[i] in ("-o", "--output"):
        outputs.append(argv[i + 1])
        i += 2
    else:
        i += 1

doc = json.dumps({"metadata": {"component": {"name": name, "version": version}}})
if not outputs:
    sys.stdout.write(doc)
for spec in outputs:
    fmt, _, path = spec.partition("=")
    payload = doc if fmt == "cyclonedx-json" else "NAME VERSION\n"
    if path:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(payload)
    else:
        sys.stdout.write(payload)
"""


def _extract_function(script: Path, name: str) -> str:
    out = subprocess.run(
        ["sed", "-n", f"/^{name}()/,/^}}/p", str(script)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.strip(), f"{name}() not found in {script.name}"
    return out


def _run_generate_sbom(
    tmp_path: Path, *, image: str, pass_identity: bool
) -> tuple[Path, list[list[str]]]:
    """Run the REAL generate_sbom() against a fake syft. Returns (sbom path, syft argvs)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "syft"
    fake.write_text(_FAKE_SYFT, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    out_dir = tmp_path / "reports"
    out_dir.mkdir()
    argv_log = tmp_path / "syft-argv.log"

    identity = f'"{REPO}" "{TAG}"' if pass_identity else ""
    snippet = f"""
set -e
print_header()  {{ :; }}
print_success() {{ :; }}
print_info()    {{ :; }}
OUTPUT_DIR="{out_dir}"

{_extract_function(SECURITY_SCAN_SH, "generate_sbom")}

generate_sbom "{image}" "backend-amd64" {identity}
"""
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["SYFT_ARGV_LOG"] = str(argv_log)
    proc = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, f"generate_sbom failed:\n{proc.stdout}\n{proc.stderr}"

    argvs = [
        line.split("\x1f")
        for line in argv_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return out_dir / "backend-amd64-sbom.json", argvs


def _finish_stage_accepts(sbom: Path, version: str) -> bool:
    """The REAL release_assets_sbom_matches_version() from release-assets.sh."""
    snippet = f"""
source "{RELEASE_ASSETS_SH}"
release_assets_sbom_matches_version "{sbom}" "{version}"
"""
    return subprocess.run(["bash", "-c", snippet], capture_output=True, text=True).returncode == 0


@pytest.mark.unit
def test_sbom_written_for_a_scanleg_ref_names_the_release_tag(tmp_path: Path) -> None:
    """The whole loop: scan alias in, release tag out, finish stage accepts it."""
    sbom, _ = _run_generate_sbom(tmp_path, image=SCAN_ALIAS, pass_identity=True)

    doc = json.loads(sbom.read_text(encoding="utf-8"))
    component = doc["metadata"]["component"]
    assert component["version"] == TAG, (
        f"the SBOM must describe {TAG}, not the per-arch scan alias — got "
        f"{component['version']!r}; 95-finish.sh compares this field EXACTLY"
    )
    assert component["name"] == REPO, f"the SBOM must name the repo, got {component['name']!r}"
    assert _finish_stage_accepts(sbom, TAG), (
        "95-finish.sh's sbom-describes-this-version criterion rejected an SBOM produced by "
        "the real generate_sbom() — the release would stop in its last stage"
    )


@pytest.mark.unit
def test_control_without_the_identity_flags_the_finish_stage_rejects_it(tmp_path: Path) -> None:
    """Must-fail control: the fake syft really does reproduce the original defect.

    Without this, a fake that always writes the plain version would make the test above pass
    against a `generate_sbom` that dropped `--source-version` entirely — a test that cannot
    fail, checking a fix that is not there.
    """
    sbom, _ = _run_generate_sbom(tmp_path, image=SCAN_ALIAS, pass_identity=False)

    doc = json.loads(sbom.read_text(encoding="utf-8"))
    assert doc["metadata"]["component"]["version"] == f"{TAG}-scanleg-amd64", (
        "the fake syft is supposed to derive the version from the reference when no "
        "--source-version is given; if it does not, the sibling test proves nothing"
    )
    assert not _finish_stage_accepts(sbom, TAG), (
        "release_assets_sbom_matches_version accepted a scanleg-versioned SBOM — the "
        "exact-match criterion this whole fix exists for has been loosened"
    )


@pytest.mark.unit
def test_one_syft_walk_produces_both_output_formats(tmp_path: Path) -> None:
    """Two full catalogue walks of a ~13.8 GB image to write two renderings of one result.

    syft renders as many `-o fmt=path` targets as it is given from a single catalogue pass;
    calling it twice doubles the only expensive part of this function.
    """
    _, argvs = _run_generate_sbom(tmp_path, image=SCAN_ALIAS, pass_identity=True)

    assert len(argvs) == 1, f"expected exactly one syft invocation, got {len(argvs)}: {argvs}"
    formats = {argvs[0][i + 1].partition("=")[0] for i, a in enumerate(argvs[0]) if a == "-o"}
    assert formats == {"cyclonedx-json", "table"}, (
        f"both the machine-readable and human-readable renderings must come from the one "
        f"walk; got {formats}"
    )


@pytest.mark.unit
def test_scan_component_passes_the_repo_and_tag_not_the_scan_alias() -> None:
    """The wiring: generate_sbom's caller must hand it repo/tag, never `$image`.

    generate_sbom takes the identity as arguments precisely so it cannot re-derive it from
    the alias; passing `"${image}"` a second time would satisfy the arity and reintroduce the
    bug in a form that reads as correct.
    """
    body = _extract_function(SECURITY_SCAN_SH, "scan_component")
    call = next(
        (line.strip() for line in body.splitlines() if "generate_sbom " in line),
        "",
    )
    assert call, "no generate_sbom call found in scan_component()"
    assert '"${repo}" "${tag}"' in call, (
        f"scan_component must pass the repo and release tag to generate_sbom, not the "
        f"per-arch scan alias; found: {call!r}"
    )
