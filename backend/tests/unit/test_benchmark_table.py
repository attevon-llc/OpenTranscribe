"""The engine benchmark scripts share one table/CSV writer (scripts/benchmark_table.py)."""

from __future__ import annotations

import csv
import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"


@pytest.fixture(scope="module")
def table() -> ModuleType:
    sys.path.insert(0, str(_SCRIPTS))
    try:
        return importlib.import_module("benchmark_table")
    finally:
        sys.path.remove(str(_SCRIPTS))


def test_format_cell_renders_none_bool_and_values(table):
    assert table.format_cell(None) == "—"
    assert table.format_cell(True) == "Y"
    assert table.format_cell(False) == "N"
    assert table.format_cell(1.5) == "1.5"


def test_print_table_aligns_columns_to_the_widest_cell(table, capsys):
    table.print_table([{"a": "x", "b": None}, {"a": "longer", "b": True}], ["a", "b"])
    lines = capsys.readouterr().out.splitlines()
    assert lines == ["a       b", "------  -", "x       —", "longer  Y"]


def test_write_csv_creates_parents_and_ignores_extra_keys(table, tmp_path):
    out = tmp_path / "nested" / "r.csv"
    table.write_csv([{"a": 1, "b": 2, "zzz": 3}], out, ["a", "b"])
    with out.open(encoding="utf-8") as fh:
        assert list(csv.reader(fh)) == [["a", "b"], ["1", "2"]]


@pytest.fixture(scope="module")
def common() -> ModuleType:
    sys.path.insert(0, str(_SCRIPTS))
    try:
        return importlib.import_module("bench_common")
    finally:
        sys.path.remove(str(_SCRIPTS))


def test_redis_url_reads_the_password_from_the_env_file_and_the_port_from_the_stack(
    common, tmp_path, monkeypatch
):
    """The port is the stack's published one (an exported REDIS_PORT here), never the .env
    value: a --fresh stack publishes at an offset its .env does not know (1cc6878ab)."""
    monkeypatch.delenv("BENCHMARK_REDIS_URL", raising=False)
    monkeypatch.setenv("REDIS_PORT", "6400")
    env = tmp_path / ".env"
    env.write_text('REDIS_PASSWORD="s3cret"  # note\nREDIS_PORT=7777  # debug\n')
    assert common.load_redis_url(env) == "redis://:s3cret@localhost:6400/0"


def test_redis_url_has_no_password_without_an_env_file_and_env_var_wins(
    common, tmp_path, monkeypatch
):
    monkeypatch.delenv("BENCHMARK_REDIS_URL", raising=False)
    monkeypatch.setenv("REDIS_PORT", "5177")
    assert common.load_redis_url(tmp_path / "missing") == "redis://localhost:5177/0"
    monkeypatch.setenv("BENCHMARK_REDIS_URL", "redis://override/1")
    assert common.load_redis_url(tmp_path / "missing") == "redis://override/1"


def test_default_env_file_is_the_repo_root_dotenv(common):
    assert _SCRIPTS.parent / ".env" == common.REPO_ENV_FILE
