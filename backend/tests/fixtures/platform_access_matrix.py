"""Derive the platform-access matrix from the code (issue #1281).

Every way platform staff can act as another user or reach their content, enumerated from the
live route table and an AST scan of ``app/``, never from prose. ``docs/security/
platform-access-matrix.tsv`` is the committed rendering of ``build_rows()``; the unit test
``tests/unit/test_platform_access_matrix.py`` fails when the two differ, and
``scripts/generate-platform-access-matrix.py --write`` regenerates the file.

Two kinds of row:

* ``route``: a route reachable by a platform role or carrying the grant header. Derived from the
  dependency tree (the same walk ``test_platform_bypass_route_coverage.py`` uses), so a route that
  reaches a guard through a wrapper still counts. Self-scoped routes (``get_current_active_user``
  only) and public routes are not platform-access paths and are covered by that test instead.
* ``site``: a place in ``app/`` that decides platform access (``.allows(``), resolves a grant,
  builds a bypass, reads the grant header or widens a list to the whole scope.

Hand-written knowledge lives in ``PLATFORM_OP_RULES``, ``ALLOWS_KINDS`` and ``SITE_FORMS`` here
and in ``platform_access_matrix_tables.py`` (content-route guards, inline-role routes, scope and
reveal wideners). An item the tables do not classify is reported as ``UNCLASSIFIED``, which the
test refuses, so a new path forces a decision.

This module deliberately imports the app lazily: the AST half needs no import at all.
"""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fixtures.platform_access_matrix_tables import CONTENT_GUARD_TEXT
from fixtures.platform_access_matrix_tables import CONTENT_GUARDS
from fixtures.platform_access_matrix_tables import INLINE_ROLE_ROUTES
from fixtures.platform_access_matrix_tables import REVEAL_WIDENER_SITES
from fixtures.platform_access_matrix_tables import SCOPE_WIDENER_SITES
from fixtures.platform_access_matrix_tables import TENANT_MARKERS

BACKEND_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = BACKEND_ROOT / "app"
REPO_ROOT = BACKEND_ROOT.parent
MATRIX_PATH = REPO_ROOT / "docs" / "security" / "platform-access-matrix.tsv"

HEADER = ("kind", "key", "gate", "header", "guard", "test")

ADMIN_DEPS = frozenset(
    {"get_current_admin_user", "get_current_super_admin_user", "get_current_active_superuser"}
)

#: ``tests/api/test_support_access_matrix.py`` proves the quartet per chokepoint kind.
MATRIX_TEST = "tests/api/test_support_access_matrix.py"
ENFORCEMENT_TEST = "tests/api/test_support_access_enforcement.py"
SWEEP_TEST = f"{MATRIX_TEST}::test_a_platform_admin_without_a_grant_sees_no_tenant_content"


@dataclass(frozen=True)
class Row:
    kind: str
    key: str
    gate: str
    header: str
    guard: str
    test: str

    def cells(self) -> tuple[str, ...]:
        return (self.kind, self.key, self.gate, self.header, self.guard, self.test)


