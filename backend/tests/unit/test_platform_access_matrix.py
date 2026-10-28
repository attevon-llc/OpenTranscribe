"""Every way platform staff can reach a tenant is a row in the committed matrix (issue #1281).

``docs/security/platform-access-matrix.tsv`` is derived from the live route table and an AST scan
of ``app/`` (``tests/fixtures/platform_access_matrix.py``). This file keeps it honest:

* a route or code site that reaches a tenant, or asks the platform bypass anything, and has no
  row, fails the run (regenerate with ``scripts/generate-platform-access-matrix.py --write`` and
  read the diff: a new row is a new path);
* a row whose route or site is gone fails the run;
* a platform route no table can classify, or a site that is not accounted for, is
  ``UNCLASSIFIED`` and fails;
* the structural promises a category makes are checked against the handler's own source, so a
  row cannot claim ``metadata-audited`` or ``grant-gated`` for a handler that does neither;
* every ``test`` cell names a test that exists.

The behavioural half (no grant, expired, revoked, wrong tenant, wrong scope) is
``tests/api/test_support_access_matrix.py``.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fixtures import platform_access_matrix as matrix
from fixtures import platform_access_matrix_tables as tables

BACKEND = Path(__file__).resolve().parents[2]
REPO = BACKEND.parent

KNOWN_GATES = {
    "content",
    "content-refused",
    "lifecycle",
    "org-admin",
    "allows",
    "bypass-builder",
    "grant-resolver",
    "header-reader",
    "scope-widener",
    "reveal-widener",
}


@pytest.fixture(scope="module")
def derived():
    from app.main import app

    return matrix.build_rows(app)


@pytest.fixture(scope="module")
def committed():
    return matrix.parse(matrix.MATRIX_PATH.read_text(encoding="utf-8"))


def _by_key(rows):
    return {(r.kind, r.key, r.gate): r for r in rows}


def test_the_committed_matrix_is_current(derived, committed):
    now, then = _by_key(derived), _by_key(committed)
    added = sorted(set(now) - set(then))
    removed = sorted(set(then) - set(now))
    changed = sorted(k for k in set(now) & set(then) if now[k] != then[k])
    assert not (added or removed or changed), (
        "docs/security/platform-access-matrix.tsv is out of date. Run\n"
        "  backend/venv/bin/python scripts/generate-platform-access-matrix.py --write\n"
        "and READ the diff: each added row is a new way for platform staff to reach a tenant.\n"
        f"new: {added[:10]}\ngone: {removed[:10]}\nchanged: {changed[:10]}"
    )


def test_nothing_is_unclassified(derived):
    open_items = [r.key + " :: " + r.gate for r in derived if "UNCLASSIFIED" in r.gate + r.guard]
    assert not open_items, (
        "Classify these in tests/fixtures/platform_access_matrix.py (PLATFORM_OP_RULES, "
        "ALLOWS_KINDS or SITE_FORMS) with a written reason:\n  " + "\n  ".join(open_items)
    )


def test_the_derivation_is_not_vacuous(derived):
    """A scan that finds nothing would make every other test here pass."""
    routes = [r for r in derived if r.kind == "route"]
    assert len(routes) > 300
    assert {r.gate for r in routes if r.gate in KNOWN_GATES} >= {"content", "content-refused"}
    assert any(r.gate.startswith("platform-op:super_admin:") for r in routes)
    assert sum(1 for r in derived if r.gate == "allows") >= 10
    assert any(r.gate == "header-reader" for r in derived)


def test_every_gate_is_a_known_one(derived):
    unknown = sorted(
        {
            r.gate
            for r in derived
            if r.gate not in KNOWN_GATES and not r.gate.startswith("platform-op:")
        }
    )
    assert not unknown, f"unknown gate(s) in the matrix: {unknown}"


def test_the_grant_header_has_exactly_one_reader(derived):
    readers = {r.key for r in derived if r.gate == "header-reader"}
    assert readers == {
        "api/deps_context.py::get_current_context",
        "api/deps_context.py::resolve_request_bypass",
    }, readers


def test_a_grant_is_only_ever_built_from_a_validated_header(derived):
    """``resolve_active_grant`` is called from the request resolver and nowhere else."""
    callers = {r.key for r in derived if r.gate == "grant-resolver"}
    assert callers == {
        "api/deps_context.py::get_current_context",
        "api/deps_context.py::resolve_request_bypass",
        "api/endpoints/files/__init__.py::get_thumbnail",
    }, callers


def test_org_admin_routes_never_honour_the_grant_header(derived):
    """A grant never confers ``org:admin``: those routes ignore it or refuse it."""
    honoured = [r.key for r in derived if r.gate == "org-admin" and r.header == "honoured"]
    assert not honoured, honoured


def test_admin_prefix_routes_refuse_a_grant(derived):
    """``/api/admin`` is a platform surface a grant never reaches (router-level refusal)."""
    leaks = [
        r.key
        for r in derived
        if r.kind == "route"
        and r.gate.startswith("platform-op:")
        and " /api/admin/" in r.key
        and r.header != "refused"
    ]
    assert not leaks, leaks


def test_metadata_audited_routes_call_the_audit_in_their_handler(derived):
    """The category promises one PLATFORM_ADMIN_METADATA_ACCESS event; the handler must emit it."""
    from app.main import app

    sources = matrix.handler_sources(app)
    audited = [r.key for r in derived if r.gate.endswith(":metadata-audited")]
    assert sorted(audited) == sorted(matrix.AUDITED_ROUTES)
    missing = [k for k in audited if "audit_metadata_access(" not in sources[k]]
    assert not missing, f"handlers that no longer audit: {missing}"
    # and the reverse: a platform handler that audits must say so
    undeclared = [
        k
        for k, src in sources.items()
        if "audit_metadata_access(" in src and k not in matrix.AUDITED_ROUTES
    ]
    assert not undeclared, f"audited handlers missing from AUDITED_ROUTES: {undeclared}"


def test_grant_gated_platform_routes_take_the_request_bypass(derived):
    from app.main import app

    sources = matrix.handler_sources(app)
    gated = [r.key for r in derived if r.gate.endswith(":grant-gated")]
    assert set(gated) >= {
        "GET /api/speakers/debug/cross-media-by-name",
        "POST /api/tasks/system/fix-file/{file_uuid}",
    }, gated
    # in CODE: a docstring that says "bypasses the schedule" must not make a route grant-gated
    assert all(matrix.uses_bypass(sources[k]) for k in gated), gated
    assert not any("is_admin" in sources[k] for k in gated), "a gated route reads the flag itself"


def test_deployment_scope_routes_ask_for_the_right_in_their_code(derived):
    """The category promises the handler refuses a caller without the deployment-scope right."""
    from app.main import app

    sources = matrix.handler_sources(app)
    scoped = {r.key for r in derived if r.gate.endswith(":deployment-scope")}
    assert scoped >= {
        "POST /api/search/reindex",
        "POST /api/search/reindex/stop",
        "POST /api/search/repair-indices",
        "POST /api/search/reembed-degraded",
        "PUT /api/search/models/neural/active",
        "POST /api/admin/data-integrity",
    }, sorted(scoped)
    assert all(matrix.uses_deployment_scope(sources[k]) for k in scoped), sorted(scoped)
    # and the reverse: a platform route that asks for the right must say so
    undeclared = [
        r.key
        for r in derived
        if r.gate.startswith("platform-op:")
        and not r.key.startswith("GET ")
        and matrix.uses_deployment_scope(sources.get(r.key, ""))
        and not r.gate.endswith(":deployment-scope")
    ]
    assert not undeclared, undeclared


def test_a_docstring_is_not_a_deployment_scope_check():
    code = 'def f(ctx):\n    ctx.bypass.require_deployment_scope("Reindexing")\n'
    prose = 'def f(ctx):\n    """Needs ``deployment_scope_writable``."""\n    return 1  # deployment_scope_writable\n'
    assert matrix.uses_deployment_scope(code) is True
    assert matrix.uses_deployment_scope(prose) is False


def test_every_content_route_has_an_explicit_guard_that_its_code_bears_out(derived):
    """``CONTENT_GUARDS`` is the claim; the handler's own code is the check on it."""
    from app.main import app

    sources = matrix.handler_sources(app)
    content = {r.key for r in derived if r.kind == "route" and r.gate == "content"}
    assert content, "no content routes found"
    assert sorted(content - set(tables.CONTENT_GUARDS)) == [], "content routes without an entry"
    assert sorted(set(tables.CONTENT_GUARDS) - content) == [], "entries for routes that are gone"
    wrong = [
        key
        for key in content
        if (tables.CONTENT_GUARDS[key] in {"bypass", "bypass-scope"})
        != matrix.uses_bypass(sources[key])
    ]
    assert not wrong, f"CONTENT_GUARDS disagrees with the handler's code for: {sorted(wrong)}"


