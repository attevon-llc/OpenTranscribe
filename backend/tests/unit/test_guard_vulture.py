"""Behavioural tests for ``scripts/guards/check_vulture.py``.

Each case builds a throwaway tree and runs the real guard (and so the real, pinned vulture)
against it, so a guard that scans nothing, ignores the whitelist or lets a registered route
count as dead fails here.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_GUARD_PATH = Path(__file__).resolve().parents[3] / "scripts" / "guards" / "check_vulture.py"


def _load_guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_vulture_under_test", _GUARD_PATH)
    assert spec is not None and spec.loader is not None, f"cannot load {_GUARD_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load_guard()

UNUSED = "def _unused_probe_function():\n    pass\n"
KEY = "backend/app/_probe.py::function::_unused_probe_function"
TAGGED_WHITELIST = (
    "from types import SimpleNamespace\n_ = SimpleNamespace()\nW = (\n"
    "    # [test-observation] a unit test asserts the invariant through this helper\n"
    "    _._unused_probe_function,\n)\n"
)
REASON = "residual after the dead-code pass; delete or whitelist when touched"


def _tree(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    for target in guard.TARGETS:
        (tmp_path / target).mkdir(parents=True, exist_ok=True)
    for rel, body in (files or {}).items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return tmp_path


def _run(root: Path, baseline: str = "", *extra: str) -> int:
    path = root / "baseline.txt"
    path.write_text(baseline, encoding="utf-8")
    code: int = guard.main(["--root", str(root), "--baseline", str(path), *extra])
    return code


def test_planted_unused_function_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _tree(tmp_path, {"backend/app/_probe.py": UNUSED})
    assert _run(root) == 1
    assert f"{KEY}: 1 finding(s) (new violation" in capsys.readouterr().err


def test_used_function_passes(tmp_path: Path) -> None:
    body = "def helper():\n    return 1\n\n\nVALUE = helper()\n"
    root = _tree(tmp_path, {"backend/app/ok.py": body})
    assert _run(root) == 0


def test_whitelisted_name_passes(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "backend/app/_probe.py": UNUSED,
            guard.WHITELIST: TAGGED_WHITELIST,
        },
    )
    assert _run(root) == 0


def test_decorated_route_handler_passes(tmp_path: Path) -> None:
    body = (
        "from fastapi import APIRouter\n\nrouter = APIRouter()\n\n\n"
        "@router.get('/x')\ndef handler():\n    return 1\n"
    )
    root = _tree(tmp_path, {"backend/app/routes.py": body})
    assert _run(root) == 0


def test_baselined_finding_passes_and_extra_one_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"backend/app/_probe.py": UNUSED})
    assert _run(root, f"{KEY}\t1\t{REASON}\n") == 0
    (root / "backend/app/_probe.py").write_text(
        UNUSED + "\n\ndef _another_unused():\n    pass\n", encoding="utf-8"
    )
    assert _run(root, f"{KEY}\t1\t{REASON}\n") == 1


def test_stale_entry_fails_and_tighten_repairs(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"backend/app/ok.py": "X = 1\n"})
    stale = f"{KEY}\t1\t{REASON}\n"
    assert _run(root, stale) == 1
    assert _run(root, stale, "--tighten") == 0
    assert (root / "baseline.txt").read_text(encoding="utf-8") == ""


def test_class_body_variable_is_out_of_scope(tmp_path: Path) -> None:
    body = "class Model:\n    never_read_field: int = 0\n\n\nM = Model()\n"
    root = _tree(tmp_path, {"backend/app/m.py": body})
    assert _run(root) == 0


def test_tests_directory_is_not_scanned(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"backend/tests/t.py": UNUSED})
    assert _run(root) == 0


def test_missing_targets_fail_closed(tmp_path: Path) -> None:
    assert _run(tmp_path) == 2


@pytest.mark.parametrize(
    "block",
    [
        "    _._unused_probe_function,\n",
        "    # a plain comment with a long enough explanation but no tag at all\n"
        "    _._unused_probe_function,\n",
        "    # [extension-seam]\n    _._unused_probe_function,\n",
        "    # [extension-seam] too short\n    _._unused_probe_function,\n",
        "    # [other-tag] a long enough reason that names an unknown class of entry\n"
        "    _._unused_probe_function,\n",
    ],
    ids=["no-comment", "no-tag", "no-reason", "short-reason", "unknown-tag"],
)
def test_whitelist_block_without_tag_and_reason_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], block: str
) -> None:
    whitelist = f"from types import SimpleNamespace\n_ = SimpleNamespace()\nW = (\n{block})\n"
    root = _tree(tmp_path, {"backend/app/_probe.py": UNUSED, guard.WHITELIST: whitelist})
    assert _run(root) == 1
    assert "untagged vulture whitelist block" in capsys.readouterr().err


def test_second_block_is_checked_independently(tmp_path: Path) -> None:
    whitelist = (
        "from types import SimpleNamespace\n_ = SimpleNamespace()\nW = (\n"
        "    # [extension-seam] the cloud edition registers this at startup\n"
        "    _._unused_probe_function,\n"
        "    # no tag on this one even though it has a comment\n"
        "    _._another,\n)\n"
    )
    root = _tree(tmp_path, {"backend/app/_probe.py": UNUSED, guard.WHITELIST: whitelist})
    assert _run(root) == 1


def test_shipped_whitelist_is_fully_tagged() -> None:
    path = _GUARD_PATH.parent / "vulture_whitelist.py"
    assert guard.whitelist_problems(path.read_text(encoding="utf-8")) == []
