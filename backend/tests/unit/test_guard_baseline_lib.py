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


def _write(tmp_path, text):
    path = tmp_path / "g.tsv"
    path.write_text(text)
    return path


def test_load_missing_file_is_an_empty_baseline(tmp_path):
    assert bl.load(tmp_path / "absent.tsv") == {}


def test_load_ignores_blank_whitespace_and_comment_lines(tmp_path):
    path = _write(
        tmp_path,
        f"# header\n\n   \n\t\n   # indented comment\na.py\t3\t{REASON}\n# trailer\n",
    )
    assert bl.load(path) == {"a.py": bl.Entry(3, REASON)}


def test_load_strips_reason_and_accepts_padded_integer(tmp_path):
    path = _write(tmp_path, "a.py\t 4 \t  padded reason  \n")
    assert bl.load(path) == {"a.py": bl.Entry(4, "padded reason")}


@pytest.mark.parametrize("extra", ["\textra", "\t\t"])
def test_load_rejects_more_than_three_columns(tmp_path, extra):
    path = _write(tmp_path, f"a.py\t3\t{REASON}{extra}\n")
    with pytest.raises(ValueError, match="expected"):
        bl.load(path)


def test_load_error_names_the_offending_line_number(tmp_path):
    path = _write(tmp_path, f"# c\n\na.py\t3\t{REASON}\nb.py\tx\t{REASON}\n")
    with pytest.raises(ValueError, match=r"g\.tsv:4: value must be an integer"):
        bl.load(path)


def test_load_missing_reason_message_is_not_the_integer_message(tmp_path):
    path = _write(tmp_path, "a.py\t3\t\n")
    with pytest.raises(ValueError, match=r"g\.tsv:1: expected .*with a reason"):
        bl.load(path)


def test_load_duplicate_key_message_names_the_second_line(tmp_path):
    path = _write(tmp_path, f"a.py\t1\t{REASON}\n\na.py\t2\t{REASON}\n")
    with pytest.raises(ValueError, match=r"g\.tsv:3: duplicate key 'a\.py'"):
        bl.load(path)


def test_compare_equal_value_passes():
    assert bl.compare({"a.py": 5}, {"a.py": bl.Entry(5, REASON)}, unit="lines") == []


def test_compare_flags_a_new_violation():
    errors = bl.compare({"new.py": 2}, {}, unit="lines")
    assert errors == ["new.py: 2 lines (new violation, not in the baseline)"]


def test_compare_flags_a_value_that_grew():
    errors = bl.compare({"a.py": 9}, {"a.py": bl.Entry(5, REASON)}, unit="lines")
    assert errors == ["a.py: 9 lines grew past the baseline 5"]


def test_compare_flags_a_loosened_entry_below_the_baseline():
    errors = bl.compare({"a.py": 3}, {"a.py": bl.Entry(5, REASON)}, unit="lines")
    assert errors == [
        "a.py: 3 lines is below the baseline 5; lower it (run the guard with --tighten)"
    ]


def test_compare_flags_a_stale_entry_with_no_violation():
    errors = bl.compare({}, {"gone.py": bl.Entry(5, REASON)}, unit="lines")
    assert errors == [
        "gone.py: stale baseline entry, no violation left (delete the line or run --tighten)"
    ]


def test_compare_reports_every_class_sorted_with_stale_last():
    baseline = {
        "z.py": bl.Entry(1, REASON),
        "b.py": bl.Entry(5, REASON),
        "stale2.py": bl.Entry(1, REASON),
        "stale1.py": bl.Entry(1, REASON),
    }
    current = {"z.py": 2, "b.py": 4, "a.py": 1}
    errors = bl.compare(current, baseline, unit="x")
    assert [e.split(":")[0] for e in errors] == ["a.py", "b.py", "z.py", "stale1.py", "stale2.py"]
    assert "new violation" in errors[0]
    assert "below the baseline" in errors[1]
    assert "grew past" in errors[2]


def test_tighten_lowers_only_the_value_and_keeps_the_reason(tmp_path):
    path = _write(tmp_path, "a.py\t5\tfirst reason\nb.py\t2\tsecond reason\n")
    changed = bl.tighten(path, {"a.py": 3, "b.py": 2}, bl.load(path))
    assert changed == 1
    assert path.read_text() == "a.py\t3\tfirst reason\nb.py\t2\tsecond reason\n"