def test_inline_role_routes_table_has_no_stale_entries(derived):
    listed = {r.key for r in derived if r.gate.startswith("platform-op:inline:")}
    assert listed == set(tables.INLINE_ROLE_ROUTES), sorted(listed ^ set(tables.INLINE_ROLE_ROUTES))


def test_widener_tables_name_exactly_the_sites_the_scan_finds():
    found = matrix.scan_tree()
    scope = {key for form, key in found if form == "scope-widener"}
    reveal = {key for form, key in found if form == "reveal-widener"}
    assert scope, "the scan found no scope wideners"
    assert reveal, "the scan found no reveal wideners"
    assert scope == set(tables.SCOPE_WIDENER_SITES), sorted(scope ^ set(tables.SCOPE_WIDENER_SITES))
    assert reveal == set(tables.REVEAL_WIDENER_SITES), sorted(
        reveal ^ set(tables.REVEAL_WIDENER_SITES)
    )
    assert set(tables.TENANT_MARKERS) == set(tables.SCOPE_WIDENER_SITES)


def test_every_scope_widener_still_applies_its_tenant_predicate():
    """Dropping the tenant filter next to a widening must fail here, not only in the sweep."""
    assert matrix.missing_tenant_markers() == []


def test_every_test_cell_names_a_test_that_exists(derived):
    missing = []
    for row in derived:
        for ref in re.split(r";\s*", row.test):
            path, _, name = ref.partition("::")
            file = BACKEND / path
            if not file.is_file():
                missing.append(f"{row.key}: no file {path}")
            elif name and f"def {name}(" not in file.read_text(encoding="utf-8"):
                missing.append(f"{row.key}: no test {ref}")
    assert not sorted(set(missing)), "\n".join(sorted(set(missing)))