# ---------------------------------------------------------------------------------------------
# Platform-role routes: (regex over "METHOD path", category, why that category is acceptable)
# First match wins. ``grant-gated`` and ``metadata-audited`` are not in this table: they are
# derived (the handler takes the request bypass / calls ``audit_metadata_access``).
# ---------------------------------------------------------------------------------------------
PLATFORM_OP_RULES: tuple[tuple[str, str, str], ...] = (
    (
        r"^\w+ /api/support-access/",
        "grant-lifecycle",
        "requests, lists and reads the caller's own grants; a grant is created pending and is "
        "unusable until the tenant approves it (break-glass is super_admin, ticketed, notified)",
    ),
    (
        r"^\w+ /api/admin/(takedown-notices|files/\{file_uuid\}/(quarantine|release))",
        "takedown-action",
        "abuse takedown acts on a named file's quarantine state and the notice record, never "
        "returns content; by design it needs no grant (the operator must always be able to act)",
    ),
    (
        r"^\w+ /api/admin/audit-logs|^\w+ /api/admin/auth-config/audit/",
        "audit-read",
        "reads audit events (ids, actions, timestamps); no filenames, titles or transcript text",
    ),
    (
        r"^\w+ /api/(admin/users|admin/locked-accounts|admin/user-approvals|"
        r"admin/reports/account-status|admin/scim-tokens|admin/group-mappings|"
        r"admin/gdpr/erase-user|users|auth/invitations)",
        "account-admin",
        "user accounts, roles, sessions, approvals and identity mapping; no media, transcript, "
        "speaker or collection content",
    ),
    (
        r"^\w+ /api/(tasks/system/|tasks/recover-stuck-tasks|tasks/fix-inconsistent-files)",
        "state-repair",
        "pipeline-state repair by id or in bulk; returns counts and status, never content",
    ),
    (
        r"^GET /api/search/degraded-embeddings",
        "tenant-scoped-diagnostic",
        "names only the caller's own files in the active tenant (the whole tenant where the "
        "bypass sees the scope, everything in a single-tenant deployment); other tenants "
        "appear as a count",
    ),
    (
        r"^\w+ /api/(admin/(stats|timing|timing-summary|gpu-profiles|data-integrity|"
        r"embedding-consistency|imohash-recompute|media-duration-backfill|"
        r"profile-embeddings|engine-settings/metrics|settings/cache-config/clear|"
        r"settings/retention-config/(run|status))|search/(reindex|reembed-degraded|"
        r"repair-indices|models)|embeddings/migration|speaker-attributes/migration|"
        r"speakers/combined-migration)",
        "platform-ops",
        "deployment maintenance (indexes, migrations, recovery, metrics); counts and status only",
    ),
    (
        r"^\w+ /api/(admin/(settings|auth-config|first-run-wizard|system|asr-providers|"
        r"audit/content-access|hf-token|local-models|engine-settings|chat-settings|"
        r"redaction-policy|backup|directory-sync)|watch-sources/(settings|email-configs)|"
        r"llm-settings/bedrock|asr-settings/local-model)",
        "settings",
        "deployment configuration and credentials; no tenant content",
    ),
    (
        r"^\w+ /api/admin/privacy-policy",
        "settings",
        "the deployment's voiceprint and gender switches and their purge (issue #1280); returns "
        "switch state or a task id, never tenant content (the purge deletes derived voiceprint "
        "data, it reads none back)",
    ),
    (
        r"^GET /api/legal/acknowledgments",
        "settings",
        "the operator's legal acknowledgment record; deployment-level",
    ),
    (
        r"^GET /api/speakers/debug/cross-media-data",
        "self-scoped",
        "admin-gated diagnostic that reads only the caller's own speakers (user_id filter)",
    ),
)

#: Categories whose rows must be backed by ``audit_metadata_access`` in the handler.
AUDITED_ROUTES = (
    "GET /api/admin/files/quarantined",
    "GET /api/admin/settings/retention-config/preview",
    "GET /api/admin/takedown-notices",
    "GET /api/admin/takedown-notices/export",
    "GET /api/admin/takedown-notices/{notice_uuid}",
)

CATEGORY_TESTS = {
    "grant-gated": f"{MATRIX_TEST}::test_every_chokepoint_demands_a_valid_grant",
    "metadata-audited": f"{MATRIX_TEST}::test_cross_tenant_filename_listings_are_audited",
    "grant-lifecycle": "tests/api/test_support_access_lifecycle.py",
    "takedown-action": "tests/test_takedown_quarantine.py",
}


