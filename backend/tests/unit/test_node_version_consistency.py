"""Every Node.js build stage in this repo must agree on one, non-EOL major (issue #780).

This exists because they silently didn't. `docs-site/Dockerfile` shipped `node:20-alpine`
— Node 20 went EOL 2026-04-30 — while `frontend/Dockerfile.prod` had already moved to
`node:26-alpine` in `fd491be7f`, an unreviewed Dependabot robot bump. That commit is the
whole mechanism: Dependabot's `docker` ecosystem rewrites `FROM` lines and nothing else, so
it bumped the one `FROM` line it watches and left `frontend/.nvmrc` (still `24`), three
workflow `node-version:` literals, and a stale parity comment in
`.github/workflows/pre-commit.yml` behind. `docs-site/Dockerfile` was not watched by
Dependabot at all until this issue added a `docker` / `/docs-site` entry to
`.github/dependabot.yml` — so it just... stayed on 20, indefinitely, with nothing red to
show for it.

Two properties made the drift invisible rather than merely present:

* **The stale Node 20 build stage is discarded.** `docs-site/Dockerfile` is a multi-stage
  build (`COPY --from=build ... AS build` into an `nginx:alpine` runtime stage), so the EOL
  Node stage never shipped in the image or its attack surface. A Trivy/Grype scan of the
  *published* image cannot see it — there is nothing there to flag. The only place this bug
  was ever visible was the Dockerfile source itself.
* **`node-version-file` collapses 5 workflow call sites down to 2 files, but cannot see a
  `FROM` line, an `engines` floor, or a prose comment** — precisely the surface Dependabot's
  `docker` ecosystem moves. Fixing the 5 workflow literals (this issue's A1) does nothing to
  stop the next robot bump from re-splitting `docs-site/Dockerfile` against
  `frontend/Dockerfile.prod` exactly as `fd491be7f` did the reverse.

So this is the actual deliverable, not the one-time version bump: a gate that makes the
*next* accidental split fail loudly, on the PR that causes it, instead of drifting silently
for months. This test is EXPECTED to fail a future Dependabot PR that bumps only
`docs-site/Dockerfile`'s base image (or only `frontend/Dockerfile.prod`'s) — that is the
point. When it does, bump the sibling files in the same PR rather than loosening this test.

Four independent checks, each with its own must-fire and must-stay-clean case so a detector
that silently matches nothing cannot be mistaken for a clean tree (the repo's own rule,
see `backend/tests/CLAUDE.md`'s "Four tools that keep the suite honest"):

1. Every `FROM node:<N>-alpine` major across the three Dockerfiles that build a Node stage
   agrees.
2. Both `.nvmrc` files declare that same major.
3. No `actions/setup-node` step anywhere under `.github/workflows/` uses a literal
   `node-version:` — every one must use `node-version-file:`, and the file it names must
   exist. (Without this half, a re-added literal could sit right beside a correct
   `node-version-file:` step and this test would still see 5 files agree.)
4. That major is not End-of-Life as of today, checked against a small in-repo table with one
   line per Node major this repo has ever run. (Without this half, three files agreeing on
   `20` — the exact bug this issue fixes — would pass checks 1-2 vacuously.)

Style follows `backend/tests/unit/test_precommit_stage_ci_parity.py`: parse the real config
with `yaml.safe_load`, raise `AssertionError` (not `pytest.fail`) from helpers so they stay
usable outside pytest, and pin a must-fire/must-stay-clean pair per detector.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The Dockerfiles that build a Node stage. `docs-site/Dockerfile`'s Node stage is discarded
#: by `COPY --from=build` into an nginx runtime image; `frontend/Dockerfile.dev` and
#: `frontend/Dockerfile.prod`'s build stage both ship in what they produce. All three must
#: still agree — a discarded stage is invisible to a vulnerability scan, not to a developer
#: reading three Dockerfiles that disagree about "the" Node version.
NODE_DOCKERFILES: tuple[Path, ...] = (
    REPO_ROOT / "docs-site" / "Dockerfile",
    REPO_ROOT / "frontend" / "Dockerfile.prod",
    REPO_ROOT / "frontend" / "Dockerfile.dev",
)

NVMRC_FILES: tuple[Path, ...] = (
    REPO_ROOT / "docs-site" / ".nvmrc",
    REPO_ROOT / "frontend" / ".nvmrc",
)

WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

#: Node.js major -> published End-of-Life date. One line per major this repo has ever
#: targeted; add the NEXT major here BEFORE bumping to it, so this test already has an
#: opinion on the day the bump lands rather than discovering EOL months later. Source:
#: https://nodejs.org/en/about/previous-releases (each even major's LTS End-of-Life date).
NODE_EOL: dict[int, date] = {
    18: date(2025, 4, 30),
    20: date(2026, 4, 30),
    22: date(2027, 4, 30),
    24: date(2028, 4, 30),
    26: date(2029, 4, 30),
}

#: Matches a Dockerfile `FROM node:<major>[.minor[.patch]]-alpine` build-stage line. Anchored
#: on the literal `node:` image name so it does not match `FROM nginx:...`,
#: `FROM emscripten/emsdk:...`, or an internal stage alias like `FROM ffmpeg-base AS ...`.
FROM_NODE_RE = re.compile(r"^FROM\s+node:(\d+)(?:\.\d+){0,2}-alpine\b", re.MULTILINE)


def _node_major_from_dockerfile(path: Path) -> int:
    """Return the Node major pinned by ``path``'s ``FROM node:<major>-alpine`` line.

    Raises ``AssertionError`` (not a bare regex failure) so a missing or renamed Dockerfile,
    or a `FROM` line that stops matching the pattern entirely, reads as this test's own
    finding instead of an opaque ``NoneType`` error deep in a helper.
    """
    if not path.is_file():
        raise AssertionError(f"expected Dockerfile is missing: {path}")
    text = path.read_text(encoding="utf-8")
    matches = FROM_NODE_RE.findall(text)
    if not matches:
        raise AssertionError(
            f"{path} has no `FROM node:<major>-alpine` line — expected exactly one Node "
            "build stage. If this Dockerfile stopped building a Node stage on purpose, "
            "remove it from NODE_DOCKERFILES with a written reason."
        )
    if len(matches) > 1:
        raise AssertionError(
            f"{path} has {len(matches)} `FROM node:<major>-alpine` lines "
            f"({matches}) — expected exactly one."
        )
    return int(matches[0])


def _read_nvmrc(path: Path) -> int:
    """Return the Node major declared by an ``.nvmrc`` file."""
    if not path.is_file():
        raise AssertionError(f"expected .nvmrc is missing: {path}")
    content = path.read_text(encoding="utf-8").strip()
    if not content.isdigit():
        raise AssertionError(f"{path} does not contain a bare major version: {content!r}")
    return int(content)


def _load_yaml(path: Path) -> dict[str, Any]:
    """Parse a YAML file, failing with the path when it is unreadable or not a mapping."""
    if not path.is_file():
        raise AssertionError(f"expected file is missing: {path}")
    with path.open(encoding="utf-8") as handle:
        loaded: object = yaml.safe_load(handle)
    if not isinstance(loaded, dict):
        raise AssertionError(f"{path} did not parse as a YAML mapping")
    return {str(key): value for key, value in loaded.items()}


def _setup_node_steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every step's ``with`` block for a step whose ``uses`` is ``actions/setup-node``."""
    steps: list[dict[str, Any]] = []
    jobs = workflow.get("jobs") or {}
    for job in jobs.values():
        for step in job.get("steps") or []:
            uses = str(step.get("uses") or "")
            if uses.startswith("actions/setup-node@"):
                steps.append(dict(step.get("with") or {}))
    return steps


