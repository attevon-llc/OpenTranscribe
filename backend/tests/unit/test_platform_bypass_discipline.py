"""No code path may read the platform-admin flag to skip a tenant gate (issue #1122).

``User.is_admin`` used to be consulted in front of every tenant gate, so one admin
credential read, edited and deleted every tenant's content by UUID. The content bypass now
lives in exactly one place, ``PlatformBypass.allows`` (``services/platform_bypass.py``),
which a site calls AFTER it has loaded the row, passing the row's own tenant.

This gate keeps it that way. It AST-scans ``app/api``, ``app/services`` and ``app/utils``
for every shape that reintroduces a bypass:

  * an ``.is_admin`` attribute read (``current_user.is_admin``, ``ctx.user.is_admin``)
    and ``getattr(x, "is_admin")``;
  * an ``is_admin=`` / ``allow_admin=`` keyword argument;
  * a bare ``get_file_by_uuid(`` lookup in ``app/api`` (it never reaches the tenant gate).

Each finding must be allowlisted under ``"<path relative to app/>::<enclosing function>"``
with a written reason. Only three classes of reason are legitimate, and every entry says which:

  * ``N`` -- not the platform-admin flag at all: an unrelated field that shares the name
    (an LDAP probe result's ``is_admin``).

  * ``P`` -- a platform OPERATION: the route is admin-gated as a whole, returns no tenant
    content (or is reached only after the chokepoint passed), and an admin needs it to run
    the deployment.
  * ``Q`` -- quarantine-review visibility: whether a taken-down row inside an already
    tenant-scoped result is shown. It never widens tenant scope.

A new entry here is a new place a platform admin can act without a tenant check. Write the
reason like you mean it. A STALE entry (the site is gone) fails the run, so the list cannot
rot into a blanket exemption.
"""

from __future__ import annotations

import ast
from pathlib import Path

_APP_ROOT = Path(__file__).resolve().parents[2] / "app"

_SCANNED_DIRS = ("api", "services", "utils")

#: The module that IMPLEMENTS the gate reads the flag by definition.
_EXCLUDED_FILES = {"services/platform_bypass.py"}

_FLAG_KWARGS = {"is_admin", "allow_admin"}


def _enclosing_function_names(tree: ast.AST) -> dict[int, str]:
    """Map every node id to the name of its innermost enclosing function."""
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


def _is_getattr_of_flag(call: ast.Call) -> bool:
    return (
        isinstance(call.func, ast.Name)
        and call.func.id == "getattr"
        and len(call.args) >= 2
        and isinstance(call.args[1], ast.Constant)
        and call.args[1].value == "is_admin"
    )


def _is_bare_file_lookup(call: ast.Call) -> bool:
    func = call.func
    return (isinstance(func, ast.Name) and func.id == "get_file_by_uuid") or (
        isinstance(func, ast.Attribute) and func.attr == "get_file_by_uuid"
    )


def _is_bypass_shaped(node: ast.AST, in_api: bool) -> bool:
    if isinstance(node, ast.Attribute):
        return node.attr == "is_admin" and isinstance(node.ctx, ast.Load)
    if not isinstance(node, ast.Call):
        return False
    return (
        any(kw.arg in _FLAG_KWARGS for kw in node.keywords)
        or _is_getattr_of_flag(node)
        or (in_api and _is_bare_file_lookup(node))
    )


def scan_source(source: str, rel_path: str) -> set[str]:
    """Return the ``"<path>::<function>"`` keys of every bypass-shaped site in ``source``."""
    tree = ast.parse(source)
    enclosing = _enclosing_function_names(tree)
    in_api = rel_path.startswith("api/")
    return {
        f"{rel_path}::{enclosing.get(id(node), '<module>')}"
        for node in ast.walk(tree)
        if _is_bypass_shaped(node, in_api)
    }


def scan_tree() -> set[str]:
    found: set[str] = set()
    for sub in _SCANNED_DIRS:
        for path in sorted((_APP_ROOT / sub).rglob("*.py")):
            rel = path.relative_to(_APP_ROOT).as_posix()
            if rel in _EXCLUDED_FILES:
                continue
            found |= scan_source(path.read_text(), rel)
    return found