def dependency_names(dependant: Any, seen: set[int] | None = None) -> set[str]:
    seen = set() if seen is None else seen
    if id(dependant) in seen:
        return set()
    seen.add(id(dependant))
    names: set[str] = set()
    call = getattr(dependant, "call", None)
    if call is not None:
        names.add(getattr(call, "__name__", ""))
    for sub in getattr(dependant, "dependencies", []) or []:
        names |= dependency_names(sub, seen)
    return names


def handler_source(endpoint: Any) -> str:
    try:
        return inspect.getsource(endpoint)
    except (OSError, TypeError):
        return ""


def _handler_tree(source: str) -> ast.AST | None:
    try:
        return ast.parse(textwrap.dedent(source))
    except SyntaxError:
        return None


def uses_bypass(source: str) -> bool:
    """Whether the handler's CODE reads a ``bypass`` (a docstring or comment cannot count)."""
    tree = _handler_tree(source)
    if tree is None:
        return False
    return any(
        (isinstance(n, ast.Name) and n.id == "bypass")
        or (isinstance(n, ast.Attribute) and n.attr == "bypass")
        for n in ast.walk(tree)
    )


#: Attributes of the signed-in user that carry the platform role.
ROLE_ATTRS = frozenset({"is_admin", "role", "is_super_admin", "is_superuser"})
#: Names a handler gives the request context; ``<ctx>.user`` is the signed-in user.
CONTEXT_NAMES = frozenset({"ctx", "context", "req_ctx"})
USER_DEPENDENCIES = frozenset(
    {
        "get_current_user",
        "get_current_active_user",
        "get_current_admin_user",
        "get_current_active_superuser",
        "get_current_super_admin_user",
        "get_optional_current_user",
    }
)


def _function_node(tree: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            return node
    return None


def _caller_params(fn: ast.FunctionDef | ast.AsyncFunctionDef | None) -> set[str]:
    """Parameter names a handler binds to the signed-in user, whatever it calls them."""
    names = {"current_user"}
    if fn is None:
        return names
    args = fn.args
    positional = args.posonlyargs + args.args
    defaults: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults))
    defaults += list(args.defaults)
    for arg, default in [
        *zip(positional, defaults, strict=True),
        *zip(args.kwonlyargs, args.kw_defaults, strict=True),
    ]:
        if not (isinstance(default, ast.Call) and default.args):
            continue
        if getattr(default.func, "id", getattr(default.func, "attr", "")) != "Depends":
            continue
        target = default.args[0]
        if getattr(target, "id", getattr(target, "attr", "")) in USER_DEPENDENCIES:
            names.add(arg.arg)
    return names


