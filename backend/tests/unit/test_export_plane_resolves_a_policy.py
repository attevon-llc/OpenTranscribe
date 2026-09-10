"""Every route that hands a user a downloadable artifact must resolve a redaction policy.

Issue #863's real lesson is not that one endpoint was missed — it is that it was missed
*twice*. #673 converted the transcript export formats, #85 converted the two worker export
paths, and both sweeps enumerated routes by hand; the chat export was outside whichever list
each of them was reading, so it kept emitting `citations[].snippet` verbatim for another two
releases. A hand-maintained list cannot catch the route that is added after it is written.

So this walks the **live route table** instead. Any route whose path names an export, a
subtitle or a download must either reach ``resolve_effective_config`` from its own handler,
or carry an entry in :data:`EXEMPT` with a written reason. A new export route that does
neither fails here, in the fast unit suite, before it can ship.

⚠️ **What this does NOT prove.** Reaching the resolver is a structural fact, not a masking
one: it says the handler asked the policy, not that it applied the answer to every field.
That second half is what the per-surface suites are for
(``api/test_chat_export_redaction.py``, ``api/test_transcript_export_redaction_gate.py``,
``redaction/test_export_redaction_paths.py``). This test exists to make the *omission* of a
policy impossible, which is the failure mode that actually recurred.
"""

from __future__ import annotations

import ast
import inspect
import sys
from functools import cache

import pytest

pytestmark = pytest.mark.unit

#: A path naming any of these is in the plane. Deliberately broad — a false positive costs
#: one EXEMPT line with a reason, a false negative costs another issue #863.
_PLANE_KEYWORDS = ("export", "subtitles", "download")

#: The call that resolves a user's effective redaction policy, admin floor folded in.
_RESOLVER = "resolve_effective_config"

#: Routes in the plane that legitimately resolve nothing. ``"<METHOD> <path>": "<reason>"``.
#: A reason is mandatory: every one of these is a claim that the route emits no
#: transcript-derived content, or that its content is masked somewhere this scan cannot see.
EXEMPT: dict[str, str] = {
    "GET /api/files/{file_uuid}/subtitles/validate": (
        "Emits a timing report — 'Segment 4: Overlaps with previous segment' — built by "
        "SubtitleService.validate_subtitle_timing from indices and float offsets. No "
        "transcript text reaches the response."
    ),
    "POST /api/files/bulk-export/prepare": (
        "Dispatches the ZIP build; the artifact is produced in a Celery task which resolves "
        "the policy at RUN time from a user_id, never from a config serialized into the "
        "signature (redaction/export_policy.py's module docstring is the argument). Covered "
        "by tests/redaction/test_export_redaction_paths.py."
    ),
    "GET /api/files/bulk-export-stream": (
        "SSE progress frames for the above — counts, status and a presigned URL. The bytes "
        "the user keeps are produced by the worker, not here."
    ),
    "GET /api/admin/audit-logs/export": (
        "Audit records: actor, event type, outcome, ids and timestamps. Chat audit is "
        "metadata-only by construction (services/chat/CLAUDE.md: 'Never message content'), "
        "and no transcript text is stored in an audit row."
    ),
    "GET /api/custom-vocabulary/export": (
        "The caller's own vocabulary terms, which they typed into the vocabulary editor. "
        "Not recording-derived content, so there is no owner policy to resolve against."
    ),
    "GET /api/user-settings/download": "Download PREFERENCES. Matched on the group's name.",
    "PUT /api/user-settings/download": "Download PREFERENCES. Matched on the group's name.",
    "DELETE /api/user-settings/download": "Download PREFERENCES. Matched on the group's name.",
    "GET /api/user-settings/download/system-defaults": (
        "Download PREFERENCES (the deployment defaults). Matched on the group's name."
    ),
}


@cache
def _module_functions(module_name: str) -> dict[str, str]:
    """Every module-level ``def``/``async def`` in ``module_name``, name -> source."""
    module = sys.modules[module_name]
    try:
        tree = ast.parse(inspect.getsource(module))
    except (OSError, TypeError, SyntaxError):  # pragma: no cover - a C or generated module
        return {}
    lines = inspect.getsource(module).splitlines()
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            out[node.name] = "\n".join(lines[node.lineno - 1 : node.end_lineno])
    return out