#: ``key -> reason``. Classes: P = platform operation, Q = quarantine review, N = not the
#: platform-admin flag at all (an unrelated field that happens to share the name).
ALLOWLIST: dict[str, str] = {
    "services/support_access_lifecycle.py::_refuse_if_not_independent": (
        "P: decides who may approve a request for platform staff (a platform admin must not "
        "approve another platform admin's request, decision D2); a role check on the approver, "
        "never a content decision"
    ),
    "services/support_access_service.py::resolve_active_grant": (
        "P: a grant is usable only while its grantee still holds the platform role, so a "
        "demoted admin's grant dies with the role; it narrows access, it never widens it"
    ),
    "api/endpoints/admin.py::quarantine_media_file": (
        "P: abuse/DMCA takedown is an admin-gated platform operation; it flips a quarantine "
        "flag on the named file and returns no tenant content (plan 3.3)"
    ),
    "api/endpoints/admin.py::release_media_file": (
        "P: the inverse of the takedown above, same admin gate, state change only, no content "
        "returned"
    ),
    "api/endpoints/admin_group_mappings.py::_ldap_claims_for": (
        "N: reads the is_admin field of an LDAP probe result; not the User flag and not a "
        "content decision"
    ),
    "api/endpoints/chat/export.py::export_conversation": (
        "Q: hides a taken-down citation inside the caller's own already-scoped conversation; "
        "argument is ctx.bypass.user_is_admin"
    ),
    "api/endpoints/chat/messages.py::list_messages": (
        "Q: hides a taken-down citation inside the caller's own already-scoped conversation; "
        "argument is ctx.bypass.user_is_admin"
    ),
    "api/endpoints/comments.py::_assert_comment_file_in_scope": (
        "Q: is_hidden_for(is_admin=False) keeps a taken-down file's comments 404; it never "
        "grants anything"
    ),
    "api/endpoints/files/__init__.py::get_thumbnail": (
        "Q: is_hidden_for runs before the public-file branch and before an org is resolved; "
        "tenant access itself is decided by bypass.allows"
    ),
    "api/endpoints/files/crud.py::delete_media_file": (
        "P: is_admin only fills the cancel_and_delete/force_delete capability hints in the "
        "409 body; the delete itself is authorized by get_deletable_file(bypass=)"
    ),
    "api/endpoints/files/management.py::bulk_file_action": (
        "P: the per-file handlers skip the retry ceiling and allow force for admins after the "
        "chokepoint passed with ctx.bypass"
    ),
    "api/endpoints/files/management.py::force_delete_file": (
        "P: the route's admin precondition (force delete is a platform operation); the file is "
        "also resolved through get_deletable_file(bypass=)"
    ),
    "api/endpoints/files/management.py::get_file_status_detail": (
        "P: offers the force_delete action in the response to admins; display only, the file "
        "was resolved with the bypass"
    ),
    "api/endpoints/files/management.py::recover_stuck_files_bulk": (
        "P: admin-gated bulk stuck-file recovery (plan 3.3); repairs pipeline state and "
        "returns counts, no tenant content"
    ),
    "api/endpoints/files/management.py::retry_file_processing": (
        "P: only an admin may pass reset_retry_count and skip the retry ceiling, evaluated "
        "after the chokepoint passed with ctx.bypass"
    ),
    "api/endpoints/files/reprocess.py::dispatch_task_by_name": (
        "P: internal re-fetch of a file the caller's chokepoint already authorized "
        "(process_file_reprocess); not an HTTP entry point"
    ),
    "api/endpoints/files/reprocess.py::process_file_reprocess": (
        "P: admins skip the retry ceiling, evaluated after "
        "get_file_by_uuid_with_permission(bypass=) passed"
    ),
    "api/endpoints/files/waveform.py::generate_waveforms_for_files": (
        "P: admin-gated bulk waveform backfill (plan 3.3)"
    ),
    "api/endpoints/files/waveform.py::get_waveform_status": (
        "P: admin-gated instance-wide waveform coverage counts, no tenant content"
    ),
    "api/endpoints/media_collections.py::get_collection": (
        "Q: include_quarantined for a collection already authorized through the chokepoint; "
        "argument is ctx.bypass.user_is_admin"
    ),
    "api/endpoints/search.py::_source_counts": (
        "Q: quarantine visibility of per-source counts inside the caller's own tenant filter; "
        "argument is ctx.bypass.user_is_admin"
    ),
    "api/endpoints/search.py::get_available_filters": (
        "Q: quarantine visibility of facet counts inside the caller's own tenant filter; "
        "argument is ctx.bypass.user_is_admin"
    ),
    "api/endpoints/search.py::get_index_health": (
        "P: instance index health; non-admins get doc counts scoped to themselves, admins the "
        "platform totals (counts, no content)"
    ),
    "api/endpoints/search.py::search_match_count": (
        "Q: quarantine visibility of a count inside the caller's own tenant filter; argument "
        "is ctx.bypass.user_is_admin"
    ),
    "api/endpoints/search.py::search_suggestions": (
        "Q: quarantine visibility of suggestions inside the caller's own tenant filter; "
        "argument is ctx.bypass.user_is_admin"
    ),
    "api/endpoints/speaker_clusters.py::get_speaker_media_preview": (
        "Q: include_quarantined for a preview inside the caller's own scope; argument is "
        "ctx.bypass.user_is_admin"
    ),
    "api/endpoints/speakers.py::debug_cross_media_by_name": (
        "P: operator diagnostic, admin-gated by its dependency; its queries are tenant-scoped "
        "unless the bypass sees all in scope (single mode)"
    ),
    "api/endpoints/tags/crud.py::cleanup_unused_tags": (
        "P: deployment-wide unused-tag sweep, admin-gated, double opt-in for scope=all_users; "
        "deletes unreferenced tags and returns counts"
    ),
    "api/endpoints/tags/discovery.py::list_files_for_tag": (
        "Q: quarantine exclusion inside files already accessible to the caller; argument is "
        "ctx.bypass.user_is_admin"
    ),
    "api/endpoints/tags/operations.py::delete_tags_endpoint": (
        "P: an admin may mutate SYSTEM tags (the shared vocabulary); touches no file and no "
        "tenant content"
    ),
    "api/endpoints/tags/operations.py::get_tag_impact": (
        "P: system-tag mutation rights for an admin, same as the mutation routes; counts only"
    ),
    "api/endpoints/tags/operations.py::merge_tags_endpoint": (
        "P: an admin may mutate SYSTEM tags (the shared vocabulary); touches no file and no "
        "tenant content"
    ),
    "api/endpoints/tags/operations.py::promote_tags_endpoint": (
        "P: promotion into the shared vocabulary is an admin platform operation; touches no "
        "file by uuid"
    ),
    "api/endpoints/tags/operations.py::rename_tag_endpoint": (
        "P: an admin may mutate SYSTEM tags (the shared vocabulary); touches no file and no "
        "tenant content"
    ),
    "api/endpoints/tags/sharing.py::create_tag_share": (
        "P: system-tag mutation rights for an admin; a share grants vocabulary, not file access"
    ),
    "api/endpoints/tags/sharing.py::list_tag_shares": (
        "P: system-tag mutation rights for an admin; lists vocabulary shares, not files"
    ),
    "api/endpoints/tags/sharing.py::revoke_tag_share": (
        "P: system-tag mutation rights for an admin; a share grants vocabulary, not file access"
    ),
    "api/endpoints/tasks.py::retry_file_processing": (
        "P: admins skip the retry ceiling, evaluated after the chokepoint passed with "
        "ctx.bypass and min_permission=editor"
    ),
    "api/endpoints/transcript_segments.py::update_segment_speaker": (
        "Q: quarantine visibility of the speaker's file inside the caller's own scope; "
        "argument is ctx.bypass.user_is_admin"
    ),
    "api/endpoints/watch_sources.py::create_watch_source": (
        "P: only an admin may assign a source to another user; in multi-tenant mode the "
        "target must also be in the caller's tenant"
    ),
    "services/delete_permissions.py::get_deletable_file": (
        "Q: is_hidden_for(is_admin=False) hides a taken-down file from everyone who did not "
        "pass bypass.allows first"
    ),
    "services/directory_sync_service.py::_reconcile": (
        "N: passes legacy_admin from an LDAP probe result to the group-mapping resolver; not "
        "the User flag"
    ),
    "services/takedown_service.py::is_review_admin": (
        "Q: defines who may review quarantined content (the Q class itself); never consulted "
        "for tenant scope"
    ),
    "utils/uuid_helpers.py::get_file_by_uuid_with_permission": (
        "Q: is_hidden_for(is_admin=bypass.user_is_admin) after bypass.allows had its say; "
        "quarantine visibility only"
    ),
    "utils/uuid_helpers.py::require_speaker_access": (
        "Q: is_hidden_for(is_admin=False) keeps a taken-down file's speaker 404; it never "
        "grants anything"
    ),
}