def test_tighten_preserves_comments_and_blank_lines(tmp_path):
    path = _write(tmp_path, f"# header\n\na.py\t5\t{REASON}\n# mid\ngone.py\t1\t{REASON}\n")
    changed = bl.tighten(path, {"a.py": 2}, bl.load(path))
    assert changed == 2
    assert path.read_text() == f"# header\n\na.py\t2\t{REASON}\n# mid\n"


def test_tighten_leaves_a_grown_value_alone_and_refuses_to_report_clean(tmp_path):
    path = _write(tmp_path, f"a.py\t5\t{REASON}\n")
    baseline = bl.load(path)
    assert bl.tighten(path, {"a.py": 9}, baseline) == 0
    assert path.read_text() == f"a.py\t5\t{REASON}\n"
    assert bl.compare({"a.py": 9}, bl.load(path), unit="u") != []


def test_tighten_with_every_entry_stale_leaves_an_empty_file(tmp_path):
    path = _write(tmp_path, f"a.py\t5\t{REASON}\n")
    assert bl.tighten(path, {}, bl.load(path)) == 1
    assert path.read_text() == ""
    assert bl.load(path) == {}


def test_tighten_result_compares_clean_against_the_findings(tmp_path):
    path = _write(tmp_path, f"a.py\t5\t{REASON}\nb.py\t9\t{REASON}\ngone.py\t1\t{REASON}\n")
    current = {"a.py": 4, "b.py": 9}
    assert bl.compare(current, bl.load(path), unit="u") != []
    bl.tighten(path, current, bl.load(path))
    assert bl.compare(current, bl.load(path), unit="u") == []


def test_find_root_returns_start_itself_when_it_has_a_marker(tmp_path):
    (tmp_path / "MARK").touch()
    assert bl.find_root(tmp_path, ("MARK",)) == tmp_path


def test_find_root_walks_up_to_an_ancestor(tmp_path):
    (tmp_path / "MARK").touch()
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    assert bl.find_root(deep, ("MARK",)) == tmp_path


def test_find_root_prefers_the_nearest_ancestor(tmp_path):
    (tmp_path / "MARK").touch()
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "MARK").touch()
    nested = inner / "x"
    nested.mkdir()
    assert bl.find_root(nested, ("MARK",)) == inner


def test_find_root_accepts_any_one_of_several_markers_and_directories(tmp_path):
    (tmp_path / "second_marker_dir").mkdir()
    assert bl.find_root(tmp_path, ("first_marker", "second_marker_dir")) == tmp_path


def test_find_root_without_a_marker_raises_scan_error_naming_the_markers(tmp_path):
    with pytest.raises(bl.ScanError, match=r"no zz_nope_1 or zz_nope_2 found .*--root"):
        bl.find_root(tmp_path, ("zz_nope_1", "zz_nope_2"))


def test_scan_error_is_a_plain_exception_not_a_value_error():
    assert issubclass(bl.ScanError, Exception)
    assert not issubclass(bl.ScanError, ValueError)


def test_write_initial_strips_the_reason_it_writes(tmp_path):
    path = tmp_path / "g.tsv"
    bl.write_initial(path, {"a.py": 1}, f"  {REASON}  ")
    assert path.read_text() == f"a.py\t1\t{REASON}\n"


def test_write_initial_rejects_a_placeholder_reason_padded_with_whitespace(tmp_path):
    path = tmp_path / "g.tsv"
    with pytest.raises(bl.ScanError, match="real --reason"):
        bl.write_initial(path, FINDINGS, "  TODO  ")
    assert not path.exists()


@pytest.mark.parametrize("bad", ["two\tcolumns", "two\nlines", "carriage\rreturn"])
def test_write_initial_rejects_a_reason_that_would_corrupt_the_tsv(tmp_path, bad):
    path = tmp_path / "g.tsv"
    with pytest.raises(bl.ScanError, match="single line"):
        bl.write_initial(path, FINDINGS, bad)
    assert not path.exists()


