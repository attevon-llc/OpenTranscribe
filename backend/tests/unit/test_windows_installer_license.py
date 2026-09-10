"""The Windows installer's licence pane must show the real AGPL-3.0 text (issue #887).

Before this fix, ``installer.iss``'s ``LicenseFile=`` pointed at
``windows-installer/license.txt`` — a 61-line summary opening "OpenTranscribe -
AI-Powered Transcription Application" that never once contained the words "GNU Affero
General Public License" text of the actual license (it *referenced* the AGPL by name
in prose, but the pane the user clicks "I accept" on showed the summary, not the
license). Root ``LICENSE`` (the real AGPL-3.0 text, 661 lines) was never copied into the
build directory at all, and ``scripts/build-windows-installer.sh`` could silently
synthesize a five-line placeholder "licence" if ``windows-installer/license.txt`` was
ever emptied.

Static — no Windows host or ISCC compiler is available here. The manual click-through
verification these can't cover is documented in the PR / release notes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
LICENSE = REPO_ROOT / "LICENSE"
NOTICE_SOURCE = REPO_ROOT / "windows-installer" / "license.txt"
INSTALLER_ISS = REPO_ROOT / "windows-installer" / "installer.iss"
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build-windows-installer.sh"

pytestmark = pytest.mark.skipif(
    not all(p.exists() for p in (LICENSE, NOTICE_SOURCE, INSTALLER_ISS, BUILD_SCRIPT)),
    reason="Windows installer files not present in this checkout",
)


def test_root_license_is_the_real_agpl_text():
    """Guard against someone 'simplifying' it later — a real AGPL-3.0 is ~34 KB."""
    text = LICENSE.read_text(encoding="utf-8")
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in text
    assert "Version 3, 19 November 2007" in text
    assert LICENSE.stat().st_size >= 30_000


def test_installer_iss_points_the_license_pane_at_license_txt():
    text = INSTALLER_ISS.read_text(encoding="utf-8")
    match = re.search(r"^LicenseFile=(.+)$", text, re.MULTILINE)
    assert match, "LicenseFile= not found in installer.iss"
    assert match.group(1).strip().endswith("LICENSE.txt"), (
        f"LicenseFile={match.group(1)!r} does not point at LICENSE.txt — the pane "
        f"would show whatever this DOES point at instead of the real AGPL-3.0 text"
    )


def test_build_script_copies_the_real_license_into_the_license_file_target():
    """Association test, not presence: the file installer.iss's LicenseFile= resolves
    to (LICENSE.txt in the build dir) must be populated FROM root LICENSE by the build
    script — a `cp LICENSE.txt` from somewhere else would satisfy the filename alone."""
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    expected_command = 'cp LICENSE "${PACKAGE_DIR}/LICENSE.txt"'
    assert expected_command in script, (
        f"build-windows-installer.sh has no {expected_command!r} — the installer.iss "
        "LicenseFile= target would not exist at build time, or would resolve to "
        "something else entirely"
    )


def test_build_script_synthesizes_no_license_fallback():
    """The old placeholder fallback must be GONE, not repaired: with root LICENSE
    unconditionally copied, there is no file whose emptiness is tolerable."""
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert "is empty, creating placeholder" not in script
    assert 'echo "OpenTranscribe - AI-Powered Transcription Application" >' not in script


def test_build_script_also_copies_the_notice_summary():
    """The third-party / AI-models / data-privacy notes must still ship -- demoted to
    NOTICE.txt, not deleted (they say things the AGPL text does not)."""
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    expected_command = 'cp windows-installer/license.txt "${PACKAGE_DIR}/NOTICE.txt"'
    assert expected_command in script, (
        "windows-installer/license.txt must still be copied in, as NOTICE.txt"
    )


def test_notice_txt_is_added_to_the_files_section_so_it_survives_into_the_install_dir():
    text = INSTALLER_ISS.read_text(encoding="utf-8")
    assert re.search(r'Source:\s*"\{#BuildDir\}\\NOTICE\.txt".*DestDir:\s*"\{app\}"', text), (
        "NOTICE.txt is not in installer.iss's [Files] section -- it would exist only "
        "during setup and disappear, unlike every other installed doc"
    )


def test_notice_no_longer_claims_to_be_the_license():
    """windows-installer/license.txt's own heading must no longer read as the licence
    itself, now that the real AGPL-3.0 text has its own file."""
    text = NOTICE_SOURCE.read_text(encoding="utf-8")
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" not in text
    assert "NOT the software license" in text or "Third-Party Notices" in text
    # The notes this file exists to preserve must still be present.
    assert "THIRD-PARTY COMPONENTS" in text
    assert "DATA PRIVACY" in text


def test_no_stale_repository_url_in_license_facing_text():
    """A licence-facing §13-style source pointer that 404s is a licensing defect."""
    for path in (NOTICE_SOURCE, INSTALLER_ISS):
        text = path.read_text(encoding="utf-8")
        assert "davidamacey/opentranscribe" not in text.lower(), (
            f"{path.name} still points at the stale davidamacey/opentranscribe repo"
        )
