"""Shared harness for the tests of the svelte-check ratchets in ``scripts/guards/``."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

_GUARDS = Path(__file__).resolve().parents[3] / "scripts" / "guards"


def load_guard(script: str) -> ModuleType:
    """Import ``scripts/guards/<script>.py`` under a private module name."""
    spec = importlib.util.spec_from_file_location(f"_{script}_under_test", _GUARDS / f"{script}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def diagnostic(filename: str, code: int, kind: str = "ERROR", ts: int = 1) -> str:
    body = json.dumps({"type": kind, "filename": filename, "message": "m", "code": code})
    return f"{ts} {body}"


def output(*lines: str) -> str:
    return "\n".join(
        ['1 START "/x"', *lines, "9 COMPLETED 5 FILES 1 ERRORS 0 WARNINGS 1 FILES_WITH_PROBLEMS"]
    )


def run_guard(guard: ModuleType, tmp_path: Path, text: str, baseline: str, *extra: str) -> int:
    out = tmp_path / "out.txt"
    out.write_text(text, encoding="utf-8")
    base = tmp_path / "baseline.txt"
    base.write_text(baseline, encoding="utf-8")
    return int(guard.main(["--svelte-check-output", str(out), "--baseline", str(base), *extra]))