def test_every_bypass_shaped_site_is_allowlisted_with_a_reason():
    unexplained = sorted(scan_tree() - set(ALLOWLIST))
    assert not unexplained, (
        "These sites read the platform-admin flag (or look a file up with no tenant gate). "
        "Decide the row's tenant and call ctx.bypass.allows(...) AFTER loading it, or add an "
        "ALLOWLIST entry classed P (platform operation) or Q (quarantine review) with a "
        "reason:\n  " + "\n  ".join(unexplained)
    )


def test_no_allowlist_entry_is_stale():
    stale = sorted(set(ALLOWLIST) - scan_tree())
    assert not stale, "Delete these ALLOWLIST entries; the site is gone:\n  " + "\n  ".join(stale)


def test_every_allowlist_entry_is_classed_and_explained():
    bad = [
        key
        for key, reason in ALLOWLIST.items()
        if not (reason.startswith(("P:", "Q:", "N:")) and len(reason) > 20)
    ]
    assert not bad, "Reasons must start with 'P:', 'Q:' or 'N:' and say why:\n  " + "\n  ".join(bad)


def test_the_scan_is_not_vacuous():
    """A scanner that matches nothing reports a clean tree; prove it can see the app."""
    assert len(scan_tree() | set(ALLOWLIST)) > 20


