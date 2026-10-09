"""Every content route must resolve the request context (issue #1122).

``get_current_context`` is where the platform bypass lives (and, from the support-access
phase on, where an ``X-Support-Access-Grant`` header is read). A route that depends on
``get_current_active_user`` but never reaches a context dependency cannot see either: for a
CONTENT route that would mean the header is silently ignored, or an admin check re-grows
beside the tenant gate. So the set of such routes is closed, and every member says why it
is not a content route.

The walk is over the live dependency tree, like ``test_lifecycle_gate_coverage.py``, so a
route that reaches the context through a wrapper still counts as covered.

Entries are exact ``"METHOD path"`` keys or ``"path prefix"`` keys (ending ``/``), each with
a reason. A stale entry fails the run, so the list cannot rot into a blanket exemption.
"""

from __future__ import annotations

from typing import Any

#: Route-level reasons, keyed by exact "METHOD path".
EXACT: dict[str, str] = {
    "POST /api/files/complete": (
        "upload completion: creates the caller's own file in their own scope; refused "
        "outright under a support grant"
    ),
    "POST /api/files/multipart/parts": "upload signing for the caller's own pending upload",
    "GET /api/files/bulk-export-stream": (
        "authorized by an HMAC-signed job id minted at export prepare, not by the header; "
        "exports are refused under a grant"
    ),
    "POST /api/files/waveforms/generate": "admin-gated bulk waveform backfill (plan 3.3, class P)",
    "GET /api/files/waveforms/status": "admin-gated instance-wide waveform coverage counts",
    "GET /api/files/youtube/quota": "the caller's own ingestion quota; no tenant content",
    "POST /api/files/management/cleanup-orphaned": (
        "admin-gated bulk stuck-file recovery (plan 3.3, class P); no per-file content returned"
    ),
    "GET /api/files/retroactive-auto-label/status": "the caller's own job status; no content",
    "POST /api/speakers/cleanup-orphaned-embeddings": (
        "self-scoped by user_id: repairs the caller's own search-index rows"
    ),
    "GET /api/speakers/debug/cross-media-data": (
        "self-scoped to current_user.id even for an admin; reads only the caller's own speakers"
    ),
    "POST /api/speaker-clusters/recluster": "re-clusters the caller's own speaker embeddings",
    "GET /api/custom-vocabulary/domains": "static vocabulary domain list, no tenant content",
}

#: Prefix-level reasons, keyed by a path prefix.
PREFIXES: dict[str, str] = {
    "/api/admin/": (
        "platform operations gated to admins (plan 3.3, class P); admins hold them without a "
        "grant, so a grant is never needed and the header is ignored by design"
    ),
    "/api/auth/": "session lifecycle: never resolves the grant header (plan 4 rule 10)",
    "/api/system/": "system status and capabilities; never resolves the grant header (rule 10)",
    "/api/users": "user directory and the caller's own profile; not tenant content",
    "/api/user-settings/": "the caller's own preferences; no tenant content",
    "/api/asr-settings": "deployment/user ASR provider configuration; no tenant content",
    "/api/llm-settings": "LLM provider configuration; no tenant content",
    "/api/llm/": "LLM provider status/test; no tenant content",
    "/api/prompts": "summary prompt templates; no media content",
    "/api/embeddings/": "admin embedding-model migration control plane",
    "/api/speaker-attributes/": "admin speaker-attribute migration control plane",
    "/api/speakers/combined-migration/": "admin speaker-embedding migration control plane",
    "/api/search/models": "admin neural-search model lifecycle; no tenant content",
    "/api/search/reindex": "admin reindex control plane; no tenant content returned",
    "/api/search/repair-indices": "admin index repair; no tenant content returned",
    "/api/search/degraded-embeddings": "admin embedding-health listing; ids and counts only",
    "/api/search/reembed-degraded": "admin re-embed dispatch; no tenant content returned",
    "/api/tasks/system/": "admin recovery jobs (plan 3.3, class P)",
    "/api/tasks/recover-stuck-tasks": "admin recovery job (plan 3.3, class P)",
    "/api/tasks/fix-inconsistent-files": "admin recovery job (plan 3.3, class P)",
    "/api/watch-sources/capabilities": "deployment watch-source capabilities",
    "/api/watch-sources/browse": "server-side watch-folder browser; not tenant content",
    "/api/watch-sources/settings": "deployment watch-source settings",
    "/api/watch-sources/email-configs": "deployment inbound-email configuration",
    "/api/watch-sources/test-multipart-regex": "stateless regex tester",
}