# ---------------------------------------------------------------------------------------------
# The derivation itself: a must-fire and a must-stay-clean case per shape
# ---------------------------------------------------------------------------------------------


def _synthetic_helper(user):
    return user.is_admin


def _add_role_routes(app):
    """Routes that test the caller's role in the ways the scan must find, and ways it must not."""
    from fastapi import Depends

    from app.api.deps_context import get_current_context
    from app.api.endpoints.auth import get_current_active_user

    @app.post("/x/context-then-role")
    def context_then_role(
        ctx=Depends(get_current_context), current_user=Depends(get_current_active_user)
    ):
        if not current_user.is_admin:
            return {}
        return {}

    @app.post("/x/renamed-user")
    def renamed_user(ctx=Depends(get_current_context), who=Depends(get_current_active_user)):
        return who.role

    @app.post("/x/getattr-role")
    def getattr_role(
        ctx=Depends(get_current_context), current_user=Depends(get_current_active_user)
    ):
        return getattr(current_user, "role", None) == "admin"

    @app.post("/x/context-user-role")
    def context_user_role(ctx=Depends(get_current_context)):
        return ctx.user.is_superuser

    @app.post("/x/helper-role")
    def helper_role(
        ctx=Depends(get_current_context), current_user=Depends(get_current_active_user)
    ):
        return _synthetic_helper(current_user)

    @app.post("/x/role-of-someone-else")
    def role_of_someone_else(
        ctx=Depends(get_current_context), current_user=Depends(get_current_active_user)
    ):
        other = SimpleNamespace(role="user")
        return other.role, ctx.bypass.user_is_admin


