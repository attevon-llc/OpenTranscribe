"""Structural guards for the CodeQL workflow and its config file."""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
WORKFLOW = REPO / ".github" / "workflows" / "codeql.yml"
CONFIG = REPO / ".github" / "codeql" / "codeql-config.yml"
SHA_RE = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


def _load(path: Path) -> dict[Any, Any]:
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict)
    return data


def _triggers(wf: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML (YAML 1.1) parses the bare key `on` as boolean True.
    on = wf.get("on", wf.get(True))
    assert isinstance(on, dict)
    return on


def _steps(wf: dict[Any, Any]) -> list[dict[str, Any]]:
    return list(wf["jobs"]["analyze"]["steps"])


def test_triggers_are_exactly_the_expected_set() -> None:
    on = _triggers(_load(WORKFLOW))
    assert set(on) == {"push", "pull_request", "merge_group", "schedule", "workflow_dispatch"}
    assert on["push"]["branches"] == ["master"]
    assert on["pull_request"]["branches"] == ["master", "release/**"]
    assert len(on["schedule"]) == 1 and on["schedule"][0]["cron"]


def test_no_pull_request_target() -> None:
    assert "pull_request_target" not in _triggers(_load(WORKFLOW))
    code = [ln for ln in WORKFLOW.read_text().splitlines() if not ln.lstrip().startswith("#")]
    assert not any("pull_request_target" in ln for ln in code)


def test_languages_and_build_mode() -> None:
    matrix = _load(WORKFLOW)["jobs"]["analyze"]["strategy"]["matrix"]["include"]
    assert {(m["language"], m["build-mode"]) for m in matrix} == {
        ("python", "none"),
        ("javascript-typescript", "none"),
        ("actions", "none"),
    }


def test_permissions_are_minimal() -> None:
    wf = _load(WORKFLOW)
    assert wf["permissions"] == {}
    assert wf["jobs"]["analyze"]["permissions"] == {
        "contents": "read",
        "security-events": "write",
    }


def test_every_action_is_pinned_by_full_sha() -> None:
    uses = [s["uses"] for s in _steps(_load(WORKFLOW)) if "uses" in s]
    assert len(uses) == 3
    for ref in uses:
        assert SHA_RE.match(ref), ref


def test_checkout_does_not_persist_credentials() -> None:
    checkouts = [
        s for s in _steps(_load(WORKFLOW)) if s.get("uses", "").startswith("actions/checkout@")
    ]
    assert len(checkouts) == 1
    assert checkouts[0]["with"]["persist-credentials"] is False


def test_init_uses_config_file_that_exists_and_security_extended() -> None:
    init = next(s for s in _steps(_load(WORKFLOW)) if "codeql-action/init@" in s["uses"])
    assert init["with"]["queries"] == "security-extended"
    assert (REPO / init["with"]["config-file"]).is_file()


def test_pr_runs_cancel_superseded_runs() -> None:
    conc = _load(WORKFLOW)["concurrency"]
    assert "github.ref" in conc["group"]
    assert "pull_request" in str(conc["cancel-in-progress"])


def test_paths_ignore_entries_each_have_a_reason_comment() -> None:
    lines = CONFIG.read_text().splitlines()
    entries = [i for i, ln in enumerate(lines) if ln.startswith("  - ")]
    assert entries, "paths-ignore must not be empty"
    parsed = _load(CONFIG)["paths-ignore"]
    assert len(parsed) == len(entries)
    for i in entries:
        above = lines[i - 1].strip()
        assert above.startswith("#") and len(above) > 10, f"no reason above: {lines[i]}"


def test_paths_ignore_entries_exist_or_are_globs() -> None:
    entries = _load(CONFIG)["paths-ignore"]
    assert entries
    # backend/mutants is created on demand by the mutation harness; globs match nothing static.
    stale = [
        e for e in entries if "*" not in e and e != "backend/mutants" and not (REPO / e).exists()
    ]
    assert stale == []


@pytest.mark.parametrize("path", [WORKFLOW, CONFIG])
def test_files_parse(path: Path) -> None:
    assert _load(path)


def test_analyze_job_has_a_bounded_timeout() -> None:
    timeout = _load(WORKFLOW)["jobs"]["analyze"]["timeout-minutes"]
    assert isinstance(timeout, int) and 0 < timeout <= 60
