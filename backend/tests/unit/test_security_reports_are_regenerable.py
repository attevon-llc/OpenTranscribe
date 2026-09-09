"""A published security report must be one the scanner can still write.

``security-reports/README.md`` states the directory's purpose: *"We believe in security
transparency. All security scan results are published here so users can understand the security
posture of OpenTranscribe [and] make informed deployment decisions."* ``.gitignore`` says the
same in one line — ``# security-reports/ - intentionally tracked in repo``.

That only holds while the published files are the ones the scanner produces. They were not.

Issue #667 split every image into per-architecture legs, so ``security-scan.sh`` writes
``<component>-<arch>-<tool>`` (``backend-amd64-trivy.json``). The 21 files committed here used
the pre-#667 names (``backend-trivy.json``) and **nothing has written them since**, which
``security-scan.sh`` itself records at its report-discovery glob::

    # it used to look for `backend-trivy.json`, a filename nothing writes since #667.

So a directory whose whole job is to answer "what is the security posture of this release"
answered with scans dated 2026-08-10 and 2026-08-31, describing images two releases old, under
names no future scan can refresh. A stale transparency artifact is worse than an absent one: it
is read as current, and nothing about it looks wrong.

This is the same failure this branch has been fixing all week — **an artifact that can no
longer be refreshed still reads as a measurement**. The guard is therefore not "are the reports
recent" (unknowable from a file) but the falsifiable half: *could the current scanner have
produced this filename at all?*

⚠️ **`blackwell` is deliberately never scanned** and must not be "fixed" into this set.
``scripts/release/50-scan.sh`` excludes it explicitly because it is not part of
``docker-build-push.sh all``; it is a component of the scanner's map but never a published
report. Likewise ``backend`` is **amd64-only** (``list-platforms`` reports ``backend cuda
linux/amd64``; ``cuda-arm64`` is reserved-but-unbuilt), so a ``backend-arm64-*`` report would be
just as wrong as a bare ``backend-*`` one — this test does not demand every combination exist,
only that whatever IS committed is a name the scanner could write.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
REPORTS = REPO_ROOT / "security-reports"
SCANNER = REPO_ROOT / "scripts" / "security-scan.sh"

#: Components the scanner knows but that are never published as reports, with the reason.
NEVER_PUBLISHED = {
    "blackwell": (
        "not part of `docker-build-push.sh all`; scripts/release/50-scan.sh skips it explicitly"
    ),
}

#: Architecture suffixes the multi-arch legs use (issue #667).
ARCHES = {"amd64", "arm64"}

#: Tool stems and the extensions each may be PUBLISHED under.
#:
#: The scanner writes eight files per leg; five are tracked. ``*-trivy.json`` /
#: ``*-grype.json`` / ``*-sbom.json`` are gitignored — 30.8 MB per scan against 2.1 MB for
#: these, kept forever in a public repo, regenerable in minutes — and Trivy's JSON additionally
#: embeds the scanned image's ENV block, so every ``python:*``-derived report carries
#: ``GPG_KEY=7169605F…`` (the PUBLIC CPython signing fingerprint), which gitleaks blocks as a
#: generic-api-key. ``dockle`` is json-only because it has no text form, and is small.
TOOL_EXTENSIONS = {
    "trivy": {"txt"},
    "grype": {"txt"},
    "dockle": {"json"},
    "sbom": {"txt"},
    "hadolint": {"txt"},
}

#: Suffixes that must never be committed, with the reason each is excluded.
NEVER_COMMITTED_SUFFIXES = {
    "-trivy.json": "30 MB-class machine artifact; also embeds the image ENV block (gitleaks)",
    "-grype.json": "30 MB-class machine artifact, regenerable from a published tag",
    "-sbom.json": "30 MB-class machine artifact, regenerable from a published tag",
}

#: Files in the directory that are prose, not scan output.
PROSE = {"README.md", "SECURITY-ADVISORY.md"}

pytestmark = pytest.mark.skipif(
    not SCANNER.is_file(), reason="scripts/security-scan.sh is not present in this checkout"
)


def _scanner_components() -> set[str]:
    """Components read out of the scanner's own map, not transcribed here.

    Parsing beats hardcoding: adding a component to ``SCAN_COMPONENT_DOCKERFILE`` is documented
    as "the whole change", and a copy in this file would silently stop matching it.
    """
    text = SCANNER.read_text(encoding="utf-8")
    block = re.search(r"declare -A SCAN_COMPONENT_DOCKERFILE=\((.*?)\)", text, re.S)
    assert block, "SCAN_COMPONENT_DOCKERFILE has moved or been renamed; re-point this test"
    return set(re.findall(r"\[([a-z0-9_-]+)\]=", block.group(1)))


def _tracked_report_files() -> list[str]:
    """Report files git actually tracks (the published set), prose excluded."""
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "security-reports"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return [
        name for line in out.stdout.splitlines() if (name := Path(line).name) and name not in PROSE
    ]


def _is_a_name_the_scanner_could_write(name: str, components: set[str]) -> bool:
    """``<component>-<arch>-<tool>.<ext>`` against the scanner's own vocabulary."""
    stem, _, ext = name.rpartition(".")
    if not stem or ext not in {"json", "txt"}:
        return False
    parts = stem.split("-")
    if len(parts) != 3:
        return False
    component, arch, tool = parts
    return (
        component in components
        and component not in NEVER_PUBLISHED
        and arch in ARCHES
        and ext in TOOL_EXTENSIONS.get(tool, set())
    )


