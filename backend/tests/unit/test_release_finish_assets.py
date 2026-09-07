"""`95-finish.sh`'s release-asset gate must FAIL when the evidence is missing or stale,
and must PASS (with real checksums) when it is complete.

Before this change there was no test of `95-finish.sh` at all. The stage used to attach
release assets with a bare existence glob:

    for f in dist/opentranscribe-offline-*.tar.gz dist/opentranscribe-windows-*.zip \\
             security-reports/*-sbom.json; do
        [[ -f "$f" ]] && assets+=("$f")
    done

Two defects, and they compounded rather than cancelled (issue #781): ABSENCE was silent (a
failed scan, a skipped stage, or a relocated OT_SCAN_DIR just meant nothing was appended, and
`gh release create` shipped a release with zero SBOMs and no error), and PRESENCE was not
evidence either — `security-reports/` is git-tracked and held three stale, non-arch-qualified
pre-#667 SBOMs that the glob would have attached to every future release regardless of which
version they actually described. Same shape this repo already fixed twice: a gate that
reports success when it could not do its job (#413 fail-open scan, #681 unscannable
component reporting a pass).

APPROACH — extract, don't run the whole stage

`95-finish.sh` refuses to run at all without `gh` installed, a real pushed tag, every image
already on Docker Hub, and a green CI run for that tag's SHA — none of which a unit test
should need. So this file extracts just the asset block (`# --- BEGIN release-assets (issue
#781) ---` … `# --- END release-assets ---` in 95-finish.sh) and drives it directly, the same
"extract and run the real shell" technique `test_release_ledger_abort.py` uses for
`run_stage()`. `record`/`fail_out` are replaced with tiny stand-ins that log to a file instead
of writing release-criteria.yaml-checked ledger entries (the bidirectional contract itself is
covered by `test_release_criteria_wiring.py`) — the same "stub the surrounding stage, run the
real logic" idiom `test_opentr_stop_container_scoping.py` uses with a fake `docker` on `PATH`
for `opentr.sh`'s straggler loop.

`release-assets.sh` itself is sourced for real, from the actual checkout — never a copy — so
a regression in the real file fails here, not in a hand-maintained duplicate.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_DIR = REPO_ROOT / "scripts" / "release"
FINISH_SH = RELEASE_DIR / "95-finish.sh"
RELEASE_ASSETS_SH = RELEASE_DIR / "release-assets.sh"

pytestmark = pytest.mark.skipif(
    not FINISH_SH.exists() or not RELEASE_ASSETS_SH.exists(),
    reason="scripts/release/95-finish.sh or release-assets.sh not present in this checkout",
)

VERSION = "v0.5.0"

# A small, deterministic stand-in for docker-build-push.sh's real platform table — three legs
# across two components, so "missing one leg" and "all legs present" are both expressible
# without depending on how many real components/platforms this repo happens to declare today.
_FAKE_PLATFORM_TABLE = "alpha\tcuda\tlinux/amd64\nbeta\tmultiarch\tlinux/amd64,linux/arm64\n"
_REPOS_TSV = "alpha\ttest/alpha-repo\nbeta\ttest/beta-repo\n"
EXPECTED_LEGS = ["alpha-amd64-sbom.json", "beta-amd64-sbom.json", "beta-arm64-sbom.json"]


def _sq(value: str) -> str:
    """Single-quote a value for literal bash embedding.

    Deliberately NOT `repr()`: Python's repr escapes real tab/newline bytes as the two
    characters ``\\t``/``\\n``, and bash single quotes do not interpret backslash escapes —
    so a `repr()`-quoted `repos_tsv` (which needs REAL tabs and newlines for `IFS=$'\\t' read`
    to parse) would arrive as sixteen literal backslash-letter characters instead of the
    control bytes they were meant to represent. Confirmed while writing this harness: every
    case failed with "derived zero expected SBOMs" until this replaced an f-string `{v!r}`.
    """
    return "'" + value.replace("'", "'\\''") + "'"


def _extract_asset_block(script: Path) -> str:
    """The `# --- BEGIN release-assets ... --- END release-assets ---` block, verbatim."""
    text = script.read_text(encoding="utf-8")
    start_marker = "# --- BEGIN release-assets (issue #781) ---"
    end_marker = "# --- END release-assets ---"
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    end = text.index("\n", end) + 1
    return text[start:end]


def _sbom_json(name: str, version: str) -> str:
    return json.dumps({"metadata": {"component": {"name": name, "version": version}}})


def _write_platform_stub(repo_root: Path) -> None:
    stub = repo_root / "scripts" / "docker-build-push.sh"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "list-platforms" ]; then\n'
        f"  printf '{_FAKE_PLATFORM_TABLE}'\n"
        "fi\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)


def _run_asset_block(
    tmp_path: Path,
    *,
    scan_dir: Path,
    version: str = VERSION,
    repos_tsv: str = _REPOS_TSV,
) -> tuple[str, list[list[str]]]:
    """Run the real asset block from 95-finish.sh in a scratch REPO_ROOT.

    Returns (combined stdout+stderr, parsed RECORD rows). Each RECORD row is
    ``[id, outcome, detail, fix, waived]``, in the order `record` was actually called —
    order matters for asserting an early `fail_out` skipped the later checks.
    """
    repo_root = tmp_path / f"scratch-{uuid.uuid4().hex}"
    repo_root.mkdir()
    _write_platform_stub(repo_root)

    record_log = repo_root / "record.log"
    record_log.write_text("", encoding="utf-8")

    asset_block = _extract_asset_block(FINISH_SH)

    harness = f"""
