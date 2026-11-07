"""Only ``file_availability`` may decide whether a file may appear on a read surface (#1101).

A file is unavailable when it is trashed (``deleted_at``) or quarantined (``is_quarantined``)
and the platform role did not reach it. Before this rule existed, quarantine was a predicate
placed by hand at ~60 read sites, and the sites that forgot it leaked taken-down files (the
owners facet, the collections-on-files lookup, the management lookup, speaker profile counts
and the cluster lists). A read path that filters either flag by hand ignores the other one: a
hand-written ``is_quarantined IS FALSE`` shows a trashed file.

This gate AST-scans ``app/`` for every read of ``.is_quarantined`` or ``.deleted_at`` (and
``getattr(x, "is_quarantined" | "deleted_at", ...)``) and requires each to be in
``services/file_availability`` or allowlisted here with a written reason: the takedown actions
that SET the flag, the review surfaces that must list unavailable files, and the destroy paths
that must see them. A stale entry (the site is gone) fails the run.

Self-tested below: the scanner must fire on a hand-written read predicate and stay clean on an
allowlisted one, or an empty result is indistinguishable from a clean tree.
"""

from __future__ import annotations

import ast
from pathlib import Path

_APP_ROOT = Path(__file__).resolve().parents[2] / "app"

#: The two columns the availability rule is made of.
FLAGS = frozenset({"is_quarantined", "deleted_at"})

_TAKEDOWN_ACTION = (
    "The takedown action itself: it sets, clears and reconciles the flag. It decides nothing "
    "about what a read surface shows."
)
_REVIEW = (
    "A takedown review surface: it lists or acts on quarantined files on purpose, behind the "
    "platform or organization takedown authority."
)
_DESTROY = (
    "A destroy path that must SEE unavailable rows to protect them: quarantined files are "
    "takedown evidence it must skip. Filtering through the read rule would hide them from the "
    "guard instead."
)
_REVOCATION = (
    "Storage-side takedown enforcement: it tags or revokes the objects of a quarantined file. "
    "Quarantine-specific by definition; the trash keeps objects as they are until purge."
)

#: ``"<path under app/>"`` (the whole module) or ``"<path>::<function>"`` -> reason.
ALLOWED: dict[str, str] = {
    "services/file_availability.py": "The module that answers 'is this file available'.",
    "services/takedown_service.py::apply_processing_status": _TAKEDOWN_ACTION,
    "services/takedown_service.py::stamp_completion_time": _TAKEDOWN_ACTION,
    "services/takedown_service.py::_read": _TAKEDOWN_ACTION,
    "services/takedown_service.py::quarantine_file": _TAKEDOWN_ACTION,
    "services/takedown_service.py::release_file": _TAKEDOWN_ACTION,
    "services/takedown_authority.py::current_origin": _TAKEDOWN_ACTION,
    "services/takedown_authority.py::origin_after_release": _TAKEDOWN_ACTION,
    "services/takedown_notice_service.py::transition_link": _TAKEDOWN_ACTION,
    "startup/maintenance.py::_backfill_quarantine_tags": _REVOCATION,
    "tasks/playback_rendition.py::_quarantined": _REVOCATION,
    "services/media_revocation_cache.py::any_quarantined": _REVOCATION,
    "api/endpoints/admin_quarantine.py::list_quarantined_files": _REVIEW,
    "api/endpoints/admin_quarantine.py::release_media_file": _REVIEW,
    "api/endpoints/org_admin_takedown.py::list_org_quarantined_files": _REVIEW,
    "api/endpoints/org_admin_takedown.py::release_org_file": _REVIEW,
    "schemas/admin.py::from_action": (
        "The quarantine/release response of both takedown routes: it REPORTS the flag of the "
        "file just acted on."
    ),
    "services/formatting_service.py::format_media_file": (
        "REPORTS the flag as the display status (QUARANTINED) of a file the caller was already "
        "allowed to load; the decision to load it was the read rule's."
    ),
    "services/privacy/purge_file_guard.py": _DESTROY,
    "tasks/cleanup.py::retention_guard_pred": (
        "The retention sweep's (and its preview's) never-take guard: held, quarantined and "
        "trashed rows are outside retention, so it must see them to exclude them."
    ),
    "services/trash_service.py": (
        "The trash action itself (#1101): it sets, clears and conditionally updates "
        "deleted_at / purge_after, and refuses to mark a quarantined row for purge."
    ),
    "services/trash_access.py": (
        "The trash surface's own lookup and list (#1101): it selects trashed rows on purpose "
        "and still hides quarantined ones from callers the quarantine hides them from."
    ),
    "services/trash_purge.py": _DESTROY,
    "api/endpoints/files/trash.py::_row": (
        "REPORTS deleted_at on a trash list row the trash lookup already selected."
    ),
}