# ------------------------------------------------------------------ the guard


def test_the_component_map_is_readable():
    """Guard the guard: an empty parse would make the check below vacuous."""
    components = _scanner_components()
    assert components, "parsed no components out of security-scan.sh"
    assert "backend" in components and "lite" in components, (
        f"the component parse looks wrong: {sorted(components)}"
    )
    assert NEVER_PUBLISHED.keys() <= components, (
        "a component listed here as never-published is not in the scanner's map at all — "
        "the exemption is stale and would hide a real name mismatch"
    )


def test_every_published_report_is_a_name_the_scanner_still_writes():
    components = _scanner_components()
    orphans = [
        name
        for name in _tracked_report_files()
        if not _is_a_name_the_scanner_could_write(name, components)
    ]
    assert not orphans, (
        f"{len(orphans)} tracked security report(s) use a filename no current scan writes, so "
        "they can never be refreshed and will be read as the current security posture "
        f"forever:\n  {chr(10).join('  ' + o for o in sorted(orphans))}\n"
        "Since #667 the scanner writes <component>-<arch>-<tool>.<ext>. Delete the orphans and "
        "publish the arch-suffixed set (scripts/push-security-reports.sh), or the directory's "
        "stated purpose — security transparency — is served by month-old data."
    )


def test_the_machine_json_stays_out_of_git():
    """Committing these once cost a blocked commit and would cost 30 MB per release forever.

    Kept as its own assertion rather than relying on ``.gitignore``: an ignore rule stops an
    accidental ``git add``, but ``git add -f`` and an already-tracked path both walk straight
    past it, and the second is exactly how the orphans above survived for a month.
    """
    committed = _tracked_report_files()
    for suffix, reason in NEVER_COMMITTED_SUFFIXES.items():
        offenders = sorted(n for n in committed if n.endswith(suffix))
        assert not offenders, (
            f"{len(offenders)} '{suffix}' report(s) are tracked, but {reason}. Regenerate them "
            f"locally with ./scripts/security-scan.sh <component> instead: {offenders[:4]}"
        )


def test_a_never_published_component_is_not_committed():
    """`blackwell` is built by nothing in the release path; a report for it would be a fiction."""
    committed = _tracked_report_files()
    for component, reason in NEVER_PUBLISHED.items():
        offenders = [n for n in committed if n.startswith(f"{component}-")]
        assert not offenders, (
            f"a {component!r} report is committed, but {reason}. A published scan of an image "
            f"the release never builds describes nothing: {offenders}"
        )


# ------------------------------------------------------------------ guard the guards


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("backend-amd64-trivy.txt", True),
        ("lite-arm64-grype.txt", True),
        ("docs-amd64-dockle.json", True),
        ("frontend-arm64-hadolint.txt", True),
        ("lite-amd64-sbom.txt", True),
        # the exact shape that was orphaned — no arch segment
        ("backend-trivy.txt", False),
        ("frontend-sbom.txt", False),
        # the machine artifacts: correctly named, but deliberately not published
        ("backend-amd64-trivy.json", False),
        ("lite-arm64-sbom.json", False),
        # a component the scanner does not know
        ("nosuch-amd64-trivy.json", False),
        # a real component that is never published
        ("blackwell-amd64-trivy.json", False),
        # an arch that does not exist
        ("backend-riscv64-trivy.json", False),
        # tool/extension mismatch: dockle only writes json
        ("backend-amd64-dockle.txt", False),
    ],
)
def test_the_name_matcher_discriminates(name: str, expected: bool):
    """A matcher that accepted everything would pass over any orphan set at all."""
    components = _scanner_components()
    assert _is_a_name_the_scanner_could_write(name, components) is expected, (
        f"{name!r} should have been {'accepted' if expected else 'rejected'}"
    )


def test_the_tracked_listing_is_not_silently_empty():
    """If `git ls-files` returned nothing the main assertion could never fail."""
    tracked = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "security-reports"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.split()
    assert len(tracked) > 1, (
        "git tracks nothing (or only one file) under security-reports/ — either the directory "
        "stopped being tracked (contradicting .gitignore's 'intentionally tracked in repo') or "
        f"this test is looking in the wrong place. Got: {tracked}"
    )
    # Every entry must actually live in the directory this module reasons about; a path from
    # anywhere else would mean the pathspec silently widened.
    strays = [p for p in tracked if not p.startswith("security-reports/")]
    assert not strays, f"git ls-files returned paths outside security-reports/: {strays}"
    # And the published set must contain real scan output, not just the two prose files —
    # otherwise `test_every_published_report_is_a_name_the_scanner_still_writes` would be
    # asserting over an empty list and could never fail.
    reports = [p for p in tracked if Path(p).name not in PROSE]
    assert reports, (
        "security-reports/ tracks only prose (README/advisory) and no scan output at all, so "
        "the orphan check above has nothing to examine"
    )
