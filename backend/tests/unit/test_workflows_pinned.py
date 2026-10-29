"""Workflow supply-chain hygiene: every action SHA-pinned, token permissions declared.

A `uses: owner/action@v7` tag can be re-pointed by whoever controls that repository, and a
workflow without a `permissions:` block inherits the repository default token scope. Both are
checked statically here so a new or edited workflow cannot regress either.

Rules, per workflow file:
  * every `uses:` of a remote action is `@<40 hex>` followed by a `# vX.Y.Z` comment
    (local `./` actions and `docker://...@sha256:` references are exempt);
  * a top-level `permissions:` mapping exists;
  * `pull_request_target` is absent unless listed in PULL_REQUEST_TARGET_JUSTIFIED.

FENCED lists workflows whose fixes are owned by another change set and land later. The test
is a ratchet: it fails when a fenced file is already compliant, so the entry must be deleted
the moment the file is fixed.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

WORKFLOWS_DIR = Path(__file__).resolve().parents[3] / ".github" / "workflows"

FENCED: dict[str, str] = {
    "release-validate.yml": "edited only by the release workstream; its pins and checkout "
    "hardening land after the release",
    "docker-publish.yml": "retired stub owned by the release workstream; its top-level "
    "permissions block lands after the release",
}

# workflow filename -> why `pull_request_target` (runs with a write token on fork PRs) is safe.
PULL_REQUEST_TARGET_JUSTIFIED: dict[str, str] = {}

_USES_RE = re.compile(
    r"""^\s*(?:-\s+)?["']?uses["']?\s*:\s*(?:&\S+\s+)?["']?(?P<ref>[^\s"'#]+)["']?\s*(?P<comment>#.*)?$"""
)
_USES_KEY_RE = re.compile(r"""^\s*(?:-\s+)?["']?uses["']?\s*:""")
_PINNED_RE = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
_VERSION_COMMENT_RE = re.compile(r"^#\s*v\d+(\.\d+){0,2}\b")


def _is_exempt(ref: str) -> bool:
    return ref.startswith("./") or (ref.startswith("docker://") and "@sha256:" in ref)


def _parsed_uses(doc: dict[object, object]) -> list[str]:
    """Every `uses` value in the parsed document (job-level and step-level)."""
    refs: list[str] = []
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return refs
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        if isinstance(job.get("uses"), str):
            refs.append(job["uses"])
        steps = job.get("steps")
        for step in steps if isinstance(steps, list) else []:
            if isinstance(step, dict) and isinstance(step.get("uses"), str):
                refs.append(step["uses"])
    return refs


def _line_violations(path: Path, text: str) -> tuple[list[str], int]:
    """Pin/comment violations from a line scan, plus how many `uses` keys it saw."""
    found: list[str] = []
    line_refs = 0
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not _USES_KEY_RE.match(line):
            continue
        line_refs += 1
        m = _USES_RE.match(line)
        if not m:
            found.append(f"{path.name}:{lineno}: uses value is not on the same line, cannot verify")
            continue
        if _is_exempt(m.group("ref")):
            continue
        if not _PINNED_RE.match(m.group("ref")):
            found.append(f"{path.name}:{lineno}: not pinned to a 40-hex commit: {m.group('ref')}")
        elif not _VERSION_COMMENT_RE.match(m.group("comment") or ""):
            found.append(f"{path.name}:{lineno}: pinned ref has no '# vX.Y.Z' comment")

    return found, line_refs


def violations(path: Path) -> list[str]:
    """Return human-readable hygiene violations for one workflow file."""
    text = path.read_text()
    found, line_refs = _line_violations(path, text)
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict):
        return [*found, f"{path.name}: does not parse to a mapping"]

    parsed = _parsed_uses(doc)
    if len(parsed) != line_refs:
        found.append(
            f"{path.name}: {len(parsed)} parsed uses vs {line_refs} found line by line "
            "(flow-style or otherwise unusual spelling)"
        )
    for ref in parsed:
        if not _is_exempt(ref) and not _PINNED_RE.match(ref):
            found.append(f"{path.name}: parsed uses not pinned to a 40-hex commit: {ref}")

    if not isinstance(doc.get("permissions"), dict):
        found.append(f"{path.name}: no top-level permissions mapping (use `permissions: {{}}`)")
    jobs = doc.get("jobs")
    for job_id, job in jobs.items() if isinstance(jobs, dict) else []:
        if (
            isinstance(job, dict)
            and "permissions" in job
            and not isinstance(job["permissions"], dict)
        ):
            found.append(
                f"{path.name}: job {job_id} permissions must be a mapping, not {job['permissions']!r}"
            )
    # PyYAML (YAML 1.1) parses the bare key `on` as boolean True.
    triggers = doc.get("on", doc.get(True))
    if isinstance(triggers, dict | list) and "pull_request_target" in triggers:
        if path.name not in PULL_REQUEST_TARGET_JUSTIFIED:
            found.append(f"{path.name}: pull_request_target without a justification")
    elif triggers == "pull_request_target" and path.name not in PULL_REQUEST_TARGET_JUSTIFIED:
        found.append(f"{path.name}: pull_request_target without a justification")
    return found