def _setup_node_findings(workflow_path: Path) -> list[str]:
    """Return one message per ``setup-node`` step in ``workflow_path`` that is not clean.

    "Clean" means: no literal ``node-version:`` key, exactly one ``node-version-file:``
    key, and that file exists relative to the repo root. `node-version` and
    `node-version-file` must never both be set on the same step (the tool accepts it and
    `node-version-file` silently wins, which would hide a stale literal forever instead of
    the two ever visibly disagreeing).
    """
    workflow = _load_yaml(workflow_path)
    findings: list[str] = []
    for with_block in _setup_node_steps(workflow):
        if "node-version" in with_block:
            findings.append(
                f"{workflow_path.name}: setup-node step uses a literal "
                f"node-version: {with_block['node-version']!r} instead of node-version-file"
            )
            continue
        version_file = with_block.get("node-version-file")
        if not version_file:
            findings.append(
                f"{workflow_path.name}: setup-node step sets neither node-version nor "
                "node-version-file"
            )
            continue
        if not (REPO_ROOT / str(version_file)).is_file():
            findings.append(
                f"{workflow_path.name}: node-version-file target does not exist: {version_file}"
            )
    return findings


# ---------------------------------------------------------------------------
# 1. Dockerfile FROM lines agree
# ---------------------------------------------------------------------------


