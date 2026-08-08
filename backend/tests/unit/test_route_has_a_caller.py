"""Every mounted backend route must have a caller.

The dead-surface shape this closes: five endpoints and a service layer, shipped
green, with no UI ever calling them — group mappings, before its admin panel
existed. A route with storage, validation, and a passing API test can still be
completely unreachable by any human using the product.

A route counts as having a caller when at least one of these holds:

1. **A frontend call site.** ``axiosInstance.<method>(...)`` (relative to the
   ``/api`` baseURL) or ``fetch``/``EventSource``/``WebSocket`` with the full path,
   anywhere under ``frontend/src``.
2. **A documented external-integration contract.** SCIM (RFC 7644, consumed by an
   IdP's provisioning connector), OIDC/SAML/PKI/proxy authentication flows
   (consumed by an identity provider or an authenticating reverse proxy, never by
   this app's own frontend), and infrastructure probes (health/metrics/docs).
3. **An explicit allow-list entry with a reason** — ``NO_FRONTEND_CALLER``,
   mirroring ``KNOWN_PUBLIC`` in ``test_route_privilege_tiers.py``.

Why AST/regex and not "the endpoint has a test"
------------------------------------------------
A passing API test proves the *backend* behaves correctly when called — it says
nothing about whether the *product* ever calls it. That is precisely the gap that
let group mappings ship reachable only from a Python test file.

Path matching
-------------
Backend paths use FastAPI's ``{param}`` placeholders; the frontend either embeds
``${expr}`` template interpolation or builds the path with a helper. Both sides are
normalized to a wildcard token and compared by segment count, so ``/files/{uuid}``
matches a frontend call to `` `/files/${fileId}` `` without needing to know the
frontend's variable name.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT = _BACKEND_ROOT.parent
_FRONTEND_SRC = _REPO_ROOT / "frontend" / "src"

#: A single token standing in for any dynamic path segment, on either side.
_WILDCARD = "*"

#: Route path prefixes that are documented external-integration contracts, not
#: reachable from (or meant to be reached by) this app's own frontend. Each is a
#: real caller — an IdP, a provisioning connector, an authenticating proxy, or an
#: infrastructure probe — just not one this scanner can see in `frontend/src`.
_EXTERNAL_INTEGRATION_PREFIXES = (
    "/scim/v2",  # RFC 7644 — an IdP's SCIM connector
    "/api/auth/oidc",  # an OIDC identity provider's redirect/callback
    "/api/auth/saml",  # a SAML identity provider's POST/redirect targets
    "/api/auth/pki",  # mTLS-terminating proxy or the browser's client-cert prompt
    "/api/auth/proxy",  # an authenticating reverse proxy asserting a header
    "/health",
    "/metrics",
    "/api/docs",
    "/api/redoc",
    "/api/openapi",
)

#: Routes with no frontend call site, each with a reviewed reason. Mirrors
#: ``KNOWN_PUBLIC`` in ``test_route_privilege_tiers.py`` — an entry here is a
#: decision, not an omission, and must name who the real caller is.
NO_FRONTEND_CALLER: dict[str, str] = {
    # The WebSocket handshake path itself — matched via `new WebSocket(...)`,
    # whose URL this scanner does not parse the same way as an HTTP call.
    "/api/ws": "Real caller: frontend/src/stores/websocket.ts (WebSocket, not axios/fetch)",
    # /token is FastAPI's OAuth2PasswordBearer-conventional path (what Swagger's
    # "Authorize" button and any OAuth2-tooling client posts to); /login is the
    # same handler registered a second time "for frontend compatibility"
    # (backend/app/api/endpoints/auth/login.py, the two @router.post decorators
    # stacked on login_for_access_token) and is what the SPA actually calls.
    "/api/auth/token": "Real caller: OAuth2PasswordBearer/Swagger tooling, not the SPA — /login is the frontend alias of the same handler",
    # nginx's own `auth_request /api/auth/flower-authz;` subrequest (see the
    # module docstring in app/api/endpoints/auth/flower.py) — never fetched by
    # the SPA, which never talks to Flower at all.
    "/api/auth/flower-authz": "Real caller: nginx auth_request subrequest gating /flower/ (app/api/endpoints/auth/flower.py)",
}

#: Routes this scanner found no frontend call site for, after the axios/fetch/
#: EventSource scan AND a manual full-route-suffix grep across frontend/src.
#: Tracked as **strict** xfails, not silently allow-listed — mirrors
#: `_NOT_YET_BRIDGED` in `test_auth_config_has_readers.py`: the moment a real
#: caller is wired (or found), this test starts passing, which XPASSes the
#: strict xfail and fails the suite, forcing the entry to be deleted rather
#: than rot. Grouped by what the manual investigation actually turned up —
#: several apparent near-matches during triage were false positives (see
#: reasons below), which is itself evidence these are unreached rather than
#: reached through a pattern the scanner can't parse.
_MAINTENANCE_OPS_REASON = (
    "No frontend call site found (axios/fetch/EventSource scan + manual grep). "
    "Named/shaped like a one-time admin maintenance or migration trigger "
    "(repair/recompute/cleanup/migration/debug) — plausibly curl/API-doc invoked "
    "by an operator, not the SPA. Needs product review: wire an admin-UI panel, "
    "or confirm intentionally API-only and move to NO_FRONTEND_CALLER with that reason."
)
_NO_ADMIN_PANEL_REASON = (
    "No frontend call site found. Matches this codebase's documented "
    "no-admin-UI-yet precedent (see backend/app/auth/CLAUDE.md: IdP group "
    "mappings, directory sync) — a real capability with no SPA panel over it, "
    "not confirmed dead. Needs product review to decide UI vs formal "
    "NO_FRONTEND_CALLER disposition."
)
_SUPERSEDED_OR_DUPLICATE_REASON = (
    "No frontend call site found; investigation surfaced a distinct route the "
    "SPA calls instead for what looks like the same feature (e.g. "
    "`/my-files/{id}/retry` vs this `/files/{file_uuid}/retry`, or "
    "`/user-settings/media-sources` vs this `/admin/settings/media-sources`, or "
    "`/files/{id}/stream-url?media_type=thumbnail` vs this `/files/{file_uuid}/"
    "thumbnail`). Candidate for deletion as genuinely dead — needs product "
    "review, not an agent decision, since removing an API route is user-visible "
    "for any non-SPA client."
)
_UNVERIFIED_REASON = (
    "No frontend call site found (axios/fetch/EventSource scan + manual grep "
    "for the route's distinguishing path segments — a few plausible-looking "
    "text matches were checked and turned out to be unrelated UI strings, e.g. "
    "a CSS class or an unrelated word, not a call to this route). Needs product "
    "review."
)

_NOT_YET_VERIFIED: dict[str, str] = {
    # --- admin maintenance / migration / repair triggers ---
    "/api/admin/data-integrity/counts": _MAINTENANCE_OPS_REASON,
    "/api/admin/engine-settings/metrics": _MAINTENANCE_OPS_REASON,
    "/api/admin/gpu-profiles": _MAINTENANCE_OPS_REASON,
    "/api/admin/imohash-recompute/start": _MAINTENANCE_OPS_REASON,
    "/api/admin/imohash-recompute/status": _MAINTENANCE_OPS_REASON,
    "/api/admin/profile-embeddings/repair": _MAINTENANCE_OPS_REASON,
    "/api/admin/timing": _MAINTENANCE_OPS_REASON,
    "/api/admin/timing/{task_id}": _MAINTENANCE_OPS_REASON,
    "/api/admin/timing-summary/recent": _MAINTENANCE_OPS_REASON,
    "/api/embeddings/migration/mode": _MAINTENANCE_OPS_REASON,
    "/api/speaker-attributes/migration/progress": _MAINTENANCE_OPS_REASON,
    "/api/speakers/combined-migration/start": _MAINTENANCE_OPS_REASON,
    "/api/speakers/combined-migration/progress": _MAINTENANCE_OPS_REASON,
    "/api/speakers/combined-migration/status": _MAINTENANCE_OPS_REASON,
    "/api/speakers/combined-migration/stop": _MAINTENANCE_OPS_REASON,
    "/api/speakers/cleanup-orphaned-embeddings": _MAINTENANCE_OPS_REASON,
    "/api/speakers/debug/cross-media-data": _MAINTENANCE_OPS_REASON,
    "/api/speakers/debug/cross-media-by-name": _MAINTENANCE_OPS_REASON,
    "/api/search/repair-indices": _MAINTENANCE_OPS_REASON,
    "/api/search/models/neural/active": _MAINTENANCE_OPS_REASON,
    "/api/search/models/neural/status": _MAINTENANCE_OPS_REASON,
    "/api/search/models/neural/{model_name}/deploy": _MAINTENANCE_OPS_REASON,
    "/api/search/models/neural/{model_name}/undeploy": _MAINTENANCE_OPS_REASON,
    "/api/search/models/neural/{model_name}/register": _MAINTENANCE_OPS_REASON,
    "/api/files/management/stuck": _MAINTENANCE_OPS_REASON,
    "/api/files/management/cleanup-orphaned": _MAINTENANCE_OPS_REASON,
    "/api/tags/cleanup": _MAINTENANCE_OPS_REASON,
    "/api/tags/unused": _MAINTENANCE_OPS_REASON,
    "/api/tasks/system/fix-file/{file_uuid}": _MAINTENANCE_OPS_REASON,
    # --- exists, no admin UI panel over it yet (documented pattern) ---
    "/api/admin/scim-tokens": _NO_ADMIN_PANEL_REASON,
    "/api/admin/scim-tokens/{token_uuid}": _NO_ADMIN_PANEL_REASON,
    "/api/admin/auth-config/status": _NO_ADMIN_PANEL_REASON,
    "/api/admin/gdpr/erase-user/{user_uuid}": _NO_ADMIN_PANEL_REASON,
    "/api/org-admin/gdpr/erase-organization": _NO_ADMIN_PANEL_REASON,
    "/api/org-admin/gdpr/erase-user/{user_uuid}": _NO_ADMIN_PANEL_REASON,
    "/api/org-admin/audit-logs": _NO_ADMIN_PANEL_REASON,
    "/api/admin/files/{file_uuid}/quarantine": _NO_ADMIN_PANEL_REASON,
    "/api/admin/files/{file_uuid}/release": _NO_ADMIN_PANEL_REASON,
    "/api/admin/files/quarantined": _NO_ADMIN_PANEL_REASON,
    # --- investigation found the SPA calling a different route for this feature ---
    "/api/admin/settings/media-sources": _SUPERSEDED_OR_DUPLICATE_REASON,
    "/api/admin/settings/media-sources/{source_id}": _SUPERSEDED_OR_DUPLICATE_REASON,
    "/api/files/{file_uuid}/retry": _SUPERSEDED_OR_DUPLICATE_REASON,
    "/api/files/{file_uuid}/thumbnail": _SUPERSEDED_OR_DUPLICATE_REASON,
    # --- unverified: no call site found, no specific alternate explanation ---
    "/api/admin/stats": _UNVERIFIED_REASON,
    "/api/tasks/{task_id}": _UNVERIFIED_REASON,
    "/api/admin/users/{user_uuid}": _UNVERIFIED_REASON,
    "/api/asr-settings/local-models": _UNVERIFIED_REASON,
    "/api/custom-vocabulary/all": _UNVERIFIED_REASON,
    "/api/custom-vocabulary/export": _UNVERIFIED_REASON,
    "/api/files/batch-extract": _UNVERIFIED_REASON,
    "/api/files/search": _UNVERIFIED_REASON,
    "/api/files/supported-formats": _UNVERIFIED_REASON,
    "/api/files/youtube/quota": _UNVERIFIED_REASON,
    "/api/files/waveforms/generate": _UNVERIFIED_REASON,
    "/api/files/waveforms/status": _UNVERIFIED_REASON,
    "/api/files/{file_uuid}/analytics/refresh": _UNVERIFIED_REASON,
    "/api/files/{file_uuid}/apply": _UNVERIFIED_REASON,
    "/api/files/{file_uuid}/auto-label": _UNVERIFIED_REASON,
    "/api/files/{file_uuid}/cancel": _UNVERIFIED_REASON,
    "/api/files/{file_uuid}/force": _UNVERIFIED_REASON,
    "/api/files/{file_uuid}/identify-speakers": _UNVERIFIED_REASON,
    "/api/files/{file_uuid}/info": _UNVERIFIED_REASON,
    "/api/files/{file_uuid}/recover": _UNVERIFIED_REASON,
    "/api/files/{file_uuid}/status-detail": _UNVERIFIED_REASON,
    "/api/files/{file_uuid}/subtitles": _UNVERIFIED_REASON,
    "/api/files/{file_uuid}/subtitles/validate": _UNVERIFIED_REASON,
    "/api/files/{file_uuid}/waveform/generate": _UNVERIFIED_REASON,
    "/api/files/{file_uuid}/waveform/peaks": _UNVERIFIED_REASON,
    "/api/prompts/by-content-type/{content_type}": _UNVERIFIED_REASON,
    "/api/search/filters": _UNVERIFIED_REASON,
    "/api/speaker-clusters/stats": _UNVERIFIED_REASON,
    "/api/speaker-profiles/collections": _UNVERIFIED_REASON,
    "/api/speaker-profiles/profiles/{profile_uuid}/occurrences": _UNVERIFIED_REASON,
    "/api/speaker-profiles/speakers/{speaker_uuid}/assign-profile": _UNVERIFIED_REASON,
    "/api/speaker-profiles/speakers/{speaker_uuid}/suggestions": _UNVERIFIED_REASON,
    "/api/speakers/{speaker_uuid}/verify": _UNVERIFIED_REASON,
    "/api/user-settings/ai-summary": _UNVERIFIED_REASON,
    "/api/users": _UNVERIFIED_REASON,
}


def _normalize_backend_path(path: str) -> tuple[str, ...]:
    """FastAPI `{param}` segments -> wildcard; split into segments."""
    segments = [seg for seg in path.split("/") if seg]
    return tuple(
        _WILDCARD if seg.startswith("{") and seg.endswith("}") else seg for seg in segments
    )


#: `axiosInstance.get('/path', ...)` / `.post(\`/path/${x}\`, ...)` — the path
#: argument is the first positional arg, single- or double-quoted or a backtick
#: template literal, OR a bare identifier (`const endpoint = \`/x/${id}\`; ...
#: axiosInstance.get(endpoint)`) — group 2 catches that case, resolved the same
#: way as a `${NAME}` slot. `<Type>` is an optional TypeScript generic argument
#: between the method name and the call's opening paren
#: (`axiosInstance.get<Resp>('/x')`) — matched non-greedily against *any*
#: character (`[\s\S]*?`, not `[^>(]*`) so a generic that itself nests angle
#: brackets (`axiosInstance.get<Array<{ ... }>>(...)`) or spans multiple lines
#: still resolves: `[^>(]*` stopped at the first inner `>`, which made every
#: multi-line/nested-generic call invisible to the scanner.
_AXIOS_CALL_RE = re.compile(
    r"axiosInstance\.(?:get|post|put|patch|delete)(?:<[\s\S]*?>)?\("
    r"\s*(?:[`'\"]([^`'\"]*)[`'\"]|(\w+))"
)

#: `fetch('/api/path')` / `new EventSource(\`/api/path/${x}\`)` — full path, already
#: `/api`-prefixed, unlike the axios form.
_ABSOLUTE_CALL_RE = re.compile(r"(?:\bfetch|new EventSource)\(\s*[`'\"](/api/[^`'\"]*)[`'\"]")

#: `${expr}` template interpolation, or a `+ variable +` string-concat segment —
#: either becomes a wildcard segment, same as a FastAPI `{param}`.
_TEMPLATE_EXPR_RE = re.compile(r"\$\{([^}]*)\}")

#: `const BASE = '/watch-sources';` or a TS class field
#: (`private static readonly BASE_PATH = '/asr-settings';`, with an optional type
#: annotation) — a path-prefix constant. Several API modules build every call as
#: `` `${BASE}/suffix` `` or `` `${this.BASE_PATH}/suffix` ``, so resolving these
#: is what tells that apart from a per-request dynamic segment (a uuid, an id) —
#: the latter has no such binding and stays a wildcard.
_LOCAL_CONST_RE = re.compile(
    r"^\s*(?:private\s+|public\s+|protected\s+|static\s+|readonly\s+|const\s+)*"
    r"(\w+)\s*(?::\s*[\w<>\[\], ]+\s*)?=\s*[`'\"]([^`'\"]*)[`'\"]",
    re.MULTILINE,
)


def _nearest_preceding(name: str, before: int, consts: list[tuple[int, str, str]]) -> str | None:
    """The value of the last ``name = 'literal'`` declared before position *before*.

    A file commonly declares more than one class, each with its own same-named
    ``BASE_PATH`` field — a flat "last declaration in the file wins" dict would
    silently resolve one class's calls using another class's prefix. Nearest
    preceding is right for the common shape (field declared near the top of the
    class, used by methods below it, next class repeats the pattern) without
    needing a real parser to track class boundaries.
    """
    candidates = [(pos, val) for pos, cname, val in consts if cname == name and pos < before]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _resolve_template_literal(raw: str, const_at: int, consts: list[tuple[int, str, str]]) -> str:
    """Substitute a known local string constant into a single `${NAME}` slot.

    Only ever substitutes the *whole* expression inside `${...}` against a plain
    identifier — `${BASE}` resolves, `${BASE}/${uuid}`'s two slots are handled
    independently, and anything more complex (`${a ? b : c}`) is left alone and
    falls through to the wildcard case, which is the safe default.
    """

    def _sub(match: re.Match) -> str:
        expr = match.group(1).strip()
        # `this.BASE_PATH` -> `BASE_PATH`: the class-field case.
        if expr.startswith("this."):
            expr = expr[len("this.") :]
        return _nearest_preceding(expr, const_at, consts) or _WILDCARD

    return _TEMPLATE_EXPR_RE.sub(_sub, raw)


def _resolve_axios_call_arg(
    literal: str | None, identifier: str | None, call_at: int, consts: list[tuple[int, str, str]]
) -> str | None:
    """Resolve an axios call's first argument to a path string, or ``None``.

    Handles both shapes: the path inline as a string/template literal, or built
    into a local variable first (``const endpoint = \\`/x/${id}\\`; ...
    axiosInstance.get(endpoint)``) — the latter is resolved one level (the
    variable's own declared value), then run through the same `${...}`
    resolution as an inline literal.
    """
    if literal is not None:
        return _resolve_template_literal(literal, call_at, consts)
    if identifier is not None:
        declared_value = _nearest_preceding(identifier, call_at, consts)
        if declared_value is None:
            return None
        return _resolve_template_literal(declared_value, call_at, consts)
    return None  # pragma: no cover - regex always yields one group or the other


def _normalize_frontend_path(raw: str) -> tuple[str, ...]:
    # Query strings are call parameters, not path segments — `/tasks?${x}` must
    # match the same route as a plain `/tasks` call.
    raw = raw.split("?", 1)[0]
    segments = [seg for seg in raw.split("/") if seg]
    return tuple(_WILDCARD if _WILDCARD in seg else seg for seg in segments)


def _scan_frontend_paths() -> set[tuple[str, ...]]:
    """Every path (as a normalized segment tuple) called from the frontend."""
    if not _FRONTEND_SRC.is_dir():
        pytest.fail(f"frontend source tree not found at {_FRONTEND_SRC}")

    used: set[tuple[str, ...]] = set()
    for path in _FRONTEND_SRC.rglob("*"):
        if path.suffix not in (".ts", ".svelte"):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        consts = [(m.start(), m.group(1), m.group(2)) for m in _LOCAL_CONST_RE.finditer(text)]

        for match in _AXIOS_CALL_RE.finditer(text):
            resolved = _resolve_axios_call_arg(
                match.group(1), match.group(2), match.start(), consts
            )
            if not resolved or not resolved.startswith("/"):
                continue
            used.add(_normalize_frontend_path("/api" + resolved))
        for match in _ABSOLUTE_CALL_RE.finditer(text):
            resolved = _resolve_template_literal(match.group(1), match.start(), consts)
            used.add(_normalize_frontend_path(resolved))

    return used


def _mounted_routes() -> list:
    """Every HTTP route — has both a ``path`` and a ``methods`` set.

    Excludes ``APIWebSocketRoute`` (``/api/ws``), which has no ``methods``: a
    WebSocket handshake has no per-route "has a caller" question to ask the way
    an HTTP verb does — it is instead just an entry in ``NO_FRONTEND_CALLER``,
    checked against ``_all_route_paths()`` (which does include it) for staleness.
    """
    from app.main import app

    return [r for r in app.routes if hasattr(r, "path") and hasattr(r, "methods")]


def _all_route_paths() -> set[str]:
    """Every mounted route's path, HTTP or WebSocket — for allow-list staleness checks."""
    from app.main import app

    return {r.path for r in app.routes if hasattr(r, "path")}


_FRONTEND_PATHS = _scan_frontend_paths()


def _has_a_caller(route_path: str) -> bool:
    if route_path.startswith(_EXTERNAL_INTEGRATION_PREFIXES):
        return True
    if route_path in NO_FRONTEND_CALLER:
        return True
    return _normalize_backend_path(route_path) in _FRONTEND_PATHS


def _api_route_paths() -> list[str]:
    return sorted({r.path for r in _mounted_routes() if r.path.startswith("/api/")})


def _case(route_path: str) -> Any:
    """Build the parametrize entry for *route_path*, xfailing the known-unverified ones."""
    reason = _NOT_YET_VERIFIED.get(route_path)
    if reason is None:
        return route_path
    return pytest.param(route_path, marks=pytest.mark.xfail(strict=True, reason=reason))


@pytest.mark.parametrize("route_path", [_case(p) for p in _api_route_paths()])
def test_route_has_a_caller(route_path: str) -> None:
    assert _has_a_caller(route_path), (
        f"{route_path} has no frontend call site, is not a documented external-"
        "integration contract, and is not in NO_FRONTEND_CALLER. Wire a frontend "
        "call site, or add an allow-list entry with a reason naming the real caller."
    )


def test_no_frontend_caller_entries_are_real_routes() -> None:
    """A reviewed exception for a route that does not exist is a stale entry."""
    unknown = set(NO_FRONTEND_CALLER) - _all_route_paths()
    assert not unknown, f"NO_FRONTEND_CALLER names routes that no longer exist: {unknown}"


def test_not_yet_verified_entries_are_real_routes() -> None:
    """A tracked xfail for a route that no longer exists is a stale entry."""
    unknown = set(_NOT_YET_VERIFIED) - _all_route_paths()
    assert not unknown, f"_NOT_YET_VERIFIED names routes that no longer exist: {unknown}"


def test_scanner_finds_a_known_frontend_call() -> None:
    """Guard the guard: a scanner that silently matches nothing passes everything."""
    assert _FRONTEND_PATHS, "no frontend API call sites parsed — scan is blind"
    assert ("api", "admin", "group-mappings") in _FRONTEND_PATHS, (
        "AST scan found no frontend call to a route known to have a real UI "
        f"(groupMappings.ts). Parsed paths sample: {sorted(_FRONTEND_PATHS)[:10]}"
    )


def test_scanner_finds_a_known_backend_route() -> None:
    """Guard the guard, other direction: route enumeration must not be empty."""
    paths = _api_route_paths()
    assert len(paths) > 50, f"suspiciously few /api routes found: {len(paths)}"
    assert "/api/admin/group-mappings" in paths