def workflow_files(directory: Path) -> list[Path]:
    return sorted([*directory.glob("*.yml"), *directory.glob("*.yaml")])


def check_directory(directory: Path, fenced: dict[str, str]) -> list[str]:
    """Violations in `directory`, excluding fenced files; plus ratchet failures for fenced ones."""
    problems: list[str] = []
    for path in workflow_files(directory):
        found = violations(path)
        if path.name in fenced:
            if not found:
                problems.append(f"{path.name}: already compliant, remove it from FENCED")
        else:
            problems.extend(found)
    for name in fenced:
        if not (directory / name).is_file():
            problems.append(f"{name}: listed in FENCED but does not exist, remove it")
    return problems


def test_repository_workflows_are_pinned_and_least_privilege() -> None:
    assert workflow_files(WORKFLOWS_DIR), "no workflows found: the test would pass vacuously"
    assert check_directory(WORKFLOWS_DIR, FENCED) == []


@pytest.mark.parametrize("name", sorted(FENCED))
def test_fenced_entries_have_a_reason(name: str) -> None:
    assert FENCED[name].strip()


@pytest.mark.parametrize(
    "name",
    [
        "pre-commit.yml",
        "deploy-docs.yml",
        "seam-guard.yml",
        "security-scan.yml",
        "label-from-template.yml",
    ],
)
def test_hardened_workflows_are_strictly_clean(name: str) -> None:
    assert violations(WORKFLOWS_DIR / name) == []


_GOOD = """\
name: x
on:
  push:
permissions: {}
jobs:
  j:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1
      - uses: ./.github/actions/local
"""


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "w.yml"
    path.write_text(body)
    return path


def test_detector_accepts_a_compliant_workflow(tmp_path: Path) -> None:
    assert violations(_write(tmp_path, _GOOD)) == []


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda s: s.replace("3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1", "v7"),
            "not pinned",
        ),
        (lambda s: s.replace("  # v7.0.1", ""), "no '# vX.Y.Z' comment"),
        (lambda s: s.replace("permissions: {}\n", ""), "no top-level permissions"),
        (lambda s: s.replace("  push:", "  pull_request_target:"), "pull_request_target"),
    ],
)
def test_detector_flags_each_violation(
    tmp_path: Path, mutate: Callable[[str], str], expected: str
) -> None:
    found = violations(_write(tmp_path, mutate(_GOOD)))
    assert any(expected in f for f in found), found


_PLANTS = [
    ("      - {uses: actions/checkout@v7}\n", "parsed uses"),
    ('      - "uses": actions/checkout@v7\n', "not pinned"),
    ("      - uses : actions/checkout@v7\n", "not pinned"),
    ("      - uses:\n          actions/checkout@v7\n", "not on the same line"),
]


@pytest.mark.parametrize(("plant", "expected"), _PLANTS)
def test_detector_catches_unusual_uses_spellings(tmp_path: Path, plant: str, expected: str) -> None:
    found = violations(_write(tmp_path, _GOOD + plant))
    assert any(expected in f for f in found), found


def test_detector_rejects_string_permissions(tmp_path: Path) -> None:
    job_level = _GOOD.replace(
        "    runs-on: ubuntu-latest\n", "    runs-on: ubuntu-latest\n    permissions: write-all\n"
    )
    assert any("job j permissions" in f for f in violations(_write(tmp_path, job_level)))
    top_level = _GOOD.replace("permissions: {}", "permissions: read-all")
    assert any("top-level permissions" in f for f in violations(_write(tmp_path, top_level)))


def test_ci_postgres_matches_the_shipped_compose_image() -> None:
    compose = yaml.safe_load((WORKFLOWS_DIR.parents[1] / "docker-compose.yml").read_text())
    shipped = compose["services"]["postgres"]["image"]
    ci = yaml.safe_load((WORKFLOWS_DIR / "pre-commit.yml").read_text())
    tested = ci["jobs"]["backend-tests"]["services"]["postgres"]["image"]
    assert tested == shipped


def test_ratchet_fails_when_a_fenced_file_is_compliant(tmp_path: Path) -> None:
    _write(tmp_path, _GOOD)
    assert check_directory(tmp_path, {"w.yml": "reason"}) == [
        "w.yml: already compliant, remove it from FENCED"
    ]
