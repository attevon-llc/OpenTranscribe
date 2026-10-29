"""Tests for the shared ratchet-baseline library in ``scripts/guards/_baseline.py``."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parents[3] / "scripts" / "guards" / "_baseline.py"


def _load():
    spec = importlib.util.spec_from_file_location("_guard_baseline_under_test", _LIB)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bl = _load()

FINDINGS = {"b.py": 7, "a.py": 3}
REASON = "legacy modules predate the size limit"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    bl.add_baseline_args(parser)
    return parser


def test_write_initial_is_sorted_and_deterministic(tmp_path):
    path = tmp_path / "g.tsv"
    assert bl.write_initial(path, FINDINGS, REASON) == 2
    assert path.read_text() == f"a.py\t3\t{REASON}\nb.py\t7\t{REASON}\n"


def test_write_initial_round_trips_through_load(tmp_path):
    path = tmp_path / "g.tsv"
    bl.write_initial(path, FINDINGS, REASON)
    loaded = bl.load(path)
    assert loaded == {"a.py": bl.Entry(3, REASON), "b.py": bl.Entry(7, REASON)}
    assert bl.compare(FINDINGS, loaded, unit="lines") == []


@pytest.mark.parametrize("reason", ["", "   ", "TODO", "no reason given", "tbd"])
def test_write_initial_refuses_missing_or_placeholder_reason(tmp_path, reason):
    path = tmp_path / "g.tsv"
    with pytest.raises(bl.ScanError):
        bl.write_initial(path, FINDINGS, reason)
    assert not path.exists()


def test_write_initial_refuses_overwrite_without_force(tmp_path):
    path = tmp_path / "g.tsv"
    path.write_text(f"old.py\t1\t{REASON}\n")
    with pytest.raises(bl.ScanError, match="--force"):
        bl.write_initial(path, FINDINGS, REASON)
    assert path.read_text() == f"old.py\t1\t{REASON}\n"


def test_write_initial_force_overwrites(tmp_path):
    path = tmp_path / "g.tsv"
    path.write_text(f"old.py\t1\t{REASON}\n")
    bl.write_initial(path, FINDINGS, REASON, force=True)
    assert set(bl.load(path)) == {"a.py", "b.py"}


def test_write_initial_allows_empty_existing_file(tmp_path):
    path = tmp_path / "g.tsv"
    path.write_text("\n")
    assert bl.write_initial(path, FINDINGS, REASON) == 2


def test_cli_helper_exit_codes(tmp_path, capsys):
    path = tmp_path / "g.tsv"
    assert bl.run_write_baseline(_parser().parse_args(["--write-baseline"]), path, FINDINGS) == 2
    assert "real --reason" in capsys.readouterr().err
    args = _parser().parse_args(["--write-baseline", "--reason", REASON])
    assert bl.run_write_baseline(args, path, FINDINGS) == 0
    assert bl.run_write_baseline(args, path, FINDINGS) == 2
    assert "--force" in capsys.readouterr().err
    forced = _parser().parse_args(["--write-baseline", "--reason", REASON, "--force"])
    assert bl.run_write_baseline(forced, path, {"c.py": 1}) == 0
    assert set(bl.load(path)) == {"c.py"}


def test_tighten_never_adds_a_line_or_raises_a_value(tmp_path):
    path = tmp_path / "g.tsv"
    bl.write_initial(path, {"a.py": 5, "b.py": 7, "gone.py": 2}, REASON)
    current = {"a.py": 4, "b.py": 99, "new.py": 10}
    changed = bl.tighten(path, current, bl.load(path))
    after = bl.load(path)
    assert changed == 2
    assert set(after) == {"a.py", "b.py"}
    assert after["a.py"].value == 4
    assert after["b.py"].value == 7


@pytest.mark.parametrize(
    "line",
    ["a.py\t3", f"a.py\tx\t{REASON}", f"\t3\t{REASON}", "a.py\t3\t  ", f"a.py 3 {REASON}"],
)
def test_malformed_baseline_line_fails_closed(tmp_path, line):
    path = tmp_path / "g.tsv"
    path.write_text(line + "\n")
    with pytest.raises(ValueError, match="g.tsv:1"):
        bl.load(path)


def test_duplicate_key_fails_closed(tmp_path):
    path = tmp_path / "g.tsv"
    path.write_text(f"a.py\t1\t{REASON}\na.py\t2\t{REASON}\n")
    with pytest.raises(ValueError, match="duplicate"):
        bl.load(path)