def _reaches_resolver(endpoint) -> bool:
    """Whether ``endpoint`` reaches :data:`_RESOLVER`, following module-level helpers.

    The handlers do not call it directly — ``transcript_export`` goes through
    ``_resolve_export_redaction``, ``chat/export`` through ``resolve_export_policy`` — so a
    body-only scan would report every one of them as unresolved. Following the names the
    handler mentions gives per-ROUTE granularity, which a module-wide grep does not: a new
    unmasked route added beside a masked one in the same file would otherwise inherit its
    sibling's answer.
    """
    module_name = getattr(endpoint, "__module__", None)
    if module_name not in sys.modules:
        return False
    functions = _module_functions(module_name)
    imported_names = _imported_resolver_aliases(module_name)

    try:
        pending = [inspect.getsource(endpoint)]
    except (OSError, TypeError):  # pragma: no cover
        return False
    seen: set[str] = set()
    while pending:
        source = pending.pop()
        if _RESOLVER in source:
            return True
        for name, helper_source in functions.items():
            if name in seen or name not in source:
                continue
            seen.add(name)
            pending.append(helper_source)
        # A helper re-exported from another module (chat/export -> export_redaction).
        for alias, target_module in imported_names.items():
            if alias in seen or alias not in source:
                continue
            seen.add(alias)
            target = _module_functions(target_module).get(alias)
            if target:
                pending.append(target)
    return False


@cache
def _imported_resolver_aliases(module_name: str) -> dict[str, str]:
    """``from app.x.y import z`` in ``module_name`` -> ``{"z": "app.x.y"}`` for app modules."""
    module = sys.modules[module_name]
    try:
        tree = ast.parse(inspect.getsource(module))
    except (OSError, TypeError, SyntaxError):  # pragma: no cover
        return {}
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app."):
            for alias in node.names:
                if node.module in sys.modules:
                    out[alias.asname or alias.name] = node.module
    return out


def _plane() -> list[tuple[str, str, object]]:
    """``(key, path, endpoint)`` for every route whose path names the export plane."""
    from app.main import app

    found = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if not any(keyword in path for keyword in _PLANE_KEYWORDS):
            continue
        for method in sorted(getattr(route, "methods", None) or []):
            if method in ("HEAD", "OPTIONS"):
                continue
            found.append((f"{method} {path}", path, route.endpoint))
    return found


def test_the_export_plane_is_not_empty():
    """Guard the guard: a scan that matches nothing reports a clean plane."""
    plane = _plane()
    assert len(plane) >= 10, f"the route scan found only {len(plane)} export-plane routes"
    keys = {key for key, _, _ in plane}
    # The three surfaces #673 / #85 / #863 each converted must be in the scan's sights.
    assert "GET /api/files/{file_uuid}/export" in keys
    assert "GET /api/files/{file_uuid}/subtitles" in keys
    assert "GET /api/chat/conversations/{conversation_uuid}/export" in keys


def test_every_export_route_resolves_a_redaction_policy():
    unresolved = [
        key
        for key, _path, endpoint in _plane()
        if not _reaches_resolver(endpoint) and key not in EXEMPT
    ]
    assert not unresolved, (
        "these export-plane routes resolve no redaction policy — the issue #863 shape.\n"
        "Either resolve `resolve_effective_config` for the requesting user, or add an "
        "EXEMPT entry with a written reason:\n  " + "\n  ".join(sorted(unresolved))
    )


def test_the_chat_export_resolves_a_policy():
    """The specific route issue #863 was filed about, asserted by name.

    The sweep above would go green again if this route were deleted; this one would not.
    """
    plane = dict((key, endpoint) for key, _path, endpoint in _plane())
    key = "GET /api/chat/conversations/{conversation_uuid}/export"
    assert key in plane
    assert _reaches_resolver(plane[key]), "chat export stopped resolving a redaction policy"


def test_every_exemption_still_names_a_live_route():
    """A stale exemption is a silent hole: it excuses a route that no longer exists, and
    the next route to take that path inherits the excuse."""
    live = {key for key, _path, _endpoint in _plane()}
    stale = sorted(set(EXEMPT) - live)
    assert not stale, f"EXEMPT names routes that no longer exist: {stale}"


def test_no_exemption_covers_a_route_that_already_resolves():
    """An exemption that is not doing any work reads as a decision that was made; delete it."""
    redundant = sorted(
        key for key, _path, endpoint in _plane() if key in EXEMPT and _reaches_resolver(endpoint)
    )
    assert not redundant, f"these EXEMPT entries are unnecessary — delete them: {redundant}"


def test_the_resolver_scan_can_fail():
    """Must-fire control: a handler that resolves nothing must be reported as unresolved."""

    def _handler_that_resolves_nothing():
        return {"ok": True}

    assert not _reaches_resolver(_handler_that_resolves_nothing)
