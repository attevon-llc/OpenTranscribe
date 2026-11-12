"""Commit-stage hooks skipped in CI must be fully covered by a push-stage hook.

``.github/workflows/pre-commit.yml`` runs BOTH stages on the same tree (separate matrix
legs), so the commit-stage leg skips hooks the push-stage leg supersedes via ``SKIP``
(https://pre-commit.com/#temporarily-disabling-hooks). Each skipped id must appear in
``SUPERSEDED`` and the superseding hook must check the same files with the same script,
minus only the narrowing flags allow-listed here.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG = REPO_ROOT / ".pre-commit-config.yaml"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pre-commit.yml"

#: skipped commit-stage hook -> (superseding push-stage hook, narrowing tokens only the skipped one has)
SUPERSEDED: dict[str, tuple[str, set[str]]] = {
    "frontend-check": ("frontend-build", {"--check-only", "--related-only"}),
    "bandit": ("bandit-recursive", set()),
    # narrowing = text removed from the skipped entry; staged tier passes the file list through "$@"; the whole-tree tier takes none
    "guard-stylelint-staged": ("guard-stylelint", {' "$@"\' --'}),
}


def _hooks(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [h for repo in config["repos"] for h in repo["hooks"]]


def _find(hooks: list[dict[str, Any]], hook_id: str, stage: str) -> dict[str, Any]:
    for h in hooks:
        if (h["id"] == hook_id or h.get("alias") == hook_id) and stage in h.get("stages", []):
            return h
    raise AssertionError(f"no {stage} hook {hook_id!r}")


def _tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    return out.stdout.splitlines()


def _check(config: dict[str, Any], skip_ids: list[str], files: list[str]) -> list[str]:
    hooks = _hooks(config)
    errors: list[str] = []
    for sid in skip_ids:
        if sid not in SUPERSEDED:
            errors.append(f"{sid} is skipped but has no SUPERSEDED row")
            continue
        sup_id, narrowing = SUPERSEDED[sid]
        try:
            sub = _find(hooks, sid, "pre-commit")
            sup = _find(hooks, sup_id, "pre-push")
        except AssertionError as exc:
            errors.append(str(exc))
            continue
        sub_re, sup_re = re.compile(sub["files"]), re.compile(sup["files"])
        lost = [f for f in files if sub_re.search(f) and not sup_re.search(f)]
        if lost:
            errors.append(f"{sup_id} misses files {sid} checks: {lost[:3]}")
        sub_t = set(sub.get("types_or", []) + sub.get("types", []))
        sup_t = set(sup.get("types_or", []) + sup.get("types", []))
        if sub_t and not sub_t <= sup_t:
            errors.append(f"{sup_id} types {sup_t} are not a superset of {sub_t}")
        narrowed = str(sub.get("entry", ""))
        for text in narrowing:
            narrowed = narrowed.replace(text, "'" if text.endswith("'") or "'" in text else "")
        if narrowed.split() != str(sup.get("entry", "")).split():
            errors.append(f"{sid} and {sup_id} entries differ beyond {narrowing}")
        if sub.get("additional_dependencies") != sup.get("additional_dependencies"):
            errors.append(f"{sid} and {sup_id} differ in additional_dependencies")
    return errors


def _load() -> tuple[dict[str, Any], dict[str, Any]]:
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return cfg, wf


def test_every_skipped_commit_hook_is_superseded_by_a_push_hook() -> None:
    cfg, wf = _load()
    skip_ids = wf["env"]["SUPERSEDED_COMMIT_HOOKS"].split(",")
    assert set(skip_ids) == set(SUPERSEDED)
    assert _check(cfg, skip_ids, _tracked()) == []


def test_skip_is_only_in_the_commit_stage_leg_and_push_leg_runs_on_same_events() -> None:
    _, wf = _load()
    job = wf["jobs"]["pre-commit-stage"]
    steps = {s["name"]: s for s in job["steps"] if "name" in s}
    commit = steps["Run pre-commit (pre-commit stage)"]
    push = steps["Run pre-commit (pre-push stage)"]
    assert "SUPERSEDED_COMMIT_HOOKS" in commit["env"]["SKIP"]
    assert "SUPERSEDED_COMMIT_HOOKS" not in push["env"]["SKIP"]
    assert "--hook-stage pre-push" in push["run"] and "--all-files" in push["run"]
    assert commit["if"] == "matrix.stage == 'pre-commit'"
    assert push["if"] == "matrix.stage == 'pre-push'"
    assert set(job["strategy"]["matrix"]["stage"]) >= {"pre-commit", "pre-push"}


def test_must_fire_when_superseding_hook_loses_coverage() -> None:
    cfg, _ = _load()
    for h in _hooks(cfg):
        if h["id"] == "frontend-build":
            h["files"] = r"^frontend/src/lib/"
    assert _check(cfg, ["frontend-check"], _tracked())


def test_must_fire_on_skip_id_not_in_table() -> None:
    cfg, _ = _load()
    assert _check(cfg, ["ruff"], [])