def _synthetic_app():
    from fastapi import Depends
    from fastapi import FastAPI

    from app.api.deps_context import get_base_context
    from app.api.deps_context import get_current_context
    from app.api.deps_context import refuse_under_support_grant
    from app.api.deps_context import require_org_admin
    from app.api.endpoints.auth import get_current_active_user
    from app.api.endpoints.auth import get_current_admin_user

    app = FastAPI()

    @app.get("/x/content")
    def content(ctx=Depends(get_current_context)):
        return ctx.bypass.allows(
            org_id=1, owner_id=2, need="read", resource_type="t", resource_uuid="u"
        )

    @app.get("/x/talks-about-it")
    def talks_about_it(ctx=Depends(get_current_context)):
        """Runs now (bypasses the schedule); the word is only prose."""
        return {}

    @app.post("/x/inline-role")
    def inline_role(current_user=Depends(get_current_active_user)):
        if not current_user.is_admin:
            return {}
        return {}

    _add_role_routes(app)

    @app.get("/x/refused")
    def refused(ctx=Depends(refuse_under_support_grant)):
        return {}

    @app.get("/x/org")
    def org(ctx=Depends(require_org_admin)):
        return {}

    @app.get("/x/base")
    def base(ctx=Depends(get_base_context)):
        return {}

    @app.get("/x/self")
    def own(user=Depends(get_current_active_user)):
        return {}

    @app.get("/x/unknown-admin-route")
    def unknown(user=Depends(get_current_admin_user)):
        return {}

    @app.get("/api/admin/stats")
    def known(user=Depends(get_current_admin_user)):
        return {}

    return app


_SYNTHETIC_GUARDS = {"GET /x/content": "bypass", "GET /x/talks-about-it": "caller"}


def _synthetic_rows(content_guards=None, inline_routes=None):
    from fastapi.routing import APIRoute

    app = _synthetic_app()
    rows = {}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        names = matrix.dependency_names(route.dependant)
        source = matrix.handler_source(route.endpoint)
        helpers = {"_synthetic_helper": matrix.handler_source(_synthetic_helper)}
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            for row in matrix.classify_route(
                method, route.path, names, source, content_guards, inline_routes, helpers
            ):
                # a route can earn two rows; the inline one is filed under its own name
                inline = row.gate.startswith("platform-op:inline:")
                rows[f"{row.key} (inline)" if inline else row.key] = row
    return rows


def test_the_derivation_classifies_each_shape_and_ignores_self_scoped_routes():
    rows = _synthetic_rows(_SYNTHETIC_GUARDS)
    assert rows["GET /x/content"].gate == "content"
    assert rows["GET /x/content"].header == "honoured"
    assert "bypass" in rows["GET /x/content"].guard
    assert rows["GET /x/refused"].gate == "content-refused"
    assert rows["GET /x/org"].gate == "org-admin"
    assert rows["GET /x/base"].gate == "lifecycle"
    assert rows["GET /x/base"].header == "ignored"
    assert "GET /x/self" not in rows, "a self-scoped route is not a platform-access path"
    assert rows["GET /api/admin/stats"].gate == "platform-op:admin:platform-ops"


def test_a_content_route_is_unclassified_until_it_has_an_entry():
    rows = _synthetic_rows({})
    assert "UNCLASSIFIED" in rows["GET /x/content"].guard
    assert "UNCLASSIFIED" in rows["GET /x/talks-about-it"].guard


def test_a_docstring_or_comment_is_not_a_bypass():
    code = "def f(ctx):\n    return ctx.bypass.sees_all_in_scope\n"
    prose = 'def f(ctx):\n    """Runs now (bypasses the schedule)."""\n    return 1  # bypass\n'
    assert matrix.uses_bypass(code) is True
    assert matrix.uses_bypass(prose) is False


def test_a_route_that_tests_the_callers_role_inline_is_found_and_must_be_listed():
    unlisted = _synthetic_rows(_SYNTHETIC_GUARDS, {})["POST /x/inline-role (inline)"]
    assert unlisted.gate == "platform-op:inline:UNCLASSIFIED"
    listed = _synthetic_rows(_SYNTHETIC_GUARDS, {"POST /x/inline-role": ("state-repair", "why")})
    assert listed["POST /x/inline-role (inline)"].gate == "platform-op:inline:state-repair"
    assert "POST /x/inline-role" not in listed, "its only row is the inline one"
    assert "GET /x/self" not in listed, "a self-scoped route with no role test is not listed"