def _reads_role_of(tree: ast.AST, is_subject: Any) -> bool:
    """Whether ``tree`` reads a role attribute (directly or via ``getattr``) of a subject."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ROLE_ATTRS and is_subject(node.value):
            return True
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "getattr"
            and len(node.args) >= 2
            and is_subject(node.args[0])
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in ROLE_ATTRS
        ):
            return True
    return False


def _names_the_context_user(node: ast.AST, names: set[str]) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "user"
        and isinstance(node.value, ast.Name)
        and node.value.id in names
    )


def reads_caller_role(source: str, helpers: dict[str, str] | None = None) -> bool:
    """Whether a handler tests the signed-in user's role anywhere it can be seen.

    Three shapes, all of them found by reading CODE (a docstring cannot count):

    * a direct read: ``current_user.is_admin`` or ``.role`` (or ``.is_super_admin`` /
      ``.is_superuser``), on the parameter bound to the user dependency under any name and on
      ``ctx.user``;
    * ``getattr(current_user, "role", ...)``, which the attribute scan alone missed;
    * a role read inside a module-level helper the handler hands the user (or the context) to,
      one call deep. ``helpers`` maps a function name to its source. A role flag passed in as a
      plain boolean is a direct read at the call site, so it is already found.

    A helper reached through two calls, a method, or another module is NOT followed; that limit
    is the reason the sweep in ``test_support_access_matrix`` exists beside this scan.
    """
    tree = _handler_tree(source)
    if tree is None:
        return False
    fn = _function_node(tree)
    callers = _caller_params(fn)

    def is_caller(node: ast.AST) -> bool:
        return (isinstance(node, ast.Name) and node.id in callers) or _names_the_context_user(
            node, set(CONTEXT_NAMES)
        )

    if _reads_role_of(tree, is_caller):
        return True
    for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
        name = getattr(call.func, "id", None)
        helper_source = (helpers or {}).get(name or "")
        if not helper_source:
            continue
        helper_tree = _handler_tree(helper_source)
        helper_fn = _function_node(helper_tree) if helper_tree is not None else None
        if helper_tree is None or helper_fn is None:
            continue
        params = [a.arg for a in helper_fn.args.posonlyargs + helper_fn.args.args]
        handed: set[str] = set()
        for index, arg in enumerate(call.args):
            if (is_caller(arg) or _is_context(arg)) and index < len(params):
                handed.add(params[index])
        for keyword in call.keywords:
            if keyword.arg and (is_caller(keyword.value) or _is_context(keyword.value)):
                handed.add(keyword.arg)
        if handed and _reads_role_of(
            helper_tree,
            lambda node, handed=handed: (
                (isinstance(node, ast.Name) and node.id in handed)
                or _names_the_context_user(node, handed)
            ),
        ):
            return True
    return False


def _is_context(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id in CONTEXT_NAMES


def _header_state(names: set[str]) -> str:
    if "refuse_under_support_grant" in names:
        return "refused"
    if "get_current_context" in names:
        return "honoured"
    if "get_base_context" in names:
        return "ignored"
    return "n/a"


def classify_route(
    method: str,
    path: str,
    names: set[str],
    source: str,
    content_guards: dict[str, str] | None = None,
    inline_routes: dict[str, tuple[str, str]] | None = None,
    helpers: dict[str, str] | None = None,
) -> list[Row]:
    """The matrix rows for one route: none when it is not a platform-access path.

    A route has its dependency-derived row (content, org-admin, lifecycle, platform-op) AND, when
    the handler tests the caller's role itself, a second ``platform-op:inline:*`` row. The role
    test is looked for on EVERY route: a handler that takes the request context and then asks
    ``current_user.is_admin`` is not "caller-scoped" just because the context was injected.
    """
    inline_routes = INLINE_ROLE_ROUTES if inline_routes is None else inline_routes
    key = f"{method} {path}"
    rows: list[Row] = []
    row = _dependency_row(method, path, names, source, content_guards)
    if row is not None:
        rows.append(row)
    if reads_caller_role(source, helpers):
        category, why = inline_routes.get(
            key, ("UNCLASSIFIED", "add the route to INLINE_ROLE_ROUTES with a written reason")
        )
        rows.append(
            Row(
                "route",
                key,
                f"platform-op:inline:{category}",
                _header_state(names),
                why,
                SWEEP_TEST,
            )
        )
    return rows


def _dependency_row(
    method: str,
    path: str,
    names: set[str],
    source: str,
    content_guards: dict[str, str] | None,
) -> Row | None:
    """The row the route's dependency tree earns, or ``None`` for a self-scoped/public route."""
    content_guards = CONTENT_GUARDS if content_guards is None else content_guards
    key = f"{method} {path}"
    header = _header_state(names)
    admin = bool(names & ADMIN_DEPS)
    if "require_org_admin" in names:
        return Row(
            "route",
            key,
            "org-admin",
            header,
            "require_org_admin on the base context; a grant never confers org:admin",
            f"{ENFORCEMENT_TEST}; tests/api/test_support_access_lifecycle.py",
        )
    if admin:
        return _platform_op(key, header, names, source)
    if "get_current_context" in names:
        if "refuse_under_support_grant" in names:
            return Row(
                "route",
                key,
                "content-refused",
                header,
                "refuse_under_support_grant: 403 support_grant_action_not_permitted",
                f"{ENFORCEMENT_TEST}::test_a_grant_is_refused_on_chat_search_upload_export_and_admin",
            )
        guard = CONTENT_GUARD_TEXT.get(
            content_guards.get(key, ""),
            "UNCLASSIFIED: add the route to CONTENT_GUARDS in platform_access_matrix_tables.py",
        )
        return Row(
            "route",
            key,
            "content",
            header,
            guard,
            f"{MATRIX_TEST}::test_every_chokepoint_demands_a_valid_grant; {SWEEP_TEST}",
        )
    if "get_base_context" in names:
        return Row(
            "route",
            key,
            "lifecycle",
            header,
            "base context: the grant header is never read, so a stale one cannot break it",
            f"{ENFORCEMENT_TEST}::test_lifecycle_routes_ignore_a_stale_header",
        )
    return None


