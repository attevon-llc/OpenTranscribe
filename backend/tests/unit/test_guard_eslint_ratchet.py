"""Tests for the frontend ESLint ratchet in ``scripts/guards/check_eslint_ratchet.py``."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_GUARD = _REPO / "scripts" / "guards" / "check_eslint_ratchet.py"


def _load():
    spec = importlib.util.spec_from_file_location("_guard_eslint_under_test", _GUARD)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load()
REASON = "lint: burn-down planned"


def _eslint_json(root: Path, findings: dict[str, list[tuple[str, int]]]) -> str:
    """Build eslint JSON: ``{relpath: [(ruleId, severity), ...]}``."""
    results = [
        {
            "filePath": str(root / rel),
            "messages": [
                {"ruleId": rule, "severity": sev, "message": "m", "line": 1} for rule, sev in msgs
            ],
        }
        for rel, msgs in findings.items()
    ]
    return json.dumps(results)


@pytest.fixture
def env(tmp_path):
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "eslint.config.js").write_text("")
    baseline = tmp_path / "eslint.txt"
    baseline.write_text(
        f"frontend/src/a.ts::no-explicit-any\t2\t{REASON}\n"
        f"frontend/src/b.ts::prefer-const\t1\t{REASON}\n"
    )
    return tmp_path, baseline


def _run(env, findings_or_raw, *extra, capsys):
    root, baseline = env
    raw = (
        findings_or_raw if isinstance(findings_or_raw, str) else _eslint_json(root, findings_or_raw)
    )
    src = root / "out.json"
    src.write_text(raw)
    code = guard.main(
        ["--root", str(root), "--baseline", str(baseline), "--eslint-json", str(src), *extra]
    )
    return code, capsys.readouterr()


OK = {
    "frontend/src/a.ts": [("no-explicit-any", 1), ("no-explicit-any", 1)],
    "frontend/src/b.ts": [("prefer-const", 1)],
}


def test_matching_counts_pass(env, capsys):
    code, out = _run(env, OK, capsys=capsys)
    assert code == 0, out.err
    assert "3 warnings" in out.out


def test_new_warning_bucket_fails(env, capsys):
    findings = {**OK, "frontend/src/c.ts": [("no-empty", 1)]}
    code, out = _run(env, findings, capsys=capsys)
    assert code == 1
    assert "frontend/src/c.ts::no-empty" in out.err
    assert "new violation" in out.err


def test_grown_count_fails(env, capsys):
    findings = {**OK, "frontend/src/a.ts": [("no-explicit-any", 1)] * 3}
    code, out = _run(env, findings, capsys=capsys)
    assert code == 1
    assert "grew past the baseline 2" in out.err


def test_error_severity_fails_even_if_baselined_key(env, capsys):
    findings = {**OK, "frontend/src/d.ts": [("no-undef", 2)]}
    code, out = _run(env, findings, capsys=capsys)
    assert code == 1
    assert "error no-undef" in out.err


def test_error_severity_blocks_write_baseline(env, capsys, tmp_path):
    root, baseline = env
    baseline.write_text("")
    findings = {"frontend/src/d.ts": [("no-undef", 2)]}
    code, out = _run(env, findings, "--write-baseline", "--reason", REASON, capsys=capsys)
    assert code == 1
    assert "never baselined" in out.err
    assert baseline.read_text() == ""


def test_loosened_and_stale_entries_fail_and_tighten_repairs(env, capsys):
    root, baseline = env
    findings = {"frontend/src/a.ts": [("no-explicit-any", 1)]}  # a lowered, b gone
    code, out = _run(env, findings, capsys=capsys)
    assert code == 1
    assert "below the baseline 2" in out.err
    assert "stale baseline entry" in out.err

    code, out = _run(env, findings, "--tighten", capsys=capsys)
    assert code == 0, out.err
    assert baseline.read_text() == f"frontend/src/a.ts::no-explicit-any\t1\t{REASON}\n"
    code, _ = _run(env, findings, capsys=capsys)
    assert code == 0


def test_tighten_never_raises_a_value_or_adds_a_line(env, capsys):
    root, baseline = env
    before = baseline.read_text()
    findings = {**OK, "frontend/src/a.ts": [("no-explicit-any", 1)] * 3}
    code, _ = _run(env, findings, "--tighten", capsys=capsys)
    assert code == 1
    assert baseline.read_text() == before


@pytest.mark.parametrize("raw", ["", "not json", "{}", '[{"filePath": "x"}]'])
def test_malformed_output_fails_closed(env, capsys, raw):
    code, out = _run(env, raw, capsys=capsys)
    assert code == 2
    assert "error:" in out.err


def test_write_baseline_requires_reason(env, capsys):
    root, baseline = env
    baseline.write_text("")
    code, _ = _run(env, OK, "--write-baseline", capsys=capsys)
    assert code == 2
    code, _ = _run(env, OK, "--write-baseline", "--reason", REASON, capsys=capsys)
    assert code == 0
    assert f"frontend/src/a.ts::no-explicit-any\t2\t{REASON}" in baseline.read_text()


def test_real_repo_matches_committed_baseline(capsys):
    """Runs the real eslint over frontend/ (about one minute); the full gate for the baseline."""
    if not (_REPO / "frontend" / "node_modules" / ".bin" / "eslint").exists():
        pytest.skip("frontend/node_modules not installed")
    code = guard.main(["--root", str(_REPO)])
    assert code == 0, capsys.readouterr().err