def test_node_dockerfiles_agree_on_major() -> None:
    """All Node build-stage Dockerfiles must pin the same major."""
    majors = {path: _node_major_from_dockerfile(path) for path in NODE_DOCKERFILES}
    distinct = set(majors.values())
    assert len(distinct) == 1, (
        "Node build-stage Dockerfiles disagree on major version: "
        f"{ {str(p.relative_to(REPO_ROOT)): m for p, m in majors.items()} }. "
        "This is exactly issue #780: a robot dependency bump touches one FROM line at a "
        "time. Bump every file in NODE_DOCKERFILES together."
    )


def test_from_node_regex_must_fire_on_a_real_mismatch(tmp_path: Path) -> None:
    """Must-fire control: two Dockerfiles pinning different majors are caught.

    Without this, a regex that silently matches nothing (e.g. after a Dockerfile syntax
    change) would leave `test_node_dockerfiles_agree_on_major` vacuously green forever.
    """
    old_stage = tmp_path / "Dockerfile.old"
    old_stage.write_text("FROM node:20-alpine AS build\nWORKDIR /app\n", encoding="utf-8")
    new_stage = tmp_path / "Dockerfile.new"
    new_stage.write_text("FROM node:26-alpine AS build\nWORKDIR /app\n", encoding="utf-8")

    majors = {_node_major_from_dockerfile(old_stage), _node_major_from_dockerfile(new_stage)}
    assert majors == {20, 26}, "regex failed to detect a real major-version mismatch"


def test_from_node_regex_must_stay_clean_on_non_node_from_lines(tmp_path: Path) -> None:
    """Must-stay-clean control: other FROM lines in a multi-stage build must not be matched.

    `frontend/Dockerfile.prod` has six FROM lines; only one names a Node image. A regex
    that over-matches `FROM nginx:...` or `FROM ffmpeg-base AS ...` as a Node stage would
    make check 1 compare the wrong things and could report a false mismatch or, worse, a
    false agreement.
    """
    multi_stage = tmp_path / "Dockerfile.multi"
    multi_stage.write_text(
        "FROM emscripten/emsdk:3.1.40 AS emsdk-base\n"
        "FROM emsdk-base AS ffmpeg-base\n"
        "FROM node:26-alpine AS build\n"
        "FROM nginx:1.31.2-alpine3.23\n",
        encoding="utf-8",
    )
    assert _node_major_from_dockerfile(multi_stage) == 26


# ---------------------------------------------------------------------------
# 2. .nvmrc files agree with the Dockerfiles
# ---------------------------------------------------------------------------


def test_nvmrc_files_match_dockerfile_major() -> None:
    """Both `.nvmrc` files must declare the same major the Dockerfiles pin."""
    dockerfile_major = _node_major_from_dockerfile(NODE_DOCKERFILES[0])
    for nvmrc in NVMRC_FILES:
        nvmrc_major = _read_nvmrc(nvmrc)
        assert nvmrc_major == dockerfile_major, (
            f"{nvmrc.relative_to(REPO_ROOT)} declares Node {nvmrc_major} but "
            f"{NODE_DOCKERFILES[0].relative_to(REPO_ROOT)} pins Node {dockerfile_major}. "
            "This is the exact P10 drift: frontend/.nvmrc read 24 while "
            "frontend/Dockerfile.prod had already moved to node:26-alpine."
        )


def test_read_nvmrc_must_fire_on_a_mismatched_value(tmp_path: Path) -> None:
    """Must-fire control: a stale .nvmrc value is a real, detectable mismatch."""
    stale = tmp_path / ".nvmrc"
    stale.write_text("24\n", encoding="utf-8")
    assert _read_nvmrc(stale) != 26


def test_read_nvmrc_must_stay_clean_on_a_matching_value(tmp_path: Path) -> None:
    """Must-stay-clean control: an up-to-date .nvmrc value parses and matches."""
    current = tmp_path / ".nvmrc"
    current.write_text("26\n", encoding="utf-8")
    assert _read_nvmrc(current) == 26


