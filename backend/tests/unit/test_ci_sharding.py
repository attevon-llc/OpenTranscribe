"""CI speed (issue #1099) must not cost coverage: sharding is lossless and the required checks keep their names.

Three things are pinned here, each of which was a way for the speed-up to make CI green while
checking less:

* **The split is a partition.** Real ``pytest --splits N --group i`` runs over a synthetic suite
  (the same library, algorithm and xdist mode the workflow uses) must select every test in
  exactly one group. The production-size proof is the ``backend-shard-proof`` job, which
  compares the same manifests over the real suite on every PR.
* **The proof's own checker fails when it should** (a test in two groups, in none, an unknown
  one, an empty group), because a checker that cannot fail would make the proof worthless.
* **The workflow still gates the same things under the same names.** Branch protection
  requires ``Backend Tests (pytest)`` and ``Run Pre-commit Hooks``; once those are matrix or
  aggregate jobs, a required check that no job reports leaves PRs unmergeable, and one that
  ignores a failed leg lets a red shard merge.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND = REPO_ROOT / "backend"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pre-commit.yml"
GUARD = REPO_ROOT / "scripts" / "guards" / "check_shard_coverage.py"
PRECOMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"
DURATIONS = BACKEND / ".test_durations"

pytestmark = pytest.mark.skipif(
    not WORKFLOW.is_file() or not GUARD.is_file(),
    reason="workflow / guard not present in this checkout",
)


def _duplicate_keys(node: yaml.Node, path: str = "") -> list[str]:
    """Mapping keys that occur twice. PyYAML keeps the LAST silently; GitHub rejects the workflow."""
    found: list[str] = []
    if isinstance(node, yaml.MappingNode):
        seen: set[str] = set()
        for key_node, value_node in node.value:
            key = str(getattr(key_node, "value", ""))
            if key in seen:
                found.append(f"{path}/{key} (line {key_node.start_mark.line + 1})")
            seen.add(key)
            found.extend(_duplicate_keys(value_node, f"{path}/{key}"))
    elif isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            found.extend(_duplicate_keys(item, f"{path}[{index}]"))
    return found


def _jobs() -> dict[str, dict]:
    text = WORKFLOW.read_text(encoding="utf-8")
    root = yaml.compose(text)
    assert root is not None
    assert _duplicate_keys(root) == [], "duplicate keys in the workflow"
    jobs: dict[str, dict] = yaml.safe_load(text)["jobs"]
    return jobs


def _run_text(job: dict) -> str:
    return "\n".join(str(s.get("run", "")) for s in job.get("steps", []))


def _guard():
    spec = importlib.util.spec_from_file_location("check_shard_coverage", GUARD)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- the checker -------------------------------------------------------------------------


def test_checker_accepts_a_perfect_partition() -> None:
    full = [f"t::{i}" for i in range(6)]
    assert _guard().check(full, {"a": full[:2], "b": full[2:5], "c": full[5:]}) == []


@pytest.mark.parametrize(
    ("shards", "needle"),
    [
        ({"a": ["t::0", "t::1", "t::2"], "b": ["t::2", "t::3"]}, "more than one shard"),
        ({"a": ["t::0", "t::1"], "b": ["t::3"]}, "run in no shard"),
        ({"a": ["t::0", "t::1", "t::2", "t::3"], "b": ["t::ghost"]}, "absent from the full"),
        ({"a": ["t::0", "t::1", "t::2", "t::3"], "b": []}, "selected no tests"),
    ],
)
def test_checker_fails_each_way_a_split_can_be_lossy(shards, needle) -> None:
    problems = _guard().check([f"t::{i}" for i in range(4)], shards)
    assert any(needle in p for p in problems), problems


def test_checker_exit_status_and_message(tmp_path: Path) -> None:
    full = tmp_path / "full"
    full.write_text("t::0\nt::1\n")
    good_a, good_b = tmp_path / "a", tmp_path / "b"
    good_a.write_text("t::0\n")
    good_b.write_text("t::1\n")
    bad_b = tmp_path / "bad"
    bad_b.write_text("t::0\n")
    ok = subprocess.run(
        [sys.executable, str(GUARD), "--full", str(full), str(good_a), str(good_b)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok.returncode == 0 and "2 tests, each in exactly one of 2 shards" in ok.stdout
    bad = subprocess.run(
        [sys.executable, str(GUARD), "--full", str(full), str(good_a), str(bad_b)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad.returncode == 1 and "more than one shard" in bad.stderr


# --- real pytest-split over a synthetic suite ---------------------------------------------


def _write_suite(root: Path) -> None:
    (root / "pytest.ini").write_text("[pytest]\n")
    for module in range(5):
        body = "import pytest\n\n"
        body += f"@pytest.mark.parametrize('n', range({module + 3}))\n"
        body += "def test_param(n):\n    assert n >= 0\n\n"
        body += "@pytest.mark.xdist_group('shared')\ndef test_grouped():\n    pass\n"
        body += "\nclass TestKlass:\n    def test_a(self):\n        pass\n"
        (root / f"test_mod{module}.py").write_text(body)


def _selected(root: Path, tmp: Path, *extra: str) -> list[str]:
    manifest = tmp / ("m" + "_".join(extra).replace("/", "-") + ".txt")
    env = {**os.environ, "OT_SHARD_MANIFEST": str(manifest)}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(root),
            "-c",
            str(root / "pytest.ini"),
            "--rootdir",
            str(root),
            "-p",
            "tests.shard_manifest",
            "-p",
            "no:cacheprovider",
            "-q",
            "-n",
            "2",
            "--dist",
            "loadgroup",
            *extra,
        ],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=240,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    return manifest.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("splits", [2, 3, 4])
def test_real_pytest_split_runs_every_test_in_exactly_one_group(
    splits: int, tmp_path: Path
) -> None:
    pytest.importorskip("pytest_split", reason="pytest-split is in requirements-ci.txt")
    suite = tmp_path / "suite"
    suite.mkdir()
    _write_suite(suite)
    full = _selected(suite, tmp_path)
    assert len(full) == sum(m + 3 + 2 for m in range(5))  # the control: the manifest is complete
    groups = [
        _selected(
            suite,
            tmp_path,
            "--splits",
            str(splits),
            "--group",
            str(i),
            "--splitting-algorithm",
            "duration_based_chunks",
            "--durations-path",
            str(tmp_path / "none.json"),
        )
        for i in range(1, splits + 1)
    ]
    assert _guard().check(full, {f"group{i + 1}": g for i, g in enumerate(groups)}) == []
    assert sorted(t for g in groups for t in g) == sorted(full)


# --- the workflow ---------------------------------------------------------------------------


def test_every_shard_leg_uses_the_same_split_and_the_matrix_covers_every_group() -> None:
    shard = _jobs()["backend-test-shard"]
    text = _run_text(shard)
    splits = {int(n) for n in re.findall(r"--splits\s+(\d+)", text)}
    assert len(splits) == 1, f"one --splits value expected, found {splits}"
    (n,) = splits
    assert n >= 2
    assert shard["strategy"]["matrix"]["group"] == list(range(1, n + 1)), (
        "the matrix must list every group 1..N: a missing group means those tests never run"
    )
    assert shard["strategy"]["fail-fast"] is False
    assert "--group ${{ matrix.group }}" in text
    assert "duration_based_chunks" in text
    assert "-p tests.shard_manifest" in text
    assert "--cov-fail-under=0" in text, (
        "per-leg floor would fail every leg; it is enforced on the merge"
    )


def test_required_backend_check_keeps_its_name_and_gates_on_every_leg() -> None:
    jobs = _jobs()
    agg = jobs["backend-tests"]
    assert agg["name"] == "Backend Tests (pytest)"
    assert set(agg["needs"]) >= {"changes", "backend-test-shard", "backend-shard-proof"}
    assert "always()" in agg["if"], (
        "without always() a failed shard SKIPS this job, and a skipped required check counts as satisfied"
    )
    assert "needs.changes.outputs.backend == 'true'" in agg["if"]
    text = _run_text(agg)
    assert 'test "$SHARDS" = "success"' in text and '"$PROOF" = "success"' in text
    n = max(jobs["backend-test-shard"]["strategy"]["matrix"]["group"])
    for i in range(1, n + 1):
        assert f"shard-artifacts/shard-{i}.manifest" in text, f"proof ignores shard {i}"
    assert "check_shard_coverage.py" in text
    assert "coverage combine" in text and "coverage report" in text, (
        "the pyproject floor must apply to merged data"
    )
    assert "--fail-under" not in text, "the floor lives in pyproject.toml, not in a second copy"


def test_single_run_backend_steps_still_run_once() -> None:
    steps = {s["name"]: s for s in _jobs()["backend-test-shard"]["steps"] if "name" in s}
    for name in (
        "OpenAPI snapshot is current",
        "Pipeline docs tables are current",
        "Self-test the test auditor",
        "Audit backend test quality",
    ):
        assert steps[name].get("if") == "matrix.group == 1", (
            f"{name} would run N times or not at all"
        )


def test_precommit_legs_cover_every_hook_stage_and_the_required_name_is_kept() -> None:
    jobs = _jobs()
    leg = jobs["pre-commit-stage"]
    config = yaml.safe_load(PRECOMMIT_CONFIG.read_text(encoding="utf-8"))
    stages = {
        s for repo in config["repos"] for hook in repo["hooks"] for s in hook.get("stages", [])
    }
    stages |= set(config.get("default_stages", ["pre-commit"]))
    stages.discard("manual")
    stages.discard("commit-msg")
    assert set(leg["strategy"]["matrix"]["stage"]) == stages, (
        f"matrix legs {leg['strategy']['matrix']['stage']} != hook stages {sorted(stages)}: "
        "a stage with no leg is a stage CI never runs"
    )
    by_name = {s["name"]: s for s in leg["steps"] if "name" in s}
    assert by_name["Run pre-commit (pre-commit stage)"]["if"] == "matrix.stage == 'pre-commit'"
    assert by_name["Run pre-commit (pre-push stage)"]["if"] == "matrix.stage == 'pre-push'"
    agg = jobs["pre-commit"]
    assert agg["name"] == "Run Pre-commit Hooks"
    assert agg["needs"] == "pre-commit-stage" and "always()" in str(agg["if"])
    assert 'test "$RESULT" = "success"' in _run_text(agg)


def test_durations_file_is_valid_and_not_empty() -> None:
    data = json.loads(DURATIONS.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and len(data) > 1000
    assert all(isinstance(k, str) and "::" in k for k in data)
    assert all(isinstance(v, (int, float)) and v >= 0 for v in data.values())


def _workflow() -> dict[str, Any]:
    workflow: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return workflow


def test_frontend_only_hooks_are_skipped_on_a_pr_that_does_not_touch_the_frontend() -> None:
    workflow = _workflow()
    leg = workflow["jobs"]["pre-commit-stage"]
    assert leg["needs"] == "changes"
    config = yaml.safe_load(PRECOMMIT_CONFIG.read_text(encoding="utf-8"))
    hooks = {h["id"]: h for repo in config["repos"] for h in repo["hooks"]}
    skipped = workflow["env"]["FRONTEND_ONLY_HOOKS"].split(",")
    unknown = [h for h in skipped if h not in hooks]
    assert not unknown, f"FRONTEND_ONLY_HOOKS names hooks that do not exist: {unknown}"
    for hook_id in skipped:
        pattern = re.compile(hooks[hook_id]["files"])
        for backend_path in ("backend/app/main.py", "docs-site/docs/x.md", "monitoring/a.yml"):
            assert not pattern.search(backend_path), (
                f"{hook_id} matches {backend_path}; skipping it on a backend-only PR loses coverage"
            )
    steps = {s["name"]: s for s in leg["steps"] if "name" in s}
    for name in ("Run pre-commit (pre-commit stage)", "Run pre-commit (pre-push stage)"):
        skip = steps[name]["env"]["SKIP"]
        assert "needs.changes.outputs.frontend != 'true'" in skip and "FRONTEND_ONLY_HOOKS" in skip
        # paths-filter diffs only the last commit on a manual master run, so it must not skip.
        assert "github.event_name != 'workflow_dispatch'" in skip
    assert "openapi-snapshot" in steps["Run pre-commit (pre-push stage)"]["env"]["SKIP"]


def test_the_frontend_filter_covers_everything_that_can_trigger_a_skipped_hook() -> None:
    changes = _workflow()["jobs"]["changes"]
    filters = next(s for s in changes["steps"] if s.get("id") == "filter")["with"]["filters"]
    frontend = yaml.safe_load(filters)["frontend"]
    for needed in ("frontend/**", ".pre-commit-config.yaml", "scripts/guards/**"):
        assert needed in frontend


def test_the_mypy_and_apt_caches_exist_where_the_time_is_spent() -> None:
    jobs = _workflow()["jobs"]
    stage_steps = {s.get("name"): s for s in jobs["pre-commit-stage"]["steps"]}
    assert ".mypy_cache" in stage_steps["Cache mypy"]["with"]["path"]
    for job in ("backend-test-shard", "backend-fips-tests"):
        steps = jobs[job]["steps"]
        names = [s.get("name") for s in steps]
        assert names.index("Cache apt archives") < names.index("Install system dependencies")
        install = next(s for s in steps if s.get("name") == "Install system dependencies")
        assert 'Dir::Cache::archives="$HOME/apt-archives"' in install["run"]
        assert "chown -R" in install["run"], (
            "apt runs as root, so lock/ and partial/ in the archive dir are unreadable to the "
            "cache post-step's tar and the cache never saves (actions/cache: Failed to save)"
        )
        assert install["run"].index("chown -R") > install["run"].index("apt-get -o Dir::Cache")
