"""Tests for ``scripts/guards/check_doc_references.py`` (docs naming paths that no longer exist)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_REPO = Path(__file__).resolve().parents[3]
_GUARD = _REPO / "scripts" / "guards" / "check_doc_references.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_doc_references_under_test", _GUARD)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load()


def test_missing_path_is_reported() -> None:
    assert guard.find_stale("see `backend/app/no_such_module_zzz.py`") == [
        "backend/app/no_such_module_zzz.py"
    ]


def test_existing_path_and_symbol_form_are_clean() -> None:
    text = "`backend/app/main.py` and `app/utils/url_validation.is_safe_url`"
    assert guard.find_stale(text) == []


def test_missing_dotted_module_is_reported() -> None:
    assert guard.find_stale("import app.services.no_such_zzz.thing") == [
        "app.services.no_such_zzz.thing"
    ]


def test_placeholders_and_globs_are_not_reported() -> None:
    assert guard.find_stale("`backend/tests/unit/test_lan_*` and `scripts/<name>.sh`") == []


def test_repository_docs_have_no_unlisted_stale_references(capsys) -> None:
    assert guard.main() == 0, capsys.readouterr().out
