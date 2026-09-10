"""A self-hosted install must receive the license it is granted under (issue #862).

`release-manifest.txt` shipped `NOTICE` — third-party attribution — and not `LICENSE`.
That is the wrong way round: the attribution for other people's work arrived and this
project's own terms did not. GPL-family licenses require the license text to accompany the
copies you convey, and **no build context can supply it**: the Dockerfile contexts are
`./backend`, `./frontend` and `./docs-site`, so a repo-root file reaches a deployment only
through this manifest. The `NOTICE` entry's own comment already makes exactly this argument
("a NOTICE nobody receives is not attribution"); it simply was never applied to `LICENSE`.

⚠️ **This parses the manifest the way the INSTALLER does**, deliberately. A test with its
own parsing rules proves something about a file format nobody uses:
`setup-opentranscribe.sh::download_release_manifest_artifacts` strips `#`-comments and
blanks, takes field 1 (TAB-separated) as the path and field 2 as the comma-separated flags,
and `opentranscribe.sh`'s `update-full` arm replays the same loop.
"""

from __future__ import annotations

import pathlib

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_MANIFEST = _REPO_ROOT / "release-manifest.txt"

#: Files whose absence is a legal problem, not merely a missing feature.
_REQUIRED_LEGAL_FILES = ("LICENSE", "NOTICE")


def _manifest_entries() -> dict[str, set[str]]:
    """`{path: {flags}}`, parsed exactly as the installer's read loop parses it."""
    entries: dict[str, set[str]] = {}
    for raw in _MANIFEST.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        fields = raw.split("\t")
        path = fields[0].strip()
        if not path:
            continue
        flags = fields[1].strip() if len(fields) > 1 else ""
        entries[path] = {flag.strip() for flag in flags.split(",") if flag.strip()}
    return entries


def test_the_manifest_parses_at_all():
    """Guard the guard: a parser that returns nothing declares every file missing —
    or, with an inverted assertion, declares every file present."""
    entries = _manifest_entries()

    assert len(entries) > 10, f"the manifest parsed to {len(entries)} entries; the format moved"
    assert "docker-compose.yml" in entries, "the manifest's own first required entry is missing"


@pytest.mark.parametrize("legal_file", _REQUIRED_LEGAL_FILES)
def test_the_manifest_ships_the_legal_files(legal_file: str):
    assert legal_file in _manifest_entries(), (
        f"{legal_file} is not in release-manifest.txt, so a self-hosted install never "
        f"receives it — and no Dockerfile can COPY it either (build contexts are "
        f"./backend, ./frontend, ./docs-site)."
    )


@pytest.mark.parametrize("legal_file", _REQUIRED_LEGAL_FILES)
def test_the_legal_files_are_never_optional(legal_file: str):
    """`optional` makes a 404 non-fatal. For these two that is exactly the failure mode:
    the install completes, reports success, and the operator has no license."""
    flags = _manifest_entries().get(legal_file, set())

    assert "optional" not in flags, f"{legal_file} must not be marked optional"


@pytest.mark.parametrize("legal_file", _REQUIRED_LEGAL_FILES)
def test_the_legal_files_actually_exist_in_the_tree(legal_file: str):
    """A manifest entry for a file that is not there is a required 404 — a hard install
    failure, not a missing license."""
    path = _REPO_ROOT / legal_file

    assert path.is_file(), f"{legal_file} is listed in the manifest but not present"
    assert path.stat().st_size > 0, f"{legal_file} is empty"


def test_the_shipped_license_is_the_agpl_the_project_claims():
    """Not a formatting check: every README, FAQ and image label says AGPL-3.0, and a
    LICENSE that said something else would make all of them wrong at once."""
    text = (_REPO_ROOT / "LICENSE").read_text(encoding="utf-8")

    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in text
    assert "Version 3, 19 November 2007" in text
    # Section 13 is what makes this AGPL rather than GPL, and it is the clause the
    # in-app source offer (`about.legal.*`) exists to discharge.
    assert "13. Remote Network Interaction" in text
