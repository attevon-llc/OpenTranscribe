"""Only ``hold_guard`` may ask whether a file is held (#1276).

``media_file.legal_hold`` is the PLATFORM's hold. An organization hold is a different table, so
a code path that still reads ``legal_hold`` to decide "may I destroy this" quietly ignores every
organization hold: that is how eleven guards had to be found and replaced. This gate AST-scans
``app/`` for any attribute access named ``legal_hold`` and requires each to be in the module that
answers the question (``services/hold_guard``) or in the platform takedown flow that SETS the
platform flag, with a written reason. A stale entry (the site is gone) fails the run.

Self-tested: the scanner must fire on a destroy-path read and stay clean on an allowlisted one,
or an empty result is indistinguishable from a clean tree.
"""

from __future__ import annotations

import ast
from pathlib import Path

_APP_ROOT = Path(__file__).resolve().parents[2] / "app"

_PLATFORM_TAKEDOWN = (
    "The platform takedown flow: it sets, clears and reports the platform's own flag (and "
    "the origin trigger keys on it). It decides nothing about destroying a file."
)
_ORG_TAKEDOWN = (
    "The organization takedown route: it reports the platform flag of a file the organization "
    "took down, and tells an organization admin which authority holds it. Not a destroy path."
)

#: ``"<path under app/>"`` (the whole module) or ``"<path>::<function>"`` -> reason.
ALLOWED: dict[str, str] = {
    "services/hold_guard.py": "The module that answers 'is this file held' reads the flag by definition.",
    "services/takedown_service.py::quarantine_file": _PLATFORM_TAKEDOWN,
    "services/takedown_service.py::release_file": _PLATFORM_TAKEDOWN,
    "services/takedown_authority.py::current_origin": _PLATFORM_TAKEDOWN,
    "services/takedown_authority.py::origin_after_release": _PLATFORM_TAKEDOWN,
    "api/endpoints/admin.py::list_quarantined_files": _PLATFORM_TAKEDOWN,
    "api/endpoints/admin.py::quarantine_media_file": _PLATFORM_TAKEDOWN,
    "api/endpoints/admin.py::release_media_file": _PLATFORM_TAKEDOWN,
    "api/endpoints/org_admin_takedown.py::list_org_quarantined_files": _ORG_TAKEDOWN,
    "api/endpoints/org_admin_takedown.py::quarantine_org_file": _ORG_TAKEDOWN,
    "api/endpoints/org_admin_takedown.py::release_org_file": _ORG_TAKEDOWN,
}


def scan(source: str, relpath: str) -> list[str]:
    """``"<relpath>::<function>"`` for every ``.legal_hold`` access in ``source``."""
    tree = ast.parse(source)
    found: list[str] = []

    def visit(node: ast.AST, current: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = (
                child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else current
            )
            if isinstance(child, ast.Attribute) and child.attr == "legal_hold":
                found.append(f"{relpath}::{inner}")
            visit(child, inner)

    visit(tree, "<module>")
    return found


def _allowed(site: str) -> bool:
    return site in ALLOWED or site.split("::", 1)[0] in ALLOWED


def _all_sites() -> set[str]:
    sites: set[str] = set()
    for path in sorted(_APP_ROOT.rglob("*.py")):
        sites.update(scan(path.read_text(), path.relative_to(_APP_ROOT).as_posix()))
    return sites


def test_nothing_outside_the_guard_and_the_takedown_flow_reads_the_platform_flag():
    offenders = sorted(s for s in _all_sites() if not _allowed(s))
    assert not offenders, (
        "These sites read `.legal_hold` directly, which ignores organization holds. Ask "
        "services/hold_guard (hold_state, held_pred, not_held_pred, is_held) instead, or "
        "allowlist them with a reason if they only SET or REPORT the platform flag:\n  "
        + "\n  ".join(offenders)
    )


def test_every_allowlist_entry_still_matches_a_site():
    sites = _all_sites()
    stale = sorted(
        key for key in ALLOWED if not any(s == key or s.split("::", 1)[0] == key for s in sites)
    )
    assert not stale, f"stale allowlist entries (the site is gone, delete the line): {stale}"


def test_scanner_fires_on_a_destroy_path_read():
    src = "def sweep(file):\n    if file.legal_hold:\n        return\n    purge(file)\n"
    assert scan(src, "tasks/sweep.py") == ["tasks/sweep.py::sweep"]
    assert not _allowed("tasks/sweep.py::sweep")


def test_scanner_fires_on_a_query_predicate_read():
    src = "def q(db):\n    return db.query(MediaFile).filter(MediaFile.legal_hold.is_(False))\n"
    assert scan(src, "tasks/q.py") == ["tasks/q.py::q"]


def test_scanner_stays_clean_on_a_name_that_only_contains_the_word():
    src = (
        "legal_hold = 1\n"
        "def f(x):\n    return x.legal_hold_placed, x.clear_legal_hold, x.legal_holds_skipped\n"
    )
    assert scan(src, "x.py") == []


def test_an_allowlisted_site_and_a_whole_module_entry_both_count_as_allowed():
    assert _allowed("services/takedown_service.py::release_file")
    assert _allowed("services/hold_guard.py::anything_at_all")
    assert not _allowed("services/takedown_service.py::some_new_function")
