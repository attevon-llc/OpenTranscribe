"""The dependency-review workflow: evidence artifacts, a licence gate that fails, no secrets.

CI cannot be run before merge, so the contract is checked statically: triggers, per-job token
scope, SHA pins, artifact retention, the documented inputs of the third-party action, and that
every CLI flag the workflow passes to a repo script still exists on that script. A renamed flag
would otherwise only surface as a red job on the first pull request.
`DEPENDENCY_REVIEW_WORKFLOW_PATH` points the checks at another file (used to prove they go red
against deliberately broken copies).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
DEFAULT_PATH = REPO / ".github" / "workflows" / "dependency-review.yml"
WORKFLOW_PATH = Path(os.environ.get("DEPENDENCY_REVIEW_WORKFLOW_PATH", str(DEFAULT_PATH)))

JOBS = {
    "dependency-diff": "Dependency diff",
    "dependency-review": "Dependency review",
    "license-gate": "License gate",
}
ARTIFACTS = {"dependency-diff": "dependency-diff", "license-gate": "license-evidence"}
REVIEW_ACTION = "actions/dependency-review-action"
# Inputs documented in the action's action.yml; a typo is silently ignored by GitHub.
REVIEW_INPUTS = {
    "repo-token", "fail-on-severity", "fail-on-scopes", "base-ref", "head-ref", "config-file",
    "allow-licenses", "deny-licenses", "allow-dependencies-licenses", "allow-ghsas",
    "external-repo-token", "license-check", "vulnerability-check", "comment-summary-in-pr",
    "deny-packages", "deny-groups", "retry-on-snapshot-warnings",
    "retry-on-snapshot-warnings-timeout", "warn-only", "show-openssf-scorecard",
    "warn-on-openssf-scorecard-level", "show-patched-versions",
}  # fmt: skip
_PIN_RE = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
_VERSION_COMMENT_RE = re.compile(r"#\s*v\d+\.\d+\.\d+\s*$")
_SCRIPT_RE = re.compile(r"scripts/([\w-]+\.py)")
_FLAG_RE = re.compile(r"(?<![\w-])(--[a-z][a-z-]*)")


def _load() -> dict[Any, Any]:
    doc = yaml.safe_load(WORKFLOW_PATH.read_text())
    assert isinstance(doc, dict)
    return doc


def _triggers(doc: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML (YAML 1.1) parses the bare key `on` as boolean True.
    raw = doc["on"] if "on" in doc else doc[True]
    assert isinstance(raw, dict)
    return raw


def _job(doc: dict[Any, Any], key: str) -> dict[str, Any]:
    job = doc["jobs"][key]
    assert isinstance(job, dict)
    return job


def _steps(doc: dict[Any, Any], key: str) -> list[dict[str, Any]]:
    steps = _job(doc, key)["steps"]
    assert isinstance(steps, list)
    return steps


def _all_steps(doc: dict[Any, Any]) -> list[dict[str, Any]]:
    return [s for key in doc["jobs"] for s in _steps(doc, key)]


def _uses(step: dict[str, Any], action: str) -> bool:
    return str(step.get("uses", "")).startswith(action + "@")


@cache
def _script_flags(script: str) -> frozenset[str]:
    out = subprocess.run(
        [sys.executable, str(REPO / "scripts" / script), "--help"],
        capture_output=True, text=True, check=True, cwd=REPO,
    ).stdout  # fmt: skip
    return frozenset(_FLAG_RE.findall(out))


def _command_flags(run: str) -> set[str]:
    """Flags in a run block that are passed to a repo script (git/pip lines are not ours)."""
    joined = re.sub(r"\\\n", " ", run)
    flags: set[str] = set()
    for line in joined.splitlines():
        if re.search(r"\bgit\b|pip install", line):
            continue
        flags.update(_FLAG_RE.findall(line))
    return flags


def test_workflow_file_exists() -> None:
    assert WORKFLOW_PATH.is_file(), WORKFLOW_PATH


def test_triggers_are_exactly_pr_and_merge_group() -> None:
    triggers = _triggers(_load())
    assert set(triggers) == {"pull_request", "merge_group"}
    assert triggers["pull_request"]["branches"] == ["master", "release/**"]
    assert "pull_request_target" not in triggers


def test_jobs_are_the_three_named_checks() -> None:
    doc = _load()
    assert {k: _job(doc, k)["name"] for k in doc["jobs"]} == JOBS


def test_top_level_permissions_empty_and_jobs_read_contents_only() -> None:
    doc = _load()
    assert doc["permissions"] == {}
    for key in JOBS:
        assert _job(doc, key)["permissions"] == {"contents": "read"}, key


def test_cancels_superseded_pr_runs_but_never_a_queue_entry() -> None:
    concurrency = _load()["concurrency"]
    assert "github.event.pull_request.number" in concurrency["group"]
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


def test_default_shell_is_bash() -> None:
    assert _load()["defaults"]["run"]["shell"] == "bash"


def test_every_uses_is_sha_pinned_with_a_version_comment() -> None:
    refs = [
        line
        for line in WORKFLOW_PATH.read_text().splitlines()
        if re.match(r"^\s*(?:-\s+)?uses\s*:", line)
    ]
    assert refs
    for line in refs:
        value = line.split(":", 1)[1].split("#", 1)[0].strip()
        assert _PIN_RE.match(value), line
        assert _VERSION_COMMENT_RE.search(line), line


def test_every_checkout_drops_credentials() -> None:
    checkouts = [s for s in _all_steps(_load()) if _uses(s, "actions/checkout")]
    assert len(checkouts) == len(JOBS)
    for step in checkouts:
        assert step["with"]["persist-credentials"] is False


def test_diff_job_checks_out_full_history_on_python_313() -> None:
    steps = _steps(_load(), "dependency-diff")
    checkout = next(s for s in steps if _uses(s, "actions/checkout"))
    assert checkout["with"]["fetch-depth"] == 0
    python = next(s for s in steps if _uses(s, "actions/setup-python"))
    assert python["with"]["python-version"] == "3.13"


@pytest.mark.parametrize("job,name", sorted(ARTIFACTS.items()))
def test_artifact_is_uploaded_with_retention_and_fails_when_empty(job: str, name: str) -> None:
    uploads = [s for s in _steps(_load(), job) if _uses(s, "actions/upload-artifact")]
    assert len(uploads) == 1
    with_ = uploads[0]["with"]
    assert with_["name"] == name
    assert with_["retention-days"] == 90
    assert with_["if-no-files-found"] == "error"


def test_dependency_review_action_inputs() -> None:
    steps = [s for s in _steps(_load(), "dependency-review") if _uses(s, REVIEW_ACTION)]
    assert len(steps) == 1
    with_ = steps[0]["with"]
    assert set(with_) <= REVIEW_INPUTS, set(with_) - REVIEW_INPUTS
    assert with_["fail-on-severity"] == "high"
    assert with_["fail-on-scopes"] == "runtime"
    assert with_["vulnerability-check"] is True
    # Licences have one policy home (the License gate job).
    assert with_["license-check"] is False
    assert with_["show-openssf-scorecard"] is True
    # A PR comment needs pull-requests: write, which a fork PR never gets.
    assert with_["comment-summary-in-pr"] == "never"
    # Adoption phase: evidence only. Flipping this is a deliberate edit of this test.
    assert with_["warn-only"] is True


def test_dependency_review_is_skipped_for_merge_group() -> None:
    assert _job(_load(), "dependency-review")["if"] == "github.event_name == 'pull_request'"


@pytest.mark.parametrize("job", ["dependency-diff", "license-gate"])
def test_evidence_jobs_write_a_step_summary(job: str) -> None:
    runs = [str(s.get("run", "")) for s in _steps(_load(), job)]
    assert any("$GITHUB_STEP_SUMMARY" in r for r in runs)


def test_review_job_leaves_the_summary_to_the_action() -> None:
    # The action writes its own summary; copying `comment-content` through env: fails with
    # E2BIG above 128 KiB and shows the same table twice.
    steps = _steps(_load(), "dependency-review")
    assert [s for s in steps if "run" in s] == []
    assert "comment-content" not in WORKFLOW_PATH.read_text().replace("# ", "")


@pytest.mark.parametrize("job", sorted(JOBS))
def test_every_job_has_a_timeout(job: str) -> None:
    # The default is 360 minutes; a hung pip dry-run or API call must not hold a runner that long.
    minutes = _job(_load(), job)["timeout-minutes"]
    assert isinstance(minutes, int)
    assert 5 <= minutes <= 30


def test_no_expression_reaches_a_run_block() -> None:
    runs = [str(s["run"]) for s in _all_steps(_load()) if "run" in s]
    assert runs
    assert [r for r in runs if "${{" in r] == []


def test_no_secrets_are_referenced() -> None:
    text = WORKFLOW_PATH.read_text()
    assert "secrets." not in text
    assert "pull_request_target" not in text


def test_licence_gate_is_not_report_only() -> None:
    steps = _steps(_load(), "license-gate")
    gate = next(s for s in steps if "scripts/check-licenses.py" in str(s.get("run", "")))
    assert "continue-on-error" not in gate
    assert "|| true" not in gate["run"]
    assert "--verify-report-version" in gate["run"]
    assert "continue-on-error" not in _job(_load(), "license-gate")


def test_licence_gate_reads_the_three_shipped_reports_it_needs() -> None:
    steps = _steps(_load(), "license-gate")
    resolve = next(s for s in steps if "check-requirements-resolve.py" in str(s.get("run", "")))
    run = re.sub(r"\\\n", " ", resolve["run"])
    for name in ("requirements.txt", "requirements-nodeps.txt", "requirements-lite.txt"):
        assert f"--only {name}" in run
    assert "--report-dir pipreports" in run
    assert "--no-drift-check" in run
    gate = next(s for s in steps if "check-licenses.py" in str(s.get("run", "")))
    assert "--pip-report-dir pipreports" in gate["run"]


def test_workflow_invokes_real_scripts_with_flags_that_exist() -> None:
    seen: set[str] = set()
    for step in _all_steps(_load()):
        run = str(step.get("run", ""))
        scripts = set(_SCRIPT_RE.findall(run))
        if not scripts:
            continue
        assert len(scripts) == 1, f"one repo script per step: {step.get('name')}"
        script = scripts.pop()
        assert (REPO / "scripts" / script).is_file(), script
        flags = _command_flags(run)
        assert flags, f"{script} step passes no flags: {step.get('name')}"
        unknown = flags - _script_flags(script)
        assert not unknown, f"{script} has no flag(s) {sorted(unknown)}"
        seen.add(script)
    assert seen == {"dependency-diff.py", "check-requirements-resolve.py", "check-licenses.py"}


def test_diff_job_passes_base_head_and_out_dir() -> None:
    step = next(
        s for s in _steps(_load(), "dependency-diff") if "dependency-diff.py" in str(s.get("run"))
    )
    flags = _command_flags(step["run"])
    assert {"--base", "--head", "--out-dir", "--resolved-base", "--resolved-head"} <= flags


def _step_with(job: str, needle: str) -> dict[str, Any]:
    return next(s for s in _steps(_load(), job) if needle in str(s.get("run", "")))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip


def _pr_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A repo shaped like a PR run: HEAD is a merge commit and the base branch moved on after."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    (repo / "a.txt").write_text("a")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "base")
    base_when_built = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "pr")
    (repo / "b.txt").write_text("b")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-qm", "pr")
    _git(repo, "checkout", "-q", "master")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "pr")
    merge_commit = _git(repo, "rev-parse", "HEAD")
    _git(repo, "branch", "run", merge_commit)
    (repo / "c.txt").write_text("c")
    _git(repo, "add", "c.txt")
    _git(repo, "commit", "-qm", "master moved on")
    _git(repo, "checkout", "-q", "run")
    return repo, base_when_built, merge_commit


def _run_base_ref_step(repo: Path, tmp_path: Path, **env: str) -> str:
    step = _step_with("dependency-diff", "BASE_REF")
    github_env = tmp_path / "github_env"
    github_env.write_text("")
    subprocess.run(
        ["bash", "-eo", "pipefail", "-c", step["run"]],
        cwd=repo, check=True, capture_output=True, text=True,
        env={**os.environ, "GITHUB_ENV": str(github_env), **env},
    )  # fmt: skip
    (line,) = github_env.read_text().splitlines()
    assert line.startswith("BASE_REF=")
    return line.removeprefix("BASE_REF=")


def test_pull_request_base_is_the_merge_commits_first_parent_not_the_moving_tip(
    tmp_path: Path,
) -> None:
    repo, base_when_built, _ = _pr_repo(tmp_path)
    base = _run_base_ref_step(repo, tmp_path, EVENT_NAME="pull_request", QUEUE_BASE_SHA="")
    assert base == base_when_built
    assert base != _git(repo, "rev-parse", "master")


def test_merge_group_base_is_the_queue_base_sha(tmp_path: Path) -> None:
    repo, _, _ = _pr_repo(tmp_path)
    base = _run_base_ref_step(repo, tmp_path, EVENT_NAME="merge_group", QUEUE_BASE_SHA="abc123")
    assert base == "abc123"


def test_requirements_change_detection_does_not_pipe_into_grep() -> None:
    # `git diff | grep -q` under pipefail turns a SIGPIPE into "no change" and skips the leg.
    run = str(_step_with("dependency-diff", "check-requirements-resolve.py")["run"])
    assert "git diff --quiet" in run
    assert "grep" not in run


def test_gate_resolve_step_writes_the_reports_where_the_gate_reads_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run the gate's real resolve arguments from a scratch cwd, with pip stubbed but cwd-aware.

    pip runs with cwd=backend/, so a relative --report-dir used to land in backend/<dir>/ and
    the job failed on every run.
    """
    sys.path.insert(0, str(REPO / "scripts" / "lib"))
    import requirements_resolve as rr

    spec = importlib.util.spec_from_file_location(
        "check_requirements_resolve_wf", REPO / "scripts" / "check-requirements-resolve.py"
    )
    assert spec and spec.loader
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)

    def pip(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "pip 25.0 from x", "")
        cwd = Path(str(kwargs["cwd"]))
        report = cwd / command[command.index("--report") + 1]
        if not (cwd / command[-1]).is_file() or not report.parent.is_dir():
            return subprocess.CompletedProcess(command, 1, "", "OSError: pip cannot write report")
        report.write_text(json.dumps({"install": [{"metadata": {"name": "a", "version": "1"}}]}))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(rr.subprocess, "run", pip)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.chdir(scratch)
    run = str(_step_with("license-gate", "check-requirements-resolve.py")["run"])
    argv = shlex.split(re.sub(r"\\\n", " ", run).split("check-requirements-resolve.py", 1)[1])
    assert script.main(argv) == 0
    gate = str(_step_with("license-gate", "check-licenses.py")["run"])
    found = re.search(r"--pip-report-dir (\S+)", gate)
    assert found
    report_dir = scratch / found.group(1)
    assert sorted(p.name for p in report_dir.glob("*.json")) == [
        "requirements-lite.txt.json",
        "requirements-nodeps.txt.json",
        "requirements.txt.json",
    ]
