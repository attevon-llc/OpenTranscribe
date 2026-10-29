"""Behavioural tests for ``scripts/guards/check_ruff_ratchet.py``.

Each case builds a throwaway tree and runs the real guard (and so the real, pinned ruff)
against it, so a guard that scans nothing or lets a ``noqa`` hide a handler fails here.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_GUARD_PATH = Path(__file__).resolve().parents[3] / "scripts" / "guards" / "check_ruff_ratchet.py"


def _load_guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_ruff_ratchet_under_test", _GUARD_PATH)
    assert spec is not None and spec.loader is not None, f"cannot load {_GUARD_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load_guard()

COMPLEX_16 = "def probe(x):\n" + "".join(
    f"    if x == {i}:\n        return {i}\n" for i in range(15)
)
COMPLEX_16 += "    return -1\n"
BROAD = "def f():\n    try:\n        return 1\n    except Exception:  # noqa: BLE001\n        return 2\n"


def _tree(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    for target in guard.TARGETS:
        (tmp_path / target).mkdir(parents=True, exist_ok=True)
    for rel, body in (files or {}).items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return tmp_path


def _run(root: Path, *extra: str) -> tuple[int, str]:
    cx, be = root / "cx.txt", root / "be.txt"
    return guard.main(
        [
            "--root",
            str(root),
            "--complexity-baseline",
            str(cx),
            "--broad-except-baseline",
            str(be),
            *extra,
        ]
    ), ""


def _write(root: Path, cx: str = "", be: str = "") -> None:
    (root / "cx.txt").write_text(cx, encoding="utf-8")
    (root / "be.txt").write_text(be, encoding="utf-8")


def test_clean_tree_passes(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"backend/app/ok.py": "def f():\n    return 1\n"})
    _write(root)
    assert _run(root)[0] == 0


def test_planted_16_branch_function_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(tmp_path, {"backend/app/_probe.py": COMPLEX_16})
    _write(root)
    assert _run(root)[0] == 1
    assert "backend/app/_probe.py::probe: 16 CC (new violation" in capsys.readouterr().err


def test_baselined_complexity_passes_and_growth_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"backend/app/_probe.py": COMPLEX_16})
    _write(root, cx="backend/app/_probe.py::probe\t16\tcomplexity: decomposition planned\n")
    assert _run(root)[0] == 0
    grown = COMPLEX_16.replace("    return -1", "    if x == 99:\n        return 99\n    return -1")
    (root / "backend/app/_probe.py").write_text(grown, encoding="utf-8")
    assert _run(root)[0] == 1


def test_method_key_uses_qualified_name(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    body = "class K:\n" + "".join(
        f"    {line}\n" if line else "\n" for line in COMPLEX_16.splitlines()
    )
    root = _tree(tmp_path, {"backend/app/m.py": body})
    _write(root)
    assert _run(root)[0] == 1
    assert "backend/app/m.py::K.probe:" in capsys.readouterr().err


def test_noqa_does_not_hide_an_extra_broad_except(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(tmp_path, {"scripts/tool.py": BROAD})
    _write(root, be="scripts/tool.py\t1\tratchet: may only go down\n")
    assert _run(root)[0] == 0
    (root / "scripts/tool.py").write_text(
        BROAD + "\n\n" + BROAD.replace("def f", "def g"), encoding="utf-8"
    )
    assert _run(root)[0] == 1
    assert "scripts/tool.py: 2 handler(s) grew past the baseline 1" in capsys.readouterr().err


def test_broad_except_in_unbaselined_file_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"backend/alembic/env_probe.py": BROAD})
    _write(root)
    assert _run(root)[0] == 1


def test_tests_directory_is_not_scanned(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"backend/tests/t.py": BROAD, "backend/app/ok.py": "X = 1\n"})
    _write(root)
    assert _run(root)[0] == 0


def test_stale_and_loosened_entries_fail_and_tighten_repairs(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"scripts/tool.py": BROAD})
    stale = "backend/app/gone.py::f\t20\tcomplexity: decomposition planned\n"
    _write(root, cx=stale, be="scripts/tool.py\t5\tratchet: may only go down\n")
    assert _run(root)[0] == 1
    assert _run(root, "--tighten")[0] == 0
    assert (root / "cx.txt").read_text(encoding="utf-8") == ""
    assert (root / "be.txt").read_text(encoding="utf-8").split("\t")[1] == "1"
    assert _run(root)[0] == 0


def test_tighten_never_adds_or_raises(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"backend/app/_probe.py": COMPLEX_16})
    _write(root)
    assert _run(root, "--tighten")[0] == 1
    assert (root / "cx.txt").read_text(encoding="utf-8") == ""


def test_unparsable_file_fails_closed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _tree(tmp_path, {"backend/app/bad.py": "def f(:\n"})
    _write(root)
    assert _run(root)[0] == 2
    assert "could not parse" in capsys.readouterr().err


def test_missing_scan_target_fails_closed(tmp_path: Path) -> None:
    _write(tmp_path)
    assert _run(tmp_path)[0] == 2


def test_wrong_ruff_version_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(tmp_path)
    _write(root)
    monkeypatch.setattr(guard, "PINNED_RUFF", "0.0.1")
    assert _run(root)[0] == 2
    assert "ruff 0.0.1 required" in capsys.readouterr().err


def test_write_baseline_requires_real_reason_and_records_findings(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"backend/app/_probe.py": COMPLEX_16})
    assert _run(root, "--write-baseline", "--only", "complexity", "--reason", "")[0] == 2
    reason = "complexity: decomposition planned"
    assert _run(root, "--write-baseline", "--only", "complexity", "--reason", reason)[0] == 0
    assert (root / "cx.txt").read_text(
        encoding="utf-8"
    ) == f"backend/app/_probe.py::probe\t16\t{reason}\n"


def test_real_repo_passes_with_committed_baselines() -> None:
    assert guard.main([]) == 0
