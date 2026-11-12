"""Tests for the frontend Stylelint ratchet in ``scripts/guards/check_stylelint_ratchet.py``."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_GUARD = _REPO / "scripts" / "guards" / "check_stylelint_ratchet.py"


def _load():
    spec = importlib.util.spec_from_file_location("_guard_stylelint_under_test", _GUARD)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load()
REASON = "css: reorder planned with a screenshot A/B"
SPEC = "no-descending-specificity"


def _report(root: Path, findings: dict[str, list[str]], **extra: object) -> str:
    """Build stylelint JSON: ``{relpath: [rule, ...]}`` plus optional per-file extra keys."""
    results = [
        {
            "source": str(root / rel),
            "warnings": [
                {"rule": rule, "severity": "error", "text": "t", "line": 1} for rule in rules
            ],
            "parseErrors": [],
            "invalidOptionWarnings": [],
            **extra,
        }
        for rel, rules in findings.items()
    ]
    return json.dumps(results)


@pytest.fixture
def env(tmp_path):
    (tmp_path / "frontend" / "src").mkdir(parents=True)
    (tmp_path / "frontend" / "stylelint.config.mjs").write_text("")
    for name in ("a.svelte", "b.css", "c.svelte"):
        (tmp_path / "frontend" / "src" / name).write_text("")
    baseline = tmp_path / "stylelint.txt"
    baseline.write_text(
        f"frontend/src/a.svelte::{SPEC}\t2\t{REASON}\nfrontend/src/b.css::{SPEC}\t1\t{REASON}\n"
    )
    return tmp_path, baseline


def _run(env, raw_or_findings, *extra, capsys):
    root, baseline = env
    raw = raw_or_findings if isinstance(raw_or_findings, str) else _report(root, raw_or_findings)
    src = root / "report.json"
    src.write_text(raw)
    argv = ["--root", str(root), "--baseline", str(baseline), "--stylelint-json", str(src)]
    code = guard.main([*argv, *extra])
    return code, capsys.readouterr()


OK = {"frontend/src/a.svelte": [SPEC, SPEC], "frontend/src/b.css": [SPEC]}


def test_matching_counts_pass(env, capsys):
    code, out = _run(env, OK, capsys=capsys)
    assert code == 0, out.err
    assert "3 findings in 2 buckets" in out.out


def test_new_finding_fails(env, capsys):
    findings = {**OK, "frontend/src/c.svelte": ["no-duplicate-selectors"]}
    code, out = _run(env, findings, capsys=capsys)
    assert code == 1
    assert "frontend/src/c.svelte::no-duplicate-selectors" in out.err
    assert "new violation" in out.err


def test_grown_count_fails(env, capsys):
    findings = {**OK, "frontend/src/a.svelte": [SPEC] * 3}
    code, out = _run(env, findings, capsys=capsys)
    assert code == 1
    assert "grew past the baseline 2" in out.err


@pytest.mark.parametrize(
    "rule",
    ["CssSyntaxError", "--report-needless-disables", "--report-descriptionless-disables"],
)
def test_never_baselined_findings_fail(env, capsys, rule):
    findings = {**OK, "frontend/src/c.svelte": [rule]}
    code, out = _run(env, findings, capsys=capsys)
    assert code == 1
    assert rule in out.err


def test_invalid_rule_option_fails(env, capsys):
    root, _ = env
    raw = _report(root, OK, invalidOptionWarnings=[{"text": "Invalid option for rule x"}])
    code, out = _run(env, raw, capsys=capsys)
    assert code == 1
    assert "invalid stylelint option" in out.err


def test_never_baselined_blocks_write_baseline(env, capsys):
    root, baseline = env
    baseline.write_text("")
    findings = {"frontend/src/c.svelte": ["CssSyntaxError"]}
    code, out = _run(env, findings, "--write-baseline", "--reason", REASON, capsys=capsys)
    assert code == 1
    assert "never baselined" in out.err
    assert baseline.read_text() == ""


def test_loosened_and_stale_entries_fail_and_tighten_repairs(env, capsys):
    root, baseline = env
    findings = {"frontend/src/a.svelte": [SPEC]}  # a lowered, b gone
    code, out = _run(env, findings, capsys=capsys)
    assert code == 1
    assert "below the baseline 2" in out.err
    assert "stale baseline entry" in out.err

    code, out = _run(env, findings, "--tighten", capsys=capsys)
    assert code == 0, out.err
    assert baseline.read_text() == f"frontend/src/a.svelte::{SPEC}\t1\t{REASON}\n"


def test_tighten_never_raises_a_value(env, capsys):
    _, baseline = env
    before = baseline.read_text()
    code, _ = _run(env, {**OK, "frontend/src/a.svelte": [SPEC] * 3}, "--tighten", capsys=capsys)
    assert code == 1
    assert baseline.read_text() == before


def test_file_mode_compares_only_the_given_files(env, capsys):
    """A staged-file run must not call b.css's baseline entry stale just because b was not linted."""
    findings = {"frontend/src/a.svelte": [SPEC, SPEC]}
    code, out = _run(env, findings, "frontend/src/a.svelte", capsys=capsys)
    assert code == 0, out.err