def scan(source: str, relpath: str) -> list[str]:
    """``"<relpath>::<function>"`` for every read of an availability flag in ``source``."""
    tree = ast.parse(source)
    found: list[str] = []

    def visit(node: ast.AST, current: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = (
                child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else current
            )
            if isinstance(child, ast.Attribute) and child.attr in FLAGS:
                found.append(f"{relpath}::{inner}")
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "getattr"
                and len(child.args) >= 2
                and isinstance(child.args[1], ast.Constant)
                and child.args[1].value in FLAGS
            ):
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


def test_nothing_outside_the_rule_and_the_allowlist_reads_an_availability_flag():
    offenders = sorted(s for s in _all_sites() if not _allowed(s))
    assert not offenders, (
        "These sites read `is_quarantined` / `deleted_at` directly, so they apply one half of "
        "the availability rule and miss the other. Use services/file_availability "
        "(exclude_unavailable, available_pred, unavailable_pred, is_unavailable_for, "
        "is_unavailable, unavailable_among) instead, or allowlist them with a reason if they "
        "SET, REPORT or must SEE unavailable rows:\n  " + "\n  ".join(offenders)
    )


def test_every_allowlist_entry_still_matches_a_site():
    sites = _all_sites()
    stale = sorted(
        key for key in ALLOWED if not any(s == key or s.split("::", 1)[0] == key for s in sites)
    )
    assert not stale, f"stale allowlist entries (the site is gone, delete the line): {stale}"


def test_the_models_and_migrations_are_not_scanned_as_reads():
    """A column declaration is an assignment, not a read; the scan must not need them listed."""
    assert not any(s.startswith("models/") for s in _all_sites())


# --- self-test: the scanner fires, and stays quiet where it should ---------------------------


def test_scanner_fires_on_a_hand_written_quarantine_predicate():
    src = "def owners(q):\n    return q.filter(MediaFile.is_quarantined.is_(False))\n"
    assert scan(src, "api/endpoints/files/x.py") == ["api/endpoints/files/x.py::owners"]
    assert not _allowed("api/endpoints/files/x.py::owners")


def test_scanner_fires_on_a_hand_written_trash_predicate():
    src = "def gallery(q):\n    return q.filter(MediaFile.deleted_at.is_(None))\n"
    assert scan(src, "api/endpoints/files/listing.py") == [
        "api/endpoints/files/listing.py::gallery"
    ]


def test_scanner_fires_on_a_per_row_check_and_a_getattr():
    src = (
        "def detail(f):\n    if f.is_quarantined:\n        raise E\n"
        "def other(f):\n    return getattr(f, 'deleted_at', None)\n"
    )
    assert scan(src, "x.py") == ["x.py::detail", "x.py::other"]


def test_scanner_stays_clean_on_the_helper_calls_and_lookalike_names():
    src = (
        "def f(q, row, ctx):\n"
        "    q = exclude_unavailable(q, include_quarantined=unavailable_list_exempt(ctx.bypass))\n"
        "    payload = {'is_quarantined': True}\n"
        "    return row.quarantined_at, row.deleted_at_utc, getattr(row, 'is_quarantined_x', 0)\n"
    )
    assert scan(src, "x.py") == []


def test_an_allowlisted_site_and_a_whole_module_entry_both_count_as_allowed():
    assert _allowed("services/takedown_service.py::release_file")
    assert _allowed("services/file_availability.py::anything_at_all")
    assert not _allowed("services/takedown_service.py::some_new_function")