set -uo pipefail
RED=''; GREEN=''; YELLOW=''; NC=''
VERSION={_sq(version)}
SCRIPT_DIR={_sq(str(RELEASE_DIR))}
REPO_ROOT={_sq(str(repo_root))}
repos_tsv={_sq(repos_tsv)}
RECORD_LOG={_sq(str(record_log))}

record() {{
    local id="$1" outcome="$2" detail="${{3:-}}" fix="${{4:-}}" waived="${{5:-}}"
    printf '%s\\t%s\\t%s\\t%s\\t%s\\n' "$id" "$outcome" "$detail" "$fix" "$waived" >> "$RECORD_LOG"
}}
fail_out() {{
    local rc="$1"
    echo "RC=$rc"
    exit "$rc"
}}

cd "$REPO_ROOT" || exit 2

{asset_block}

echo "RC=0"
echo "ASSETS_COUNT=${{#assets[@]}}"
for __a in "${{assets[@]}}"; do
    echo "ASSET_PATH=$__a"
done

# checksum_dir carries an EXIT trap that deletes it the moment THIS process exits, so any
# content verification must happen HERE — a caller inspecting the paths after this subprocess
# returns would find nothing there at all. Copy the real SBOM bytes in beside their sidecars
# and let sha256sum -c do the same comparison a release consumer would.
if [[ -n "${{checksum_dir:-}}" && -d "$checksum_dir" ]]; then
    for __p in "${{present_sboms[@]}}"; do
        cp "$__p" "$checksum_dir/$(basename "$__p")"
    done
    if ( cd "$checksum_dir" && sha256sum -c --strict SHA256SUMS ) >/dev/null 2>&1; then
        echo "CHECKSUM_VERIFY_RC=0"
    else
        echo "CHECKSUM_VERIFY_RC=1"
    fi