def test_file_mode_still_catches_a_new_finding(env, capsys):
    findings = {"frontend/src/c.svelte": ["no-duplicate-selectors"]}
    code, out = _run(env, findings, "frontend/src/c.svelte", capsys=capsys)
    assert code == 1
    assert "frontend/src/c.svelte::no-duplicate-selectors" in out.err


def test_file_mode_ignores_files_outside_frontend_src(env, capsys):
    code, out = _run(env, "not json", "backend/app/main.py", "frontend/src/a.ts", capsys=capsys)
    assert code == 0, out.err
    assert "no frontend/src CSS or Svelte files" in out.out


@pytest.mark.parametrize("raw", ["", "not json", "{}", '[{"source": "x"}]', '[{"warnings": [{}]}]'])
def test_malformed_output_fails_closed(env, capsys, raw):
    code, out = _run(env, raw, capsys=capsys)
    assert code == 2
    assert "error:" in out.err


def test_real_repo_matches_committed_baseline(capsys):
    """Runs the real stylelint over frontend/src (seconds); the full gate for the baseline."""
    if not (_REPO / "frontend" / "node_modules" / ".bin" / "stylelint").exists():
        pytest.skip("frontend/node_modules not installed")
    code = guard.main(["--root", str(_REPO)])
    assert code == 0, capsys.readouterr().err


def _fake_fix(calls, rewrite):
    def run(argv, **kwargs):
        calls.append(argv)
        for name in argv[argv.index("--fix") + 1 :]:
            if rewrite:
                (kwargs["cwd"] / name).write_text("fixed")

        class Done:
            returncode = 0

        return Done()

    return run


def test_autofix_runs_on_plain_css_only_and_reports_changed_files(env, monkeypatch):
    root, _ = env
    calls: list[list[str]] = []
    monkeypatch.setattr(guard.subprocess, "run", _fake_fix(calls, rewrite=True))
    changed = guard.autofix_css(root / "frontend", ["src/a.svelte", "src/b.css"], 5)
    assert changed == ["src/b.css"]
    assert calls[0][-1] == "src/b.css" and "src/a.svelte" not in calls[0]


def test_autofix_with_nothing_to_fix_reports_nothing(env, monkeypatch):
    root, _ = env
    monkeypatch.setattr(guard.subprocess, "run", _fake_fix([], rewrite=False))
    assert guard.autofix_css(root / "frontend", ["src/b.css"], 5) == []
    assert guard.autofix_css(root / "frontend", ["src/a.svelte"], 5) == []


def test_file_mode_exits_1_when_autofix_rewrote_a_css_file(env, monkeypatch, capsys):
    root, baseline = env
    monkeypatch.setattr(guard.subprocess, "run", _fake_fix([], rewrite=True))
    code = guard.main(["--root", str(root), "--baseline", str(baseline), "frontend/src/b.css"])
    assert code == 1
    assert "re-stage" in capsys.readouterr().err
