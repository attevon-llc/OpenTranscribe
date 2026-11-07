"""Erasure and account deletion must reach unavailable files too (#1101, plan §2.5 / §4.3).

GDPR erasure, account deletion, the account purge plan, the erasure receipt and
``purge_media_file`` select files by owner or organization and destroy them. A trashed file is
still the person's data, and Art. 17 says "without undue delay": erasure must destroy it now, not
wait for the trash window. A quarantined file is handled by the hold rules, not by hiding it. So
none of these modules may filter through ``app.services.file_availability``: one call there and
an erasure would silently skip every trashed file and report success.

Self-tested: the scanner must fire on each import form and stay quiet on an unrelated module.
"""

from __future__ import annotations

import ast
from pathlib import Path

_APP_ROOT = Path(__file__).resolve().parents[2] / "app"
_MODULE = "app.services.file_availability"

#: Paths under ``app/`` (a directory means every module below it).
DESTROY_PATHS = (
    "services/gdpr_erasure_service.py",
    "services/user_deletion_service.py",
    "services/account_purge.py",
    "services/file_cleanup_service.py",
    "services/erasure_receipt",
)


def imports_availability(source: str) -> bool:
    """Whether ``source`` imports the availability module, or anything from it, in any form."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import) and any(
            a.name == _MODULE or a.name.startswith(_MODULE + ".") for a in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom):
            if node.module == _MODULE:
                return True
            if node.module == "app.services" and any(
                a.name == "file_availability" for a in node.names
            ):
                return True
    return False


def _modules() -> list[Path]:
    found: list[Path] = []
    for rel in DESTROY_PATHS:
        path = _APP_ROOT / rel
        found.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
    return found


def test_every_destroy_path_exists():
    """A renamed module would make the guard below pass by scanning nothing."""
    missing = [rel for rel in DESTROY_PATHS if not (_APP_ROOT / rel).exists()]
    assert not missing, f"update DESTROY_PATHS, these moved: {missing}"
    assert len(_modules()) >= len(DESTROY_PATHS)


def test_no_erasure_or_purge_module_filters_by_availability():
    offenders = [
        str(p.relative_to(_APP_ROOT)) for p in _modules() if imports_availability(p.read_text())
    ]
    assert not offenders, (
        "These erasure/purge modules import file_availability, so they would skip trashed "
        f"(or quarantined) files instead of destroying them: {offenders}"
    )


def test_scanner_fires_on_every_import_form():
    assert imports_availability("from app.services.file_availability import exclude_unavailable")
    assert imports_availability("from app.services import file_availability")
    assert imports_availability("import app.services.file_availability as fa")
    assert imports_availability(
        "def f():\n    from app.services.file_availability import available_pred\n"
    )


def test_scanner_stays_quiet_on_other_imports():
    assert not imports_availability("from app.services.hold_guard import held_pred")
    assert not imports_availability("from app.services import file_cleanup_service")