_INLINE_SHAPES = (
    "POST /x/context-then-role",
    "POST /x/renamed-user",
    "POST /x/getattr-role",
    "POST /x/context-user-role",
    "POST /x/helper-role",
)


def test_a_content_route_that_also_tests_the_role_inline_is_not_just_caller_scoped():
    """Taking the request context used to end the classification before the role scan ran."""
    rows = _synthetic_rows(_SYNTHETIC_GUARDS, {})
    for key in _INLINE_SHAPES:
        assert rows[key].gate == "content", key
        flagged = rows[f"{key} (inline)"]
        assert flagged.gate == "platform-op:inline:UNCLASSIFIED", key
    listed = _synthetic_rows(
        _SYNTHETIC_GUARDS, {key: ("display-hint", "why") for key in _INLINE_SHAPES}
    )
    assert all(
        listed[f"{key} (inline)"].gate == "platform-op:inline:display-hint"
        for key in _INLINE_SHAPES
    )


def test_the_role_scan_ignores_other_objects_and_the_bypass_flag():
    rows = _synthetic_rows(_SYNTHETIC_GUARDS, {})
    assert "POST /x/role-of-someone-else (inline)" not in rows
    assert "GET /x/content (inline)" not in rows
    assert "GET /x/talks-about-it (inline)" not in rows


def test_a_widener_outside_the_tables_is_unclassified():
    assert "UNCLASSIFIED" in matrix.site_row("scope-widener", "api/new.py::list_things").guard
    assert "UNCLASSIFIED" in matrix.site_row("reveal-widener", "api/new.py::show_things").guard
    known = matrix.site_row("scope-widener", "api/endpoints/speakers.py::list_speakers")
    assert "UNCLASSIFIED" not in known.guard


def test_a_widener_without_its_tenant_predicate_is_reported(tmp_path):
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "x.py").write_text(
        "def scoped(ctx, q):\n"
        "    if ctx.bypass.sees_all_in_scope:\n"
        "        return q.filter(org_scope_pred(1))\n"
        "def leaky(ctx, q):\n"
        "    if ctx.bypass.sees_all_in_scope:\n"
        "        return q\n"
    )
    markers = {
        "api/x.py::scoped": ("org_scope_pred",),
        "api/x.py::leaky": ("org_scope_pred",),
        "api/x.py::gone": ("org_scope_pred",),
    }
    assert matrix.missing_tenant_markers(tmp_path, markers) == [
        "api/x.py::gone: no such function",
        "api/x.py::leaky: no longer mentions org_scope_pred",
    ]


def test_an_unlisted_admin_route_is_reported_unclassified():
    rows = {r.key: r for r in matrix.route_rows(_synthetic_app())}
    assert rows["GET /x/unknown-admin-route"].gate == "platform-op:admin:UNCLASSIFIED"


def test_the_site_scan_finds_each_shape_and_ignores_unrelated_code():
    src = (
        "def a(ctx, row):\n"
        "    return ctx.bypass.allows(org_id=1, owner_id=2, need='read',\n"
        "                             resource_type='x', resource_uuid='u')\n"
        "def b(ctx):\n"
        "    return ctx.bypass.sees_all_in_scope\n"
        "def c(request, db, u):\n"
        "    return resolve_active_grant(db, request.headers[SUPPORT_ACCESS_GRANT_HEADER], u)\n"
        "def d(db, u):\n"
        "    return build_bypass(db, u, None)\n"
        "def e(items):\n"
        "    return [i for i in items if i.allowed]\n"
        "def f(x):\n"
        "    return x.sees_all\n"
    )
    found = matrix.scan_source(src, "api/x.py")
    assert found == {
        ("allows", "api/x.py::a"),
        ("scope-widener", "api/x.py::b"),
        ("grant-resolver", "api/x.py::c"),
        ("header-reader", "api/x.py::c"),
        ("bypass-builder", "api/x.py::d"),
    }


def test_a_new_allows_site_is_unclassified_until_someone_decides():
    row = matrix.site_row("allows", "api/endpoints/new_thing.py::get_new_thing")
    assert "UNCLASSIFIED" in row.guard
    known = matrix.site_row("allows", "utils/uuid_helpers.py::require_speaker_access")
    assert "UNCLASSIFIED" not in known.guard
