"""Nothing may ask the ``collection`` table which collections a caller can see (#1275).

``services/collection_visibility`` is the one place that answers "which collections may this
caller see, and which files does that make readable". A restricted collection (a member list,
the creator and org admins) is only hidden if EVERY surface that names a collection asks there:
one raw ``query(Collection)`` / ``select(Collection)`` / ``join(Collection)`` beside it names a
collection the caller was never meant to learn exists.

This gate AST-scans ``app/`` for the three shapes, and each finding must be allowlisted under
``"<path relative to app/>::<enclosing function>"`` with a written reason. Legitimate reasons are
a lookup by an identity the caller already holds and authorizes separately (``A``), an
owner-only surface that cannot reach a restricted collection (``O``), a write or cleanup that
names no collection to the caller (``W``), and the module that implements the rule (``M``). A
stale entry (the site is gone) fails the run, so the list cannot rot into a blanket exemption.
"""

from __future__ import annotations

import ast
from pathlib import Path

_APP_ROOT = Path(__file__).resolve().parents[2] / "app"

#: Where the rule is implemented; it reads the table by definition.
_EXCLUDED_FILES = {"services/collection_visibility.py", "services/permission_service.py"}


def _enclosing_function_names(tree: ast.AST) -> dict[int, str]:
    names: dict[int, str] = {}

    def visit(node: ast.AST, current: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = current
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                inner = child.name
            names[id(child)] = inner
            visit(child, inner)

    visit(tree, "<module>")
    return names


def _is_collection_name(node: ast.AST) -> bool:
    """``Collection`` or ``Collection.<column>``: the ORM class, never ``SpeakerCollection``."""
    if isinstance(node, ast.Name):
        return node.id == "Collection"
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "Collection"
    )


def _is_table_read(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    return name in {"query", "select", "join", "outerjoin"} and any(
        _is_collection_name(arg) for arg in node.args
    )


def scan_source(source: str, rel_path: str) -> set[str]:
    """Return the ``"<path>::<function>"`` keys of every table read in ``source``."""
    tree = ast.parse(source)
    enclosing = _enclosing_function_names(tree)
    return {
        f"{rel_path}::{enclosing.get(id(node), '<module>')}"
        for node in ast.walk(tree)
        if _is_table_read(node)
    }


def scan_tree() -> set[str]:
    found: set[str] = set()
    for path in sorted(_APP_ROOT.rglob("*.py")):
        rel = path.relative_to(_APP_ROOT).as_posix()
        if rel in _EXCLUDED_FILES or rel.startswith(("models/", "schemas/")):
            continue
        found |= scan_source(path.read_text(), rel)
    return found


#: ``key -> reason``; see the module docstring for the classes.
ALLOWLIST: dict[str, str] = {
    "api/endpoints/admin.py::_delete_user_owned_records": (
        "W: account deletion removes the user's personal collections and de-attributes the "
        "organization ones; it deletes rows, it names no collection to anyone"
    ),
    "api/endpoints/media_collections.py::_explicitly_shared_entries": (
        "A: the id set comes from PermissionService.get_accessible_collection_ids, which is "
        "collection_visibility's statement; this only narrows it to one tenant"
    ),
    "api/endpoints/media_collections.py::create_collection": (
        "W: the same-name check inside the tenant before an insert; the 400 echoes the name the "
        "caller just submitted and no other field (the accepted name-exists residual, Q6)"
    ),
    "api/endpoints/media_collections.py::get_collection": (
        "A: reloads, with its members, the collection get_collection_by_uuid_with_permission "
        "already authorized for this caller"
    ),
    "api/endpoints/media_collections.py::list_collections": (
        "A: every id set it queries by is the caller's accessible ids from "
        "collection_visibility, intersected with the tenant"
    ),
    "api/endpoints/media_collections.py::list_collections_on_files": (
        "A: restricted to visible_collection_ids in the same statement"
    ),
    "api/endpoints/media_collections.py::list_shared_collections": (
        "A: queries only the ids _explicitly_shared_entries derived from the predicate"
    ),
    "api/endpoints/media_collections.py::update_collection": (
        "W: the same-name check inside the tenant before a rename; the 400 echoes the name the "
        "caller just submitted (Q6); the collection itself was authorized by "
        "get_collection_by_uuid_with_sharing"
    ),
    "api/endpoints/prompts.py::get_prompts": (
        "O: lists the collections linked to the caller's own prompts and filters on "
        "Collection.user_id == the caller, so it can only show collections they created"
    ),
    "services/auto_label_service.py::_matches_unusable_collection": (
        "W: reads the tenant's collection names only to REFUSE an LLM suggestion that matches "
        "one the file owner cannot use; the result is a boolean that is never shown"
    ),
    "services/gdpr_erasure_service.py::_delete_owner_scoped_rows": (
        "W: Art. 17 erasure deletes the subject's collections"
    ),
    "services/gdpr_erasure_service.py::erase_org_member_data": (
        "W: Art. 17 erasure of one member's organization rows"
    ),
    "services/gdpr_erasure_service.py::erase_organization": (
        "W: Art. 17 erasure of an organization deletes its collections"
    ),
    "tasks/transcription/downstream.py::_get_collection_prompt_uuid": (
        "A: applies the default summary prompt of a collection the file is already in, for the "
        "file's own summary; no collection is named to any caller"
    ),
}


def test_every_collection_table_read_is_allowlisted_with_a_reason():
    found = scan_tree()
    missing = sorted(found - set(ALLOWLIST))
    assert not missing, (
        "These sites read the collection table directly. Ask services/collection_visibility "
        "(visible_collection_ids, visible_collections_by_uuid, usable_collections) instead, or "
        "allowlist them with a reason:\n  " + "\n  ".join(missing)
    )


def test_no_allowlist_entry_is_stale():
    stale = sorted(set(ALLOWLIST) - scan_tree())
    assert not stale, (
        "These allowlist entries no longer match a site; delete them:\n  " + "\n  ".join(stale)
    )


def test_every_allowlist_entry_carries_a_real_reason():
    for key, reason in ALLOWLIST.items():
        assert reason[:2] in {"A:", "O:", "W:"} and len(reason) > 40, key


# --- the scanner itself: a detector that matches nothing reads like a clean tree --------


def test_scanner_fires_on_each_shape():
    source = (
        "def a(db):\n    return db.query(Collection).all()\n"
        "def b(db):\n    return db.query(Collection.id).all()\n"
        "def c():\n    return select(Collection)\n"
        "def d(q):\n    return q.join(Collection, Collection.id == 1)\n"
    )
    assert scan_source(source, "x.py") == {"x.py::a", "x.py::b", "x.py::c", "x.py::d"}


def test_scanner_stays_clean_on_lookalikes():
    source = (
        "def a(db):\n    return db.query(SpeakerCollection).all()\n"
        "def b(db):\n    return db.query(CollectionMember).all()\n"
        "def c(db):\n    return db.query(Tag).filter(Collection.id == 1)\n"
    )
    assert scan_source(source, "x.py") == set()