# ---------------------------------------------------------------------------
# 3. No workflow setup-node step uses a literal node-version
# ---------------------------------------------------------------------------


def test_no_workflow_uses_a_literal_node_version() -> None:
    """Every `actions/setup-node` step must use `node-version-file`, never a literal.

    Without this, someone re-adding `node-version: '24'` beside an otherwise-correct
    `node-version-file:` step elsewhere would leave checks 1-2 green while a workflow
    silently drifted back onto a hardcoded (and eventually stale) version.
    """
    offenders: list[str] = []
    for workflow_path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        offenders.extend(_setup_node_findings(workflow_path))
    assert not offenders, "setup-node literal/missing node-version-file findings:\n" + "\n".join(
        offenders
    )


def test_setup_node_findings_must_fire_on_a_literal_version(tmp_path: Path) -> None:
    """Must-fire control: a literal `node-version:` is reported."""
    workflow = tmp_path / "bad.yml"
    workflow.write_text(
        "jobs:\n"
        "  build:\n"
        "    steps:\n"
        "      - uses: actions/setup-node@v7\n"
        "        with:\n"
        "          node-version: '24'\n",
        encoding="utf-8",
    )
    findings = _setup_node_findings(workflow)
    assert findings and "literal" in findings[0]


def test_setup_node_findings_must_fire_on_a_missing_version_file_target(tmp_path: Path) -> None:
    """Must-fire control: a `node-version-file` pointing at a file that does not exist."""
    workflow = tmp_path / "bad.yml"
    workflow.write_text(
        "jobs:\n"
        "  build:\n"
        "    steps:\n"
        "      - uses: actions/setup-node@v7\n"
        "        with:\n"
        "          node-version-file: does/not/exist/.nvmrc\n",
        encoding="utf-8",
    )
    findings = _setup_node_findings(workflow)
    assert findings and "does not exist" in findings[0]


def test_setup_node_findings_must_stay_clean_on_a_valid_version_file(tmp_path: Path) -> None:
    """Must-stay-clean control: a correct `node-version-file:` step reports nothing."""
    workflow = tmp_path / "good.yml"
    workflow.write_text(
        "jobs:\n"
        "  build:\n"
        "    steps:\n"
        "      - uses: actions/setup-node@v7\n"
        "        with:\n"
        "          node-version-file: frontend/.nvmrc\n",
        encoding="utf-8",
    )
    assert _setup_node_findings(workflow) == []


# ---------------------------------------------------------------------------
# 4. The pinned major is not End-of-Life
# ---------------------------------------------------------------------------


def test_pinned_node_major_is_not_eol() -> None:
    """The major every file agrees on must not be past its published EOL date.

    Checks 1-2 alone would pass with all files honestly agreeing on `20` — which is
    precisely the bug in issue #780. This is the check that makes agreement insufficient.
    """
    major = _node_major_from_dockerfile(NODE_DOCKERFILES[0])
    eol = NODE_EOL.get(major)
    assert eol is not None, (
        f"Node {major} has no entry in NODE_EOL — add its published End-of-Life date "
        "(https://nodejs.org/en/about/previous-releases) before relying on this check."
    )
    assert date.today() < eol, (
        f"Node {major} reached End-of-Life on {eol.isoformat()} "
        f"({date.today().isoformat()} today). Every Dockerfile/.nvmrc in this repo agrees "
        "on an EOL runtime — bump NODE_DOCKERFILES and NVMRC_FILES to a supported major."
    )


def test_eol_table_must_fire_on_an_eol_major() -> None:
    """Must-fire control: Node 20, EOL 2026-04-30, must read as EOL today.

    Pinned directly against the table rather than `date.today()` mocking, so this stays
    meaningful regardless of when the suite runs relative to Node 20's EOL date -- Node 20
    is unambiguously in the past by the time Node 26 (this repo's target) even exists.
    """
    assert date.today() >= NODE_EOL[20], "Node 20's EOL date must be in the past by now"


def test_eol_table_must_stay_clean_on_a_currently_supported_major() -> None:
    """Must-stay-clean control: a major with a comfortably future EOL date reads as fine."""
    far_future_eol = NODE_EOL[24]
    assert date.today() < far_future_eol
