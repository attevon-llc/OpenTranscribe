"""Tests for the planning-id guard in ``scripts/guards/check_process_ids.py``."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "guards" / "check_process_ids.py"
_REPO = _SCRIPT.parents[2]


def _load():
    spec = importlib.util.spec_from_file_location("_guard_process_ids_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load()
REASON = "reword planned"


def _make_repo(tmp_path: Path, files: dict[str, str | bytes]) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    return root


def _run(root: Path, baseline: Path, *extra: str) -> int:
    code: int = guard.main(["--root", str(root), "--baseline", str(baseline), *extra])
    return code


def _scan(root: Path) -> dict[str, int]:
    scanned: tuple[dict[str, int], list[str]] = guard.scan(root)
    found, broken = scanned
    assert broken == []
    return found


def test_planted_python_comment_is_found(tmp_path):
    root = _make_repo(tmp_path, {"backend/app/_probe.py": "x = 1  # W8.2: later\n"})
    assert _scan(root) == {"backend/app/_probe.py": 1}


def test_planted_comment_fails_against_empty_baseline(tmp_path):
    root = _make_repo(tmp_path, {"backend/app/_probe.py": "# W8.2: x\n"})
    assert _run(root, tmp_path / "none.txt") == 1


def test_planted_file_name_is_found(tmp_path):
    root = _make_repo(tmp_path, {"backend/tests/unit/test_wave3_probe.py": "x = 1\n"})
    assert _scan(root) == {"backend/tests/unit/test_wave3_probe.py (file name)": 1}


def test_issue_and_github_pr_references_do_not_match(tmp_path):
    source = "# see #1234 and PR #1234, issue #5\n'''\nDocstring cites PR #77.\n'''\n"
    root = _make_repo(tmp_path, {"backend/app/ok.py": source})
    assert _scan(root) == {}


def test_plan_style_pr_id_matches(tmp_path):
    root = _make_repo(tmp_path, {"backend/app/pr.py": "# lands in PR 7b\n"})
    assert _scan(root) == {"backend/app/pr.py": 1}


def test_word_and_task_patterns_match(tmp_path):
    source = "# wave 3\n# Round 2\n# WP-4\n# audit finding A-7\n# W1.2 Task 5\n"
    root = _make_repo(tmp_path, {"scripts/p.py": source})
    assert _scan(root)["scripts/p.py"] >= 5


def test_string_literal_data_is_ignored(tmp_path):
    source = 'DATA = "wave 3 and W8.2"\nOTHER = """round 4\nfinding 2"""\n'
    root = _make_repo(tmp_path, {"backend/app/data.py": source})
    assert _scan(root) == {}


def test_docstring_is_scanned(tmp_path):
    source = 'def f():\n    """Added in wave 3."""\n'
    root = _make_repo(tmp_path, {"backend/app/doc.py": source})
    assert _scan(root) == {"backend/app/doc.py": 1}


def test_other_languages_use_comment_prefixes(tmp_path):
    root = _make_repo(
        tmp_path,
        {
            "frontend/src/a.ts": "const a = 'wave 3'; // wave 3\n",
            "frontend/src/b.svelte": "<!-- W2.1 -->\n<p>round 2</p>\n",
            "scripts/c.sh": "echo 'wave 9'\n# round 2\n",
            "backend/Dockerfile.x": "RUN true  # WP-1\n",
            ".github/workflows/w.yml": "# wave 4\nname: x\n",
            "backend/pyproject.toml": "# W3.1 note\n[tool]\n",
            ".pre-commit-config.yaml": "repos: []  # round 5\n",
        },
    )
    found = _scan(root)
    assert found == {
        "frontend/src/a.ts": 1,
        "frontend/src/b.svelte": 1,
        "scripts/c.sh": 1,
        "backend/Dockerfile.x": 1,
        ".github/workflows/w.yml": 1,
        "backend/pyproject.toml": 1,
        ".pre-commit-config.yaml": 1,
    }


def test_excluded_paths_are_not_scanned(tmp_path):
    root = _make_repo(
        tmp_path,
        {
            "backend/alembic/versions/r.py": "# wave 3\n",
            "docs/plan.py": "# wave 3\n",
            "docs-site/x.js": "// wave 3\n",
            "CHANGELOG.md": "wave 3\n",
        },
    )
    assert _scan(root) == {}


def test_baseline_reason_column_is_scanned_but_key_is_not(tmp_path):
    root = _make_repo(
        tmp_path,
        {
            "scripts/guards/baselines/other.txt": "wave2/key.py\t1\tsplit in wave 3\n",
            "scripts/guards/baselines/clean.txt": "wave2/key.py\t1\tsplit planned\n",
        },
    )
    assert _scan(root) == {"scripts/guards/baselines/other.txt": 1}


def test_absent_vulture_whitelist_is_tolerated_and_present_one_scanned(tmp_path):
    root = _make_repo(tmp_path, {"backend/app/ok.py": "x = 1\n"})
    assert _scan(root) == {}
    whitelist = root / "scripts" / "guards" / "vulture_whitelist.py"
    whitelist.parent.mkdir(parents=True)
    whitelist.write_text("_.attr  # unused in round 2\n", encoding="utf-8")
    assert _scan(root) == {"scripts/guards/vulture_whitelist.py": 1}


def test_baseline_files_appearing_untracked_are_picked_up(tmp_path):
    root = _make_repo(tmp_path, {"backend/app/ok.py": "x = 1\n"})
    new = root / "scripts" / "guards" / "baselines" / "late.txt"
    new.parent.mkdir(parents=True)
    new.write_text("k\t1\tfrom wave 2\n", encoding="utf-8")
    assert _scan(root) == {"scripts/guards/baselines/late.txt": 1}


def test_matching_baseline_passes(tmp_path):
    root = _make_repo(tmp_path, {"backend/app/p.py": "# wave 3\n"})
    baseline = tmp_path / "b.txt"
    baseline.write_text(f"backend/app/p.py\t1\t{REASON}\n", encoding="utf-8")
    assert _run(root, baseline) == 0


def test_grown_hits_fail(tmp_path):
    root = _make_repo(tmp_path, {"backend/app/p.py": "# wave 3\n# wave 4\n"})
    baseline = tmp_path / "b.txt"
    baseline.write_text(f"backend/app/p.py\t1\t{REASON}\n", encoding="utf-8")
    assert _run(root, baseline) == 1


def test_stale_entry_fails(tmp_path):
    root = _make_repo(tmp_path, {"backend/app/p.py": "x = 1\n"})
    baseline = tmp_path / "b.txt"
    baseline.write_text(f"backend/app/p.py\t1\t{REASON}\n", encoding="utf-8")
    assert _run(root, baseline) == 1


def test_loosened_entry_fails_and_tighten_repairs(tmp_path):
    root = _make_repo(tmp_path, {"backend/app/p.py": "# wave 3\n", "backend/app/q.py": "x = 1\n"})
    baseline = tmp_path / "b.txt"
    baseline.write_text(
        f"backend/app/p.py\t4\t{REASON}\nbackend/app/q.py\t2\t{REASON}\n", encoding="utf-8"
    )
    assert _run(root, baseline) == 1
    assert _run(root, baseline, "--tighten") == 0
    assert baseline.read_text(encoding="utf-8") == f"backend/app/p.py\t1\t{REASON}\n"
    assert _run(root, baseline) == 0


def test_tighten_never_adds_a_new_violation(tmp_path):
    root = _make_repo(tmp_path, {"backend/app/p.py": "# wave 3\n"})
    baseline = tmp_path / "b.txt"
    baseline.write_text("", encoding="utf-8")
    assert _run(root, baseline, "--tighten") == 1
    assert baseline.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    ("rel", "content"),
    [
        ("backend/app/bad.py", "def (:  # wave 3\n"),
        ("backend/app/bin.py", b"\xff\xfe\x00bad"),
        ("frontend/src/bin.ts", b"\xff\xfe\x00bad"),
    ],
)
def test_unreadable_file_fails_closed(tmp_path, rel, content):
    root = _make_repo(tmp_path, {rel: content})
    _, broken = guard.scan(root)
    assert broken == [rel]
    assert _run(root, tmp_path / "none.txt") == 1


def test_write_baseline_refuses_when_a_file_is_unreadable(tmp_path):
    root = _make_repo(tmp_path, {"backend/app/bad.py": "def (:  # wave 3\n"})
    out = tmp_path / "b.txt"
    assert _run(root, out, "--write-baseline", "--reason", REASON) == 2
    assert not out.exists()


def test_write_baseline_records_findings_with_reason(tmp_path):
    root = _make_repo(tmp_path, {"backend/app/p.py": "# wave 3\n"})
    out = tmp_path / "b.txt"
    assert _run(root, out, "--write-baseline", "--reason", REASON) == 0
    assert out.read_text(encoding="utf-8") == f"backend/app/p.py\t1\t{REASON}\n"
    assert _run(root, out) == 0


def test_real_repo_passes_with_committed_baseline():
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--root", str(_REPO)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_guard_files_carry_no_planning_ids():
    found, broken = guard.scan(
        _REPO,
        [
            "scripts/guards/check_process_ids.py",
            "backend/tests/unit/test_guard_process_ids.py",
            "scripts/guards/baselines/process_ids.txt",
        ],
    )
    assert broken == []
    assert found == {}


def test_value_below_baseline_fails_without_any_stale_entry(tmp_path):
    root = _make_repo(tmp_path, {"backend/app/p.py": "# wave 3\n"})
    baseline = tmp_path / "b.txt"
    baseline.write_text(f"backend/app/p.py\t3\t{REASON}\n", encoding="utf-8")
    assert _run(root, baseline) == 1