fi
"""

    proc = subprocess.run(
        ["bash", "-c", harness],
        capture_output=True,
        text=True,
        cwd=repo_root,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            # No .release/<version>/scan-dir marker in this scratch repo, so
            # release_assets_resolve_scan_dir falls back to OT_SCAN_DIR exactly the way
            # 95-finish.sh does when an operator re-runs finish after 50-scan.sh relocated
            # its output — this is the fallback leg of that resolution, not the marker leg.
            "OT_SCAN_DIR": str(scan_dir),
        },
        timeout=30,
        check=False,
    )
    out = proc.stdout + proc.stderr

    records: list[list[str]] = []
    if record_log.exists():
        for line in record_log.read_text(encoding="utf-8").splitlines():
            if line:
                records.append(line.split("\t"))

    return out, records


def _write_complete_sbom_set(scan_dir: Path, version: str = VERSION) -> None:
    scan_dir.mkdir(parents=True, exist_ok=True)
    for leg in EXPECTED_LEGS:
        (scan_dir / leg).write_text(_sbom_json(f"test/{leg}", version), encoding="utf-8")


@pytest.mark.unit
def test_missing_sbom_fails_finish(tmp_path: Path) -> None:
    """#781's acceptance criterion, made repeatable: move an SBOM aside, watch finish refuse.

    6-of-7 in the real repo's leg count; here, 2-of-3 against the deterministic fake table —
    same shape, independent of how many components/platforms this repo declares today.
    """
    scan_dir = tmp_path / "scan"
    _write_complete_sbom_set(scan_dir)
    missing = scan_dir / "beta-arm64-sbom.json"
    missing.unlink()

    out, records = _run_asset_block(tmp_path, scan_dir=scan_dir)

    assert "RC=1" in out, f"a missing leg must refuse the stage:\n{out}"
    leg_records = [r for r in records if r[0] == "sbom-per-published-leg"]
    assert len(leg_records) == 1, f"expected exactly one record for the leg check:\n{records}"
    assert leg_records[0][1] == "fail", f"expected a fail outcome:\n{records}"
    assert "beta-arm64-sbom.json" in leg_records[0][2], (
        f"the failure detail must name the missing leg:\n{records}"
    )
    # The early fail_out must stop before the later checks ever ran.
    later_ids = {r[0] for r in records if r[0] != "sbom-per-published-leg"}
    assert "sbom-describes-this-version" not in later_ids, (
        f"a missing-leg failure must not also record the version check:\n{records}"
    )
    assert "asset-checksums" not in later_ids, (
        f"a missing-leg failure must not also record the checksum check:\n{records}"
    )


@pytest.mark.unit
def test_stale_sbom_fails_finish(tmp_path: Path) -> None:
    """All legs present, but one names a different version — P6: presence is not evidence."""
    scan_dir = tmp_path / "scan"
    _write_complete_sbom_set(scan_dir)
    stale = scan_dir / "beta-arm64-sbom.json"
    stale.write_text(_sbom_json("test/beta-arm64-sbom.json", "v0.4.1"), encoding="utf-8")

    out, records = _run_asset_block(tmp_path, scan_dir=scan_dir)

    assert "RC=1" in out, f"a stale-version SBOM must refuse the stage:\n{out}"
    leg_records = [r for r in records if r[0] == "sbom-per-published-leg"]
    assert leg_records and leg_records[0][1] == "pass", (
        f"presence is unaffected by content — the leg check must still pass:\n{records}"
    )
    version_records = [r for r in records if r[0] == "sbom-describes-this-version"]
    assert len(version_records) == 1, f"expected exactly one version-check record:\n{records}"
    assert version_records[0][1] == "fail", f"expected a fail outcome:\n{records}"
    assert "beta-arm64-sbom.json" in version_records[0][2], (
        f"the failure detail must name the offending file:\n{records}"
    )
    assert not any(r[0] == "asset-checksums" for r in records), (
        f"a version-mismatch failure must not also record the checksum check:\n{records}"
    )


@pytest.mark.unit
def test_complete_sbom_set_passes(tmp_path: Path) -> None:
    """Must-stay-clean control.

    Without this, a gate that fails on everything (a typo in the SBOM filename pattern, an
    inverted comparison, an always-false version check) looks identical to one that works —
    every case above would still show RC=1. This is the one case that has to reach RC=0.
    """
    scan_dir = tmp_path / "scan"
    _write_complete_sbom_set(scan_dir)

    out, records = _run_asset_block(tmp_path, scan_dir=scan_dir)

    assert "RC=0" in out, f"a complete, correctly-versioned SBOM set must pass:\n{out}"
    outcomes = {r[0]: r[1] for r in records}
    assert outcomes.get("sbom-per-published-leg") == "pass", records
    assert outcomes.get("sbom-describes-this-version") == "pass", records
    assert outcomes.get("asset-checksums") == "pass", records
    # The two developer-built packages are waived, not silently absent from the ledger.
    assert outcomes.get("offline-package-attached") == "not-measured", records
    assert outcomes.get("windows-package-attached") == "not-measured", records
    waived = {r[0]: r[4] for r in records}
    assert waived.get("offline-package-attached") == "waived", records
    assert waived.get("windows-package-attached") == "waived", records
    assert not any(o == "fail" for o in outcomes.values()), (
        f"the clean control must not record ANY failure:\n{records}"
    )

    # 3 SBOMs + 3 .sha256 sidecars + 1 combined SHA256SUMS.
    count_line = next(line for line in out.splitlines() if line.startswith("ASSETS_COUNT="))
    assert count_line == "ASSETS_COUNT=7", f"unexpected asset count:\n{out}"


@pytest.mark.unit
def test_every_attached_asset_has_a_checksum(tmp_path: Path) -> None:
    """Every non-checksum asset in the final array pairs with a verifying `.sha256`, and the
    pairing verifies against real file content via `sha256sum -c` — not merely "a file with
    that name exists".

    The checksum scratch directory carries an EXIT trap (release-assets.sh's own header
    explains why: a `trap` set inside a `$(...)` subshell fires immediately, so the function
    sets a variable instead and must run in the caller's own shell) that deletes it the moment
    the harness process exits — so `sha256sum -c` runs INSIDE the harness, before that happens,
    and reports its result on stdout rather than leaving files for this test to inspect after
    the fact.
    """
    scan_dir = tmp_path / "scan"
    _write_complete_sbom_set(scan_dir)

    out, _records = _run_asset_block(tmp_path, scan_dir=scan_dir)
    assert "RC=0" in out, out

    asset_paths = [
        Path(line.split("=", 1)[1]) for line in out.splitlines() if line.startswith("ASSET_PATH=")
    ]
    assert asset_paths, f"no assets were reported at all:\n{out}"

    sbom_paths = [p for p in asset_paths if p.suffix == ".json"]
    sidecar_names = {p.name for p in asset_paths if p.name.endswith(".sha256")}
    sums_paths = [p for p in asset_paths if p.name == "SHA256SUMS"]

    assert len(sbom_paths) == len(EXPECTED_LEGS), f"expected {len(EXPECTED_LEGS)} SBOMs:\n{out}"
    assert len(sums_paths) == 1, f"expected exactly one combined SHA256SUMS:\n{out}"

    for sbom_path in sbom_paths:
        expected_sidecar = f"{sbom_path.name}.sha256"
        assert expected_sidecar in sidecar_names, (
            f"{sbom_path.name} has no matching .sha256 in the attached assets:\n{asset_paths}"
        )

    assert "CHECKSUM_VERIFY_RC=0" in out, (
        f"sha256sum -c must verify every sidecar against the real file, run inside the "
        f"scratch tree before it is cleaned up:\n{out}"
    )