# ---------------------------------------------------------------------------
# The scanner itself: one must-fire and one must-stay-clean case per shape
# ---------------------------------------------------------------------------


def test_scanner_fires_on_an_attribute_read():
    src = "def handler(current_user):\n    if current_user.is_admin:\n        return 1\n"
    assert scan_source(src, "api/x.py") == {"api/x.py::handler"}


def test_scanner_fires_on_the_flag_keyword_arguments():
    src = "def h(u):\n    return f(1, is_admin=u)\n\ndef g(u):\n    return f(1, allow_admin=u)\n"
    assert scan_source(src, "services/x.py") == {"services/x.py::h", "services/x.py::g"}


def test_scanner_fires_on_getattr_of_the_flag():
    src = "def h(u):\n    return bool(getattr(u, 'is_admin', False))\n"
    assert scan_source(src, "api/x.py") == {"api/x.py::h"}


def test_scanner_fires_on_a_bare_file_lookup_only_in_api():
    src = "def h(db):\n    return get_file_by_uuid(db, 'x')\n"
    assert scan_source(src, "api/x.py") == {"api/x.py::h"}
    assert scan_source(src, "tasks/x.py") == set()


def test_scanner_stays_clean_on_the_bypass_api():
    src = (
        "def h(ctx, row):\n"
        "    if ctx.bypass.allows(org_id=row.organization_id, owner_id=row.user_id,\n"
        "                         need='read', resource_type='x', resource_uuid='u'):\n"
        "        return ctx.bypass.sees_all_in_scope or ctx.bypass.user_is_admin\n"
    )
    assert scan_source(src, "api/x.py") == set()


def test_scanner_ignores_a_store_to_a_field_named_is_admin():
    src = "def h(probe):\n    probe.is_admin = True\n"
    assert scan_source(src, "api/x.py") == set()