@pytest.mark.parametrize("bad", ["", "a\tb.py", "a\nb.py", "a\rb.py"])
def test_write_initial_rejects_an_unusable_key(tmp_path, bad):
    path = tmp_path / "g.tsv"
    with pytest.raises(bl.ScanError, match="unusable baseline key"):
        bl.write_initial(path, {"ok.py": 1, bad: 2}, REASON)
    assert not path.exists()


def test_write_initial_creates_missing_parent_directories(tmp_path):
    path = tmp_path / "deep" / "er" / "g.tsv"
    assert bl.write_initial(path, FINDINGS, REASON) == 2
    assert path.is_file()


def test_write_initial_with_no_findings_writes_an_empty_file(tmp_path):
    path = tmp_path / "g.tsv"
    assert bl.write_initial(path, {}, REASON) == 0
    assert path.read_text() == ""


def test_write_initial_force_works_when_nothing_exists_yet(tmp_path):
    path = tmp_path / "g.tsv"
    assert bl.write_initial(path, FINDINGS, REASON, force=True) == 2


def test_write_initial_checks_the_reason_before_the_overwrite_guard(tmp_path):
    path = _write(tmp_path, f"old.py\t1\t{REASON}\n")
    with pytest.raises(bl.ScanError, match="real --reason"):
        bl.write_initial(path, FINDINGS, "")


def test_placeholder_check_is_stable_across_repeated_calls(tmp_path):
    for i in range(3):
        assert bl.write_initial(tmp_path / f"{i}.tsv", FINDINGS, REASON) == 2
    with pytest.raises(bl.ScanError):
        bl.write_initial(tmp_path / "x.tsv", FINDINGS, "TODO")


def test_add_baseline_args_defaults_and_flags():
    defaults = _parser().parse_args([])
    assert (defaults.write_baseline, defaults.reason, defaults.force) == (False, "", False)
    given = _parser().parse_args(["--write-baseline", "--reason", "why", "--force"])
    assert (given.write_baseline, given.reason, given.force) == (True, "why", True)


def test_run_write_baseline_reports_success_on_stdout(tmp_path, capsys):
    path = tmp_path / "g.tsv"
    args = _parser().parse_args(["--write-baseline", "--reason", REASON])
    assert bl.run_write_baseline(args, path, FINDINGS) == 0
    captured = capsys.readouterr()
    assert captured.out == f"wrote 2 baseline entries to {path}\n"
    assert captured.err == ""


def test_run_write_baseline_refusal_goes_to_stderr_and_writes_nothing(tmp_path, capsys):
    path = tmp_path / "g.tsv"
    assert bl.run_write_baseline(_parser().parse_args([]), path, FINDINGS) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: refusing to write a baseline")
    assert not path.exists()


def test_tighten_only_touches_three_column_lines_even_for_a_known_key(tmp_path):
    # The baseline dict is caller-supplied, so a line that merely starts with a known key
    # but is not key<TAB>value<TAB>reason must be copied through untouched.
    path = _write(tmp_path, "a.py\t5\n")
    assert bl.tighten(path, {"a.py": 2}, {"a.py": bl.Entry(5, REASON)}) == 0
    assert path.read_text() == "a.py\t5\n"
    assert bl.tighten(path, {}, {"a.py": bl.Entry(5, REASON)}) == 0
    assert path.read_text() == "a.py\t5\n"


def test_reason_check_loads_audit_tests_once_and_reuses_the_module(tmp_path):
    name = "_guard_audit_tests_reuse"
    bl.write_initial(tmp_path / "1.tsv", FINDINGS, REASON)
    first = sys.modules[name]
    bl.write_initial(tmp_path / "2.tsv", FINDINGS, REASON)
    assert sys.modules[name] is first


@pytest.mark.parametrize("broken", ["no_spec", "no_loader"])
def test_reason_check_fails_closed_when_audit_tests_cannot_be_loaded(tmp_path, monkeypatch, broken):
    class _NoLoader:
        loader = None

    monkeypatch.setattr(
        bl.importlib.util,
        "spec_from_file_location",
        lambda *_a, **_k: None if broken == "no_spec" else _NoLoader(),
    )
    with pytest.raises(bl.ScanError, match="cannot load"):
        bl.write_initial(tmp_path / "g.tsv", FINDINGS, REASON)
    assert not (tmp_path / "g.tsv").exists()