def _platform_op(key: str, header: str, names: set[str], source: str) -> Row:
    tier = "admin"
    if names & {"get_current_super_admin_user", "get_current_active_superuser"}:
        tier = "super_admin"
    if key in AUDITED_ROUTES:
        category = "metadata-audited"
        guard = "audit_metadata_access in the handler (one PLATFORM_ADMIN_METADATA_ACCESS event)"
    elif "get_current_context" in names and uses_bypass(source):
        category = "grant-gated"
        guard = "bypass.allows / sees_all_in_scope on the request context"
    else:
        category = "UNCLASSIFIED"
        guard = "no rule matches; classify it in PLATFORM_OP_RULES"
        for pattern, name, why in PLATFORM_OP_RULES:
            if re.search(pattern, key):
                category, guard = name, why
                break
    test = CATEGORY_TESTS.get(category, SWEEP_TEST)
    return Row("route", key, f"platform-op:{tier}:{category}", header, guard, test)


def handler_sources(app: Any) -> dict[str, str]:
    """``"METHOD path"`` -> source of the handler serving it, for every API route."""
    from fastapi.routing import APIRoute

    sources: dict[str, str] = {}
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in sorted(route.methods):
                sources[f"{method} {route.path}"] = handler_source(route.endpoint)
    return sources


def module_helpers(endpoint: Any, cache: dict[str, dict[str, str]]) -> dict[str, str]:
    """Source of every function defined in the handler's own module, for the one-call-deep scan."""
    module = inspect.getmodule(endpoint)
    if module is None:
        return {}
    if module.__name__ not in cache:
        cache[module.__name__] = {
            name: handler_source(func)
            for name, func in inspect.getmembers(module, inspect.isfunction)
            if func.__module__ == module.__name__
        }
    return cache[module.__name__]


def route_rows(app: Any) -> list[Row]:
    rows: list[Row] = []
    helper_cache: dict[str, dict[str, str]] = {}
    for route in app.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        names = dependency_names(dependant)
        source = handler_source(route.endpoint)
        helpers = module_helpers(route.endpoint, helper_cache)
        for method in sorted(getattr(route, "methods", None) or ()):
            if method in {"HEAD", "OPTIONS"}:
                continue
            rows.extend(classify_route(method, route.path, names, source, None, None, helpers))
    return rows


# ---------------------------------------------------------------------------------------------
# Non-route sites, found by AST
# ---------------------------------------------------------------------------------------------

