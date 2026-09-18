"""``.github/dependabot.yml`` has already broken silently once from bad syntax.

The file's own header (see its comment block) documents the incident: a pip ``ignore:``
entry used an npm-style wildcard (``0.5.x``) instead of a real PEP 440 requirement string.
GitHub's API rejects an invalid ``ignore`` condition by rejecting the **whole file** --
every grouped weekly PR this config describes silently stopped running, and nothing
reported it because Dependabot only re-validates when the file changes.

v0.5.1 dependency maintenance (#940) added three more pip/npm ``ignore:`` entries
(``sentence-transformers``, ``fastapi``, ``typescript``) to this exact file. Nothing in
this repo validated the old ones either -- this is that gate, so the next edit to this
file fails loudly here instead of silently on GitHub a week later.

Must-fire / must-stay-clean pattern per ``backend/tests/CLAUDE.md``'s "four tools that
keep the suite honest": a detector that cannot fire is indistinguishable from a clean file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from packaging.specifiers import InvalidSpecifier
from packaging.specifiers import SpecifierSet

REPO_ROOT = Path(__file__).resolve().parents[3]
DEPENDABOT_YML = REPO_ROOT / ".github" / "dependabot.yml"


def _load() -> dict[str, Any]:
    with DEPENDABOT_YML.open() as f:
        loaded = yaml.safe_load(f)
    assert isinstance(loaded, dict), f"dependabot.yml did not parse to a mapping: {type(loaded)}"
    return loaded


def _pip_version_ignore_entries(config: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Yields (directory, dependency_name, version_string) for every pip `ignore[].versions[]`."""
    entries: list[tuple[str, str, str]] = []
    for update in config.get("updates", []):
        if update.get("package-ecosystem") != "pip":
            continue
        directory = update.get("directory", "?")
        for ignore in update.get("ignore", []):
            dep_name = ignore.get("dependency-name", "?")
            for version_str in ignore.get("versions", []):
                entries.append((directory, dep_name, version_str))
    return entries


def test_dependabot_yml_parses_as_valid_yaml() -> None:
    config = _load()
    assert isinstance(config, dict), "dependabot.yml did not parse to a mapping"
    assert config.get("version") == 2, "dependabot.yml must declare version: 2"


def test_every_update_entry_has_the_required_fields() -> None:
    config = _load()
    updates = config.get("updates", [])
    assert updates, "dependabot.yml has no updates: entries -- nothing would ever run"
    for update in updates:
        assert "package-ecosystem" in update, f"update entry missing package-ecosystem: {update}"
        assert "directory" in update, f"update entry missing directory: {update}"
        assert "schedule" in update, f"update entry missing schedule: {update}"


def test_every_pip_ignore_versions_entry_is_a_valid_pep440_specifier() -> None:
    """The exact historical failure mode: a pip `versions:` condition must be a real
    PEP 440 requirement string, never an npm-style wildcard like "0.5.x".

    GitHub rejects the whole file on one bad entry here -- this is the check that
    would have caught the original incident before it shipped.
    """
    config = _load()
    entries = _pip_version_ignore_entries(config)
    assert entries, (
        "no pip ignore[].versions[] entries found -- either the fixture drifted or "
        "this detector is checking nothing"
    )
    bad: list[str] = []
    for directory, dep_name, version_str in entries:
        try:
            SpecifierSet(version_str)
        except InvalidSpecifier:
            bad.append(f"{directory} {dep_name}: {version_str!r}")
    assert not bad, (
        "invalid PEP 440 version specifier(s) in dependabot.yml's pip ignore rules -- "
        "GitHub rejects the ENTIRE file on one bad entry, silently stopping every "
        f"grouped weekly PR (see this file's own docstring): {bad}"
    )


@pytest.mark.parametrize(
    "bad_version_str",
    [
        "0.5.x",  # the actual historical incident (npm-style wildcard)
        ">=0.5.0,<0.6.x",  # mixed valid/invalid in one comma list
        "not-a-version-at-all",
    ],
)
def test_the_pep440_check_actually_rejects_known_bad_strings(bad_version_str: str) -> None:
    """Must-fire case: prove the detector used above can actually reject something,
    not just pass vacuously against a currently-clean file."""
    with pytest.raises(InvalidSpecifier):
        SpecifierSet(bad_version_str)


@pytest.mark.parametrize(
    ("good_version_str", "should_contain", "should_not_contain"),
    [
        (">=0.137.0", "0.141.1", "0.136.3"),
        (">=0.5.0,<0.6.0", "0.5.5", "0.6.0"),
        ("==1.2.3", "1.2.3", "1.2.4"),
    ],
)
def test_the_pep440_check_accepts_known_good_strings(
    good_version_str: str, should_contain: str, should_not_contain: str
) -> None:
    """Must-stay-clean case, mirroring the must-fire case above.

    Asserts real filtering behaviour, not just "construction didn't raise" --
    a `SpecifierSet` that silently accepted any string and matched nothing
    would still pass a not-raises-only check.
    """
    spec = SpecifierSet(good_version_str)
    assert spec.contains(should_contain), f"{good_version_str!r} should match {should_contain!r}"
    assert not spec.contains(should_not_contain), (
        f"{good_version_str!r} should NOT match {should_not_contain!r}"
    )
