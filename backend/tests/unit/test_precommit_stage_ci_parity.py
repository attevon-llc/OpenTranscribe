"""Every pre-commit stage a hook is assigned to must be invoked by CI (issue #688).

``pre-commit run --all-files`` with no ``--hook-stage`` runs **default-stage hooks
only**, and ``.pre-commit-config.yaml`` sets ``default_stages: [pre-commit]``. So the
moment a hook is demoted to ``stages: [pre-push]`` — which is exactly what #688 did to
the frontend production build and the recursive ``bandit -r backend/`` scan — it stops
running in CI, and the workflow goes **green while checking less**. Nothing about that
failure is visible: no error, no skip line, no missing job.

That is a silent-coverage-loss class, so it gets a gate rather than a paragraph. This
test reads the two files that jointly decide what CI checks and fails when they
disagree:

* ``.pre-commit-config.yaml`` — which stage each hook is assigned to
* ``.github/workflows/pre-commit.yml`` — which stages the workflow actually invokes

⚠️ **Scope, stated honestly:** this checks stages declared *in this repo's config*.
A hook can also carry ``stages`` in its upstream ``.pre-commit-hooks.yaml`` (several
``pre-commit-hooks`` entries such as ``trailing-whitespace`` declare ``pre-push``
there), which no static read of this repo can see. Those are not what a demotion
touches, and they only ever ADD coverage, never remove it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
PRECOMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"
PRECOMMIT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pre-commit.yml"

#: Stages that legitimately have no ``--all-files`` CI invocation, each with the reason.
#: An entry here is a decision, not a backlog item.
EXEMPT_STAGES: dict[str, str] = {
    "commit-msg": (
        "A commit-msg hook is handed the path of a commit message file; there is no "
        "commit message in a `pre-commit run --all-files` sweep, so the stage cannot "
        "be exercised that way. conventional-pre-commit is enforced at commit time on "
        "a developer's machine and by the PR title, not by this workflow."
    ),
    "manual": (
        "`manual` means opt-in by definition — a hook parked there is deliberately not "
        "part of any automatic gate."
    ),
}


def _load_yaml(path: Path) -> dict[str, Any]:
    """Parse a YAML file, failing the test with the path when it is unreadable.

    Raises ``AssertionError`` rather than calling ``pytest.fail``: the mypy hook runs in
    an isolated env with no pytest, so ``pytest.fail`` is untyped there and does not
    narrow as ``NoReturn`` — the ``isinstance`` guard below would then not hold.
    """
    if not path.is_file():
        raise AssertionError(f"expected file is missing: {path}")
    with path.open(encoding="utf-8") as handle:
        loaded: object = yaml.safe_load(handle)
    if not isinstance(loaded, dict):
        raise AssertionError(f"{path} did not parse as a YAML mapping")
    return {str(key): value for key, value in loaded.items()}


def _hooks_with_stages() -> list[tuple[str, tuple[str, ...]]]:
    """Return ``(hook id, stages)`` for every hook declared in the config.

    The id is the ``alias`` when one is set, since that is what identifies the hook
    on the command line and in ``SKIP=``.
    """
    config = _load_yaml(PRECOMMIT_CONFIG)
    default_stages = tuple(config.get("default_stages") or ("pre-commit",))
    hooks: list[tuple[str, tuple[str, ...]]] = []
    for repo in config.get("repos") or []:
        for hook in repo.get("hooks") or []:
            hook_id = hook.get("alias") or hook.get("id")
            stages = tuple(hook.get("stages") or default_stages)
            hooks.append((str(hook_id), stages))
    return hooks


def _ci_invoked_stages() -> set[str]:
    """Return the set of stages ``.github/workflows/pre-commit.yml`` actually runs.

    A ``pre-commit run`` command with no ``--hook-stage`` runs the default stage,
    which is what made the trap invisible — so an unflagged invocation is recorded
    as ``pre-commit``, not as "everything".
    """
    workflow = _load_yaml(PRECOMMIT_WORKFLOW)
    jobs = workflow.get("jobs") or {}
    stages: set[str] = set()
    for job in jobs.values():
        for step in job.get("steps") or []:
            run = str(step.get("run") or "")
            if "pre-commit run" not in run:
                continue
            tokens = run.split()
            if "--hook-stage" in tokens:
                stages.add(tokens[tokens.index("--hook-stage") + 1])
            else:
                stages.add("pre-commit")
    return stages


def test_every_hook_stage_is_invoked_by_ci() -> None:
    """No hook may sit in a stage the pre-commit workflow never runs."""
    invoked = _ci_invoked_stages()
    offenders = [
        f"{hook_id} (stages={list(stages)})"
        for hook_id, stages in _hooks_with_stages()
        if not [s for s in stages if s in invoked or s in EXEMPT_STAGES]
    ]
    assert not offenders, (
        "These hooks run in NO stage that .github/workflows/pre-commit.yml invokes, so "
        f"CI silently stops checking them: {offenders}. Stages CI runs: {sorted(invoked)}. "
        "Add `pre-commit run --all-files --hook-stage <stage>` to the workflow, or "
        "record the stage in EXEMPT_STAGES with a written reason."
    )


def test_ci_runs_the_pre_push_stage() -> None:
    """The pre-push tier must have its own CI invocation.

    Pinned separately from the generic parity check above because deleting the last
    pre-push hook would make that check pass vacuously, and the next demotion would
    then land with no CI coverage and no failing test.
    """
    invoked = _ci_invoked_stages()
    assert "pre-push" in invoked, (
        "`pre-commit run --all-files --hook-stage pre-push` is not in "
        f"{PRECOMMIT_WORKFLOW}; CI runs only {sorted(invoked)}. Anything demoted to "
        "pre-push would stop being checked while the workflow stays green (issue #688)."
    )


def test_install_hook_types_cover_every_declared_stage() -> None:
    """`pre-commit install` must wire every stage the config uses.

    Without ``default_install_hook_types`` covering it, a stage is configured but never
    fires locally: ``pre-commit install`` writes only the ``pre-commit`` hook script, so
    the pre-push and commit-msg tiers exist on paper and run nowhere.
    """
    config = _load_yaml(PRECOMMIT_CONFIG)
    installed = set(config.get("default_install_hook_types") or ["pre-commit"])
    declared = {stage for _, stages in _hooks_with_stages() for stage in stages}
    missing = sorted(declared - installed - {"manual"})
    assert not missing, (
        f"{PRECOMMIT_CONFIG.name} assigns hooks to {missing} but "
        f"default_install_hook_types is {sorted(installed)}, so `pre-commit install` "
        "never wires those git hooks and the tier is dead locally."
    )