#: ``allows`` call sites, by the kind of row they decide. ``rel::function`` -> chokepoint kind.
ALLOWS_KINDS: dict[str, str] = {
    "utils/uuid_helpers.py::require_resource_owner": "media_file",
    "utils/uuid_helpers.py::get_file_by_uuid_with_permission": "media_file",
    "utils/uuid_helpers.py::get_collection_by_uuid_with_permission": "collection",
    "utils/uuid_helpers.py::get_collection_by_uuid_with_sharing": "collection",
    "utils/uuid_helpers.py::require_speaker_access": "speaker",
    "utils/uuid_helpers.py::require_profile_in_scope": "speaker_profile",
    "services/delete_permissions.py::get_deletable_file": "media_file",
    "services/delete_permissions.py::can_delete_file": "media_file",
    "services/tag_bulk.py::_require_editor": "media_file",
    "api/endpoints/transcript_segments.py::_get_new_speaker_id": "speaker",
    "api/endpoints/media_collections.py::get_collection_media": "collection",
    "api/endpoints/comments.py::_assert_comment_file_in_scope": "comment",
    "api/endpoints/comments.py::delete_comment": "comment",
    "api/endpoints/watch_sources.py::_get_source_or_404": "watch_source",
    "api/endpoints/watch_sources.py::create_watch_source": "watch_source_owner",
    "api/endpoints/prompts.py::share_prompt": "prompt",
    "api/endpoints/files/crud.py::get_media_file_by_id": "media_file",
    "api/endpoints/files/crud.py::get_media_file_detail": "media_file",
    "api/endpoints/files/__init__.py::get_thumbnail": "thumbnail",
}

#: Attribute reads that widen a request's reach, and the call names that create one.
WIDENERS = {"sees_all_in_scope": "scope-widener", "admin_reveal_allowed": "reveal-widener"}
CALLS = {
    "allows": "allows",
    "build_bypass": "bypass-builder",
    "resolve_request_bypass": "grant-resolver",
    "resolve_active_grant": "grant-resolver",
    "PlatformBypass": "bypass-builder",
    "SupportGrantView": "bypass-builder",
}

#: Where each form may legitimately appear, with the reason. An occurrence elsewhere is a new
#: path and shows as UNCLASSIFIED until a human adds it here.
SITE_FORMS: dict[str, dict[str, str]] = {
    "header-reader": {
        "api/deps_context.py::resolve_request_bypass": (
            "the only reader of X-Support-Access-Grant: validates the grant on every request"
        ),
        "api/deps_context.py::get_current_context": (
            "tests for the header's presence only, then defers to resolve_request_bypass"
        ),
    },
    "grant-resolver": {
        "api/deps_context.py::resolve_request_bypass": (
            "resolve_active_grant: own grant, active, live target, grantee still an admin"
        ),
        "api/endpoints/files/__init__.py::get_thumbnail": (
            "optional-auth route; calls the same resolve_request_bypass the context uses"
        ),
        "api/deps_context.py::get_current_context": (
            "defers to resolve_request_bypass when the header is present"
        ),
    },
    "bypass-builder": {
        "api/deps_context.py::get_base_context": "no grant: the platform role alone, mode-gated",
        "api/deps_context.py::resolve_request_bypass": "the only place a grant becomes a bypass",
        "services/support_access_service.py::resolve_active_grant": (
            "builds the frozen view of a grant that has just passed every check"
        ),
    },
    "scope-widener": SCOPE_WIDENER_SITES,
    "reveal-widener": REVEAL_WIDENER_SITES,
}

SITE_TEST = {
    "allows": f"{MATRIX_TEST}::test_every_chokepoint_demands_a_valid_grant",
    "header-reader": f"{ENFORCEMENT_TEST}::test_an_unknown_grant_is_invalid",
    "grant-resolver": f"{ENFORCEMENT_TEST}::test_the_grant_uuid_of_another_admin_is_refused",
    "bypass-builder": f"{ENFORCEMENT_TEST}::test_when_the_use_row_cannot_be_written_nothing_is_served",
    "scope-widener": f"{MATRIX_TEST}::test_an_org_grant_widens_lists_only_inside_its_tenant",
    "reveal-widener": f"{ENFORCEMENT_TEST}::test_redaction_is_resolved_for_the_file_owner_under_a_grant",
}


