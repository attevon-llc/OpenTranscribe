"""The isolation probe's Settings import must not need container-only paths (TEMP_DIR=/app/temp)."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_LIB = Path(__file__).resolve().parents[3] / "scripts" / "lib"


@pytest.fixture
def iso(monkeypatch):
    monkeypatch.syspath_prepend(str(_LIB))
    sys.modules.pop("mutation_isolation", None)
    return importlib.import_module("mutation_isolation")


def _planned():
    return {"spans": SimpleNamespace(db="ot_mut_spans", redis_index="3")}


def test_probe_subprocess_gets_a_host_writable_temp_dir(iso, monkeypatch):
    seen = []

    def fake_run(cmd, **kw):
        seen.append(kw["env"])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(iso.subprocess, "run", fake_run)
    iso._check_settings_honour_env(_planned())
    assert seen and seen[0]["TEMP_DIR"] not in ("", "/app/temp")
    assert seen[0]["POSTGRES_DB"] == "ot_mut_spans"


def test_probe_failure_reports_the_child_stderr(iso, monkeypatch):
    monkeypatch.setattr(
        iso.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "PermissionError: /app/temp"),
    )
    with pytest.raises(iso.IsolationError, match="PermissionError: /app/temp"):
        iso._check_settings_honour_env(_planned())