def _dependency_callables(dependant: Any, seen: set[int] | None = None) -> set[str]:
    if seen is None:
        seen = set()
    if id(dependant) in seen:
        return set()
    seen.add(id(dependant))
    names: set[str] = set()
    call = getattr(dependant, "call", None)
    if call is not None:
        names.add(getattr(call, "__name__", ""))
    for sub in getattr(dependant, "dependencies", []) or []:
        names |= _dependency_callables(sub, seen)
    return names


def _routes_without_context(app: Any = None) -> tuple[list[tuple[str, str]], int]:
    """(``(label, path)`` of each route on the lifecycle gate with no context, route count)."""
    if app is None:
        from app.main import app

    out: list[tuple[str, str]] = []
    walked = 0
    for route in app.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        names = _dependency_callables(dependant)
        if "get_current_active_user" not in names:
            continue
        walked += 1
        if {"get_current_context", "get_base_context"} & names:
            continue
        for method in sorted(getattr(route, "methods", None) or {"GET"}):
            if method in {"HEAD", "OPTIONS"}:
                continue
            out.append((f"{method} {route.path}", route.path))
    return out, walked


def _covered(label: str, path: str) -> bool:
    return label in EXACT or any(path.startswith(prefix) for prefix in PREFIXES)


def test_the_walk_flags_a_route_with_no_context_and_clears_one_with_it():
    """Must-fire and must-stay-clean: the walk itself, on a two-route synthetic app."""
    from fastapi import Depends
    from fastapi import FastAPI

    from app.api.deps_context import get_current_context
    from app.api.endpoints.auth import get_current_active_user

    app = FastAPI()

    @app.get("/content/bare")
    def bare(user=Depends(get_current_active_user)):
        return {}

    @app.get("/content/with-context")
    def with_context(ctx=Depends(get_current_context)):
        return {}

    routes, walked = _routes_without_context(app)
    assert walked == 2
    assert routes == [("GET /content/bare", "/content/bare")]


def test_the_walk_sees_the_app():
    """A walk that finds nothing would make every assertion below vacuous."""
    routes, walked = _routes_without_context()
    assert walked > 200
    assert len(routes) > 100


def test_every_route_without_a_context_is_explained():
    unexplained = sorted(
        label for label, path in _routes_without_context()[0] if not _covered(label, path)
    )
    assert not unexplained, (
        "These routes sit behind the lifecycle gate but never resolve a request context, so "
        "neither the platform bypass nor a support-access grant can reach them. Add "
        "ctx: RequestContext = Depends(get_current_context) if they touch tenant content, or "
        "list them in EXACT/PREFIXES with a reason:\n  " + "\n  ".join(unexplained)
    )


def test_no_entry_is_stale():
    routes = _routes_without_context()[0]
    labels = {label for label, _ in routes}
    paths = [path for _, path in routes]
    stale = [key for key in EXACT if key not in labels]
    stale += [prefix for prefix in PREFIXES if not any(p.startswith(prefix) for p in paths)]
    assert not stale, "Delete these entries; no such route remains:\n  " + "\n  ".join(stale)


def test_every_entry_carries_a_reason():
    thin = [k for k, v in {**EXACT, **PREFIXES}.items() if len(v) < 20]
    assert not thin, f"Write a real reason for: {thin}"
