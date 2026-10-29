"""Tests for the knip and madge ratchet in ``scripts/guards/check_knip.py``."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO / "scripts" / "guards" / "check_knip.py"
_KNIP_BASELINE = _REPO / "scripts" / "guards" / "baselines" / "knip.txt"
_MADGE_BASELINE = _REPO / "scripts" / "guards" / "baselines" / "madge_cycles.txt"


def _load():
    spec = importlib.util.spec_from_file_location("_guard_knip_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load()
REASON = "pre-existing unused export kept until its module is next touched"
CYCLE = "lib/axios.ts > stores/auth.ts"
CLEAN_MADGE = "- Finding files\nProcessed 3 files\n\n✔ No circular dependency found!\n"


def knip_json(*records: dict) -> str:
    base: dict[str, list[object]] = {
        "binaries": [],
        "dependencies": [],
        "exports": [],
        "files": [],
        "types": [],
    }
    return json.dumps({"issues": [{**base, **r} for r in records]})


def madge_text(*chains: str) -> str:
    body = "".join(f"{i}) {c}\n" for i, c in enumerate(chains, 1))
    return f"- Finding files\nProcessed 9 files\n\n✖ Found {len(chains)} circular dependency!\n\n{body}"


@pytest.fixture
def env(tmp_path):
    """Baselines for one export and the known cycle, plus helpers to run the guard offline."""
    knip_base = tmp_path / "knip.txt"
    madge_base = tmp_path / "madge.txt"
    knip_base.write_text(f"exports::src/a.ts::used\t1\t{REASON}\n")
    madge_base.write_text(f"cycle::{CYCLE}\t1\t{REASON}\n")

    def run(knip: str, madge: str, *extra: str) -> int:
        (tmp_path / "knip.json").write_text(knip)
        (tmp_path / "madge.out").write_text(madge)
        code: int = guard.main(
            [
                "--knip-json", str(tmp_path / "knip.json"),
                "--madge-output", str(tmp_path / "madge.out"),
                "--baseline", str(knip_base),
                "--madge-baseline", str(madge_base),
                *extra,
            ]
        )  # fmt: skip
        return code

    return run, knip_base, madge_base


BASE_KNIP = knip_json({"file": "src/a.ts", "exports": [{"name": "used"}]})
BASE_MADGE = madge_text(CYCLE)


def test_baselined_findings_pass(env):
    run, *_ = env
    assert run(BASE_KNIP, BASE_MADGE) == 0


def test_new_unused_export_fails(env, capsys):
    run, *_ = env
    knip = knip_json({"file": "src/a.ts", "exports": [{"name": "used"}, {"name": "plantedUnused"}]})
    assert run(knip, BASE_MADGE) == 1
    assert "exports::src/a.ts::plantedUnused" in capsys.readouterr().out


def test_unused_file_and_dependency_are_keyed_by_type(env, capsys):
    run, *_ = env
    knip = knip_json(
        {"file": "src/a.ts", "exports": [{"name": "used"}]},
        {"file": "src/dead.ts", "files": [{"name": "src/dead.ts"}]},
        {"file": "package.json", "dependencies": [{"name": "left-pad"}]},
    )
    assert run(knip, BASE_MADGE) == 1
    out = capsys.readouterr().out
    assert "files::src/dead.ts::src/dead.ts" in out
    assert "dependencies::package.json::left-pad" in out


def test_new_cycle_fails(env, capsys):
    run, *_ = env
    assert run(BASE_KNIP, madge_text(CYCLE, "lib/a.ts > lib/b.ts > lib/c.ts")) == 1
    assert "cycle::lib/a.ts > lib/b.ts > lib/c.ts" in capsys.readouterr().out


def test_cycle_key_is_rotation_independent(env):
    run, *_ = env
    assert run(BASE_KNIP, madge_text("stores/auth.ts > lib/axios.ts")) == 0


def test_stale_entries_fail_and_tighten_repairs(env):
    run, knip_base, madge_base = env
    assert run(knip_json(), CLEAN_MADGE) == 1
    assert run(knip_json(), CLEAN_MADGE, "--tighten") == 0
    assert knip_base.read_text() == ""
    assert madge_base.read_text() == ""


def test_loosened_count_fails_and_tighten_lowers_it(env):
    run, knip_base, _ = env
    knip_base.write_text(f"exports::src/a.ts::used\t3\t{REASON}\n")
    assert run(BASE_KNIP, BASE_MADGE) == 1
    assert run(BASE_KNIP, BASE_MADGE, "--tighten") == 0
    assert knip_base.read_text().split("\t")[1] == "1"


def test_tighten_never_adds_a_finding(env):
    run, knip_base, _ = env
    knip = knip_json({"file": "src/a.ts", "exports": [{"name": "used"}, {"name": "new"}]})
    assert run(knip, BASE_MADGE, "--tighten") == 1
    assert "new" not in knip_base.read_text()


@pytest.mark.parametrize("knip", ["", "not json", "[]", '{"nope": 1}', '{"issues": [3]}'])
def test_malformed_knip_output_fails_closed(env, knip):
    run, *_ = env
    assert run(knip, BASE_MADGE) == 2


@pytest.mark.parametrize("madge", ["", "sh: madge: not found\n", "Error: boom\n"])
def test_madge_crash_fails_closed(env, madge):
    run, *_ = env
    assert run(BASE_KNIP, madge) == 2


def test_write_baseline_records_both_tools_with_reasons(tmp_path):
    (tmp_path / "k.json").write_text(BASE_KNIP)
    (tmp_path / "m.out").write_text(BASE_MADGE)
    argv = [
        "--knip-json", str(tmp_path / "k.json"),
        "--madge-output", str(tmp_path / "m.out"),
        "--baseline", str(tmp_path / "kb.txt"),
        "--madge-baseline", str(tmp_path / "mb.txt"),
    ]  # fmt: skip
    assert (
        guard.main([*argv, "--write-baseline", "--reason", REASON, "--cycle-reason", "kept: cycle"])
        == 0
    )
    assert guard.main(argv) == 0
    assert (tmp_path / "mb.txt").read_text() == f"cycle::{CYCLE}\t1\tkept: cycle\n"
    assert guard.main([*argv, "--write-baseline", "--reason", ""]) == 2


def test_committed_baselines_are_well_formed_and_reasoned():
    for path in (_KNIP_BASELINE, _MADGE_BASELINE):
        for key, entry in guard.bl.load(path).items():
            assert "::" in key
            assert entry.value >= 1
            assert len(entry.reason.split()) >= 3
    assert f"cycle::{CYCLE}" in guard.bl.load(_MADGE_BASELINE)


@pytest.mark.skipif(
    not (_REPO / "frontend" / "node_modules" / ".bin" / "knip").exists(),
    reason="frontend dependencies are not installed",
)
def test_real_repo_passes_with_committed_baselines(capsys):
    """Runs knip and madge for real (about 15 s)."""
    assert guard.main(["--root", str(_REPO)]) == 0, capsys.readouterr().out
    assert "all baselined" in capsys.readouterr().out


@pytest.mark.skipif(
    not (_REPO / "frontend" / "node_modules" / ".bin" / "knip").exists(),
    reason="frontend dependencies are not installed",
)
def test_real_repo_with_planted_export_fails(tmp_path, capsys):
    """A copy of the frontend tree with a new unused export is caught by the real tools."""
    frontend = tmp_path / "frontend"
    shutil.copytree(
        _REPO / "frontend", frontend, symlinks=True,
        ignore=shutil.ignore_patterns("node_modules", ".svelte-kit", "dist", "coverage"),
    )  # fmt: skip
    (frontend / "node_modules").symlink_to(_REPO / "frontend" / "node_modules")
    (frontend / "src" / "lib" / "_probe.ts").write_text("export const plantedUnused = 1;\n")
    assert guard.main(["--root", str(tmp_path)]) == 1
    assert "exports::src/lib/_probe.ts::plantedUnused" in capsys.readouterr().out
