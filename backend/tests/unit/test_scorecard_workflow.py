"""The Scorecard workflow must satisfy the `publish_results: true` restrictions.

ossf/scorecard-action refuses to publish (and fails the run) when the workflow has a top-level
`env`/`defaults`, a workflow-level write permission, `id-token: write` outside its own job, or
steps outside a short approved list. Those are only discovered on the first real run, so they are
checked statically here. `SCORECARD_WORKFLOW_PATH` points the checks at another file (used to
prove the checks go red against deliberately broken copies).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "scorecard.yml"
WORKFLOW_PATH = Path(os.environ.get("SCORECARD_WORKFLOW_PATH", str(DEFAULT_PATH)))

APPROVED_ACTIONS = {
    "actions/checkout",
    "actions/upload-artifact",
    "github/codeql-action/upload-sarif",
    "ossf/scorecard-action",
    "step-security/harden-runner",
}
JOB_PERMISSIONS = {
    "security-events": "write",
    "id-token": "write",
    "contents": "read",
    "actions": "read",
}
_PIN_RE = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
_VERSION_COMMENT_RE = re.compile(r"#\s*v\d+\.\d+\.\d+\s*$")


def _load() -> dict[Any, Any]:
    doc = yaml.safe_load(WORKFLOW_PATH.read_text())
    assert isinstance(doc, dict)
    return doc


def _triggers(doc: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML (YAML 1.1) parses the bare key `on` as boolean True.
    raw = doc["on"] if "on" in doc else doc[True]
    assert isinstance(raw, dict)
    return raw


def _job(doc: dict[Any, Any]) -> dict[str, Any]:
    jobs = doc["jobs"]
    assert len(jobs) == 1, "exactly one job: id-token: write may only live in the scorecard job"
    job = next(iter(jobs.values()))
    assert isinstance(job, dict)
    return job


def _steps(doc: dict[Any, Any]) -> list[dict[str, Any]]:
    steps = _job(doc)["steps"]
    assert isinstance(steps, list)
    return steps


def test_workflow_file_exists() -> None:
    assert WORKFLOW_PATH.is_file(), WORKFLOW_PATH


def test_triggers_are_the_supported_set() -> None:
    triggers = _triggers(_load())
    assert set(triggers) == {"branch_protection_rule", "schedule", "push", "workflow_dispatch"}
    assert triggers["push"]["branches"] == ["master"]
    assert "pull_request_target" not in triggers
    assert "pull_request" not in triggers


def test_no_top_level_env_or_defaults() -> None:
    doc = _load()
    assert "defaults" not in doc
    assert "env" not in doc


def test_no_job_level_env_defaults_container_or_services() -> None:
    job = _job(_load())
    for key in ("env", "defaults", "container", "services"):
        assert key not in job, key
    assert str(job["runs-on"]).startswith("ubuntu-")


def test_top_level_permissions_grant_nothing() -> None:
    assert _load()["permissions"] == {}


def test_job_permissions_are_exactly_the_documented_set() -> None:
    assert _job(_load())["permissions"] == JOB_PERMISSIONS


def test_every_action_is_approved_and_pinned_by_sha_with_version_comment() -> None:
    lines = {
        m.group("ref"): m.group("line")
        for m in re.finditer(
            r"^(?P<line>\s*(?:-\s+)?uses:\s*(?P<ref>\S+).*)$", WORKFLOW_PATH.read_text(), re.M
        )
    }
    steps = _steps(_load())
    assert steps
    for step in steps:
        assert "run" not in step, "publish_results forbids run: steps in this job"
        ref = step["uses"]
        assert _PIN_RE.match(ref), f"not pinned by full SHA: {ref}"
        assert ref.split("@")[0] in APPROVED_ACTIONS, ref
        assert _VERSION_COMMENT_RE.search(lines[ref]), f"missing # vX.Y.Z comment: {ref}"


def test_checkout_does_not_persist_credentials() -> None:
    checkout = [s for s in _steps(_load()) if s["uses"].startswith("actions/checkout@")]
    assert len(checkout) == 1
    assert checkout[0]["with"]["persist-credentials"] is False


def test_publish_results_matches_documented_intent() -> None:
    text = WORKFLOW_PATH.read_text()
    scorecard = [s for s in _steps(_load()) if s["uses"].startswith("ossf/scorecard-action@")]
    assert len(scorecard) == 1
    with_ = scorecard[0]["with"]
    assert with_["publish_results"] is True
    assert "publish_results is true" in text, "the header comment must state the choice"
    assert with_["results_format"] == "sarif"


def test_results_are_uploaded_as_artifact_and_sarif() -> None:
    steps = _steps(_load())
    artifact = [s for s in steps if s["uses"].startswith("actions/upload-artifact@")]
    sarif = [s for s in steps if s["uses"].startswith("github/codeql-action/upload-sarif@")]
    assert len(artifact) == 1
    assert artifact[0]["with"]["name"] == "scorecard-results"
    assert artifact[0]["with"]["retention-days"] == 5
    assert len(sarif) == 1
    assert sarif[0]["with"]["sarif_file"] == "results.sarif"


def test_no_pull_request_target_anywhere() -> None:
    assert "pull_request_target" not in WORKFLOW_PATH.read_text()


def _guard_allows(ref_name: str, default_branch: str) -> bool:
    """Evaluate the job `if:`; only the `<context> == <context>` form is understood."""
    cond = str(_job(_load()).get("if", ""))
    left, op, right = cond.split()
    assert op == "==", f"unsupported guard expression: {cond!r}"
    values = {"github.event.repository.default_branch": default_branch, "github.ref_name": ref_name}
    return values[left] == values[right]


def test_job_runs_only_on_the_default_branch() -> None:
    assert _guard_allows("master", "master") is True
    assert _guard_allows("feature/x", "master") is False


def test_job_has_a_timeout() -> None:
    timeout = _job(_load())["timeout-minutes"]
    assert isinstance(timeout, int) and 0 < timeout <= 30
