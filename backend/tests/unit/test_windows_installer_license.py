"""The Windows installer's licence pane must show the real AGPL-3.0 text (issue #887).

Before this fix, ``installer.iss``'s ``LicenseFile=`` pointed at
``windows-installer/license.txt`` — a 61-line summary opening "OpenTranscribe -
AI-Powered Transcription Application" that never once contained the words "GNU Affero
General Public License" text of the actual license (it *referenced* the AGPL by name
in prose, but the pane the user clicks "I accept" on showed the summary, not the
license). Root ``LICENSE`` (the real AGPL-3.0 text) was never copied into the build
directory at all, and ``scripts/build-windows-installer.sh`` could silently synthesize
a five-line placeholder "licence" if ``windows-installer/license.txt`` was ever emptied.

⚠️ **These assertions are pinned to the #862 design, not #887's.** #887 and #862 fixed the
same mechanism independently and were merged together: #887 *demoted* the summary in place
(``windows-installer/license.txt`` rewritten as NOTICE source), #862 *deleted* it and copies
the repo-root ``NOTICE`` — the real attribution file, which the release manifest already
ships — instead. The merge kept #862's design, which left this module addressed at
``windows-installer/license.txt``: a path that no longer exists. Because that path was one
of the four in a module-level ``skipif``, **all eight tests silently skipped** — #887's
entire regression guard was dead in the merged tree, reporting neither pass nor failure.
There is deliberately no ``skipif`` now: every path below is a tracked repo file, so a
missing one is a real defect and must fail loudly rather than disappear.

Static — no Windows host or ISCC compiler is available here. The manual click-through
verification these can't cover is documented in the PR / release notes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
LICENSE = REPO_ROOT / "LICENSE"
#: The attribution file the build script copies in as ``NOTICE.txt`` (issue #862). This
#: is the repo ROOT ``NOTICE``, not the deleted ``windows-installer/license.txt`` summary.
NOTICE_SOURCE = REPO_ROOT / "NOTICE"
INSTALLER_ISS = REPO_ROOT / "windows-installer" / "installer.iss"
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build-windows-installer.sh"
AFTER_INSTALL = REPO_ROOT / "windows-installer" / "after-install.txt"

#: Every repo file the installer's legal story depends on. Named so a missing one says
#: which, instead of skipping the module (which is how this guard went dead once already).
_REQUIRED_PATHS = (LICENSE, NOTICE_SOURCE, INSTALLER_ISS, BUILD_SCRIPT, AFTER_INSTALL)


@pytest.mark.parametrize("path", _REQUIRED_PATHS, ids=lambda p: p.name)
def test_every_file_these_assertions_read_actually_exists(path: Path):
    """Guard the guard. These used to sit behind a module ``skipif`` keyed on exactly
    this condition, so deleting one of them turned the whole module into 8 silent skips
    rather than a failure. Assert instead of skip: absence is the defect."""
    assert path.exists(), (
        f"{path.relative_to(REPO_ROOT)} is missing — the Windows installer's licence "
        "assertions below have nothing to read"
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


def test_build_script_copies_the_root_notice_as_the_attribution_file():
    """Third-party attribution must ship too, and from the ROOT NOTICE (issue #862) —
    the file `release-manifest.txt` already ships and that CI validates, not a
    hand-maintained per-platform summary that drifts from it."""
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    expected_command = 'cp NOTICE "${PACKAGE_DIR}/NOTICE.txt"'
    assert expected_command in script, (
        f"build-windows-installer.sh has no {expected_command!r} — the installer would "
        "convey the AGPL text with no third-party attribution beside it"
    )


def test_build_script_copies_no_file_that_does_not_exist():
    """Every `cp <repo path>` in the build script must name a real file.

    This is the check that would have caught the merge: #862 deleted
    `windows-installer/license.txt` while #887's build-script line still copied it, and
    nothing static would have noticed until `cp` failed on a Windows packaging run."""
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    sources = re.findall(r'^\s*cp (?:-\S+ )*([A-Za-z][\w./-]*)\s+"\$\{', script, re.MULTILINE)
    assert len(sources) >= 8, (
        f"only matched {len(sources)} `cp` sources — the build script's copy shape moved "
        "and this assertion is no longer reading it"
    )
    missing = [s for s in sources if not (REPO_ROOT / s).exists()]
    assert not missing, f"build-windows-installer.sh copies files that do not exist: {missing}"


def test_notice_txt_is_added_to_the_files_section_so_it_survives_into_the_install_dir():
    text = INSTALLER_ISS.read_text(encoding="utf-8")
    matches = re.findall(r'Source:\s*"\{#BuildDir\}\\NOTICE\.txt".*DestDir:\s*"\{app\}"', text)
    assert matches, (
        "NOTICE.txt is not in installer.iss's [Files] section -- it would exist only "
        "during setup and disappear, unlike every other installed doc"
    )
    # The #887/#862 merge auto-applied both lanes' non-overlapping insertions and
    # produced this line twice; Inno Setup would then install the same file twice.
    assert len(matches) == 1, f"NOTICE.txt appears in [Files] {len(matches)} times, expected once"


def test_the_notice_does_not_present_itself_as_the_license():
    """The attribution file must not read as the terms — that confusion is the whole of
    #887. It must instead point at LICENSE, and carry what the AGPL text does not."""
    text = NOTICE_SOURCE.read_text(encoding="utf-8")
    assert "GNU AFFERO GENERAL PUBLIC LICENSE\nVersion 3" not in text, (
        "root NOTICE appears to contain the AGPL-3.0 text itself; LICENSE is that file"
    )
    assert "See the LICENSE file for the full license text." in text, (
        "root NOTICE does not point the reader at LICENSE for the actual terms"
    )
    # The attribution this file exists to carry, and which the AGPL text does not cover.
    assert "CC-BY-4.0" in text, "the CC-BY-4.0 diarization-model attribution is missing"
    assert "Apache License, Version 2.0" in text, "the Apache-2.0 speakrs attribution is missing"
    assert "BSD-3-Clause" in text, "the OpenBLAS BSD-3-Clause copyright notice is missing"


def test_after_install_describes_the_notice_file_it_actually_ships():
    """`after-install.txt` is shown to the user at the end of setup. It described the
    #887 summary (third-party components, AI model terms, data privacy); the shipped
    NOTICE.txt is #862's attribution file and has no data-privacy section, so the claim
    must match the file or it is a promise setup does not keep."""
    text = AFTER_INSTALL.read_text(encoding="utf-8")
    assert "NOTICE.txt" in text, "setup never tells the user NOTICE.txt exists"
    notice_source = NOTICE_SOURCE.read_text(encoding="utf-8")
    if "data-privacy" in text.lower() or "data privacy" in text.lower():
        assert "DATA PRIVACY" in notice_source or "data privacy" in notice_source.lower(), (
            "after-install.txt tells the user NOTICE.txt carries a data-privacy summary, "
            "but the NOTICE the build script ships has no such section"
        )


def test_no_stale_repository_url_in_license_facing_text():
    """A licence-facing §13-style source pointer that 404s is a licensing defect."""
    for path in (NOTICE_SOURCE, INSTALLER_ISS, AFTER_INSTALL):
        text = path.read_text(encoding="utf-8")
        assert "github.com/davidamacey/opentranscribe" not in text.lower(), (
            f"{path.name} still points at the stale davidamacey/opentranscribe repo"
        )