def _enclosing(tree: ast.AST) -> dict[int, str]:
    names: dict[int, str] = {}

    def visit(node: ast.AST, current: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = (
                child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else current
            )
            names[id(child)] = inner
            visit(child, inner)

    visit(tree, "<module>")
    return names


def scan_source(source: str, rel_path: str) -> set[tuple[str, str]]:
    """``(form, "rel::function")`` for every platform-access site in ``source``."""
    tree = ast.parse(source)
    enclosing = _enclosing(tree)
    found: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        form = None
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name == "allows" and isinstance(func, ast.Attribute):
                form = "allows"
            elif name in CALLS and name != "allows":
                form = CALLS[name]
        elif isinstance(node, ast.Attribute) and node.attr in WIDENERS:
            form = WIDENERS[node.attr]
        elif isinstance(node, ast.Name) and node.id == "SUPPORT_ACCESS_GRANT_HEADER":
            form = "header-reader"
        if form is not None:
            found.add((form, f"{rel_path}::{enclosing.get(id(node), '<module>')}"))
    return found


#: The implementation of the gate reads its own flags by definition; the constant is declared in
#: core/constants.py and re-exported nowhere else.
EXCLUDED_FILES = frozenset({"services/platform_bypass.py", "core/constants.py"})


def scan_tree(app_root: Path = APP_ROOT) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted(app_root.rglob("*.py")):
        rel = path.relative_to(app_root).as_posix()
        if rel in EXCLUDED_FILES:
            continue
        found |= scan_source(path.read_text(), rel)
    return found


def site_row(form: str, key: str) -> Row:
    if form == "allows":
        kind = ALLOWS_KINDS.get(key)
        guard = (
            f"chokepoint ({kind}): row loaded first, tenant decided on the row, use row written"
            if kind
            else "UNCLASSIFIED: add the function to ALLOWS_KINDS"
        )
    else:
        guard = SITE_FORMS[form].get(
            key, "UNCLASSIFIED: add the site to SITE_FORMS (wideners: the *_WIDENER_SITES tables)"
        )
    return Row("site", key, form, "n/a", guard, SITE_TEST[form])


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            names.add(n.id)
        elif isinstance(n, ast.Attribute):
            names.add(n.attr)
        elif isinstance(n, ast.keyword) and n.arg:
            names.add(n.arg)
    return names


def missing_tenant_markers(
    app_root: Path = APP_ROOT, markers: dict[str, tuple[str, ...]] | None = None
) -> list[str]:
    """Scope-widener sites whose function no longer mentions the tenant predicate it relies on."""
    markers = TENANT_MARKERS if markers is None else markers
    missing: list[str] = []
    for key, wanted in sorted(markers.items()):
        rel, _, func = key.partition("::")
        path = app_root / rel
        if not path.is_file():
            missing.append(f"{key}: no such file")
            continue
        tree = ast.parse(path.read_text())
        bodies = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == func
        ]
        if not bodies:
            missing.append(f"{key}: no such function")
        elif wanted and not any(set(wanted) & _called_names(b) for b in bodies):
            missing.append(f"{key}: no longer mentions {' or '.join(wanted)}")
    return missing


def site_rows(sites: Iterable[tuple[str, str]]) -> list[Row]:
    return [site_row(form, key) for form, key in sorted(sites, key=lambda s: (s[1], s[0]))]


def render(rows: Iterable[Row]) -> str:
    ordered = sorted(rows, key=lambda r: (r.kind, r.key, r.gate))
    lines = ["\t".join(HEADER)] + ["\t".join(r.cells()) for r in ordered]
    return "\n".join(lines) + "\n"


def parse(text: str) -> list[Row]:
    lines = [line for line in text.splitlines() if line.strip()]
    assert tuple(lines[0].split("\t")) == HEADER, "unexpected matrix header"
    return [Row(*line.split("\t")) for line in lines[1:]]


def build_rows(app: Any) -> list[Row]:
    return route_rows(app) + site_rows(scan_tree())
