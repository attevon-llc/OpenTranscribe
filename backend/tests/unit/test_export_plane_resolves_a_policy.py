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
import importlib
import inspect
import sys
import textwrap
import uuid
from functools import cache

import pytest

pytestmark = pytest.mark.unit

#: A path naming any of these is in the plane. Deliberately broad — a false positive costs
#: one EXEMPT line with a reason, a false negative costs another issue #863.
_PLANE_KEYWORDS = ("export", "subtitles", "download")

#: The call that resolves a user's effective redaction policy, admin floor folded in.
_RESOLVER = "resolve_effective_config"

#: The prefix that marks a module as "ours" for alias-following purposes. A module
#: constant (not an inline literal) so the order-independence test below can point it at
#: a synthetic package — a real ``app.*`` name would defeat the point of that test, which
#: is to prove the alias map does not depend on whether the target was already imported.
_APP_MODULE_PREFIX = "app."

#: Celery dispatch methods whose receiver runs LATER, in a different (worker) process.
#: A name reached only through one of these is off the CALLER's own request path — the
#: concrete case is ``subtitles.py``'s ``prepare_bulk_export``, which dispatches
#: ``prepare_bulk_subtitles_task.delay(...)`` and returns immediately; the resolver call
#: inside that task's body must not count as reachable from the route, because the route's
#: request handler never runs it.
_DEFERRED_DISPATCH = frozenset(
    {"delay", "apply_async", "si", "s", "signature", "apply", "map", "starmap", "chunks"}
)

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
def _import_app_module(module_name: str):
    """Import ``module_name``, caching the result. ``None`` on any failure.

    Fails CLOSED, matching every other miss in this scan: a module this cannot import (or
    that raises while importing) is treated as "does not resolve the policy", never
    silently skipped as if it resolved nothing was owed. Needed because, since the fix
    below, ``_imported_resolver_aliases`` records an alias target from SOURCE regardless of
    whether it happens to be in ``sys.modules`` yet — ``_module_functions`` must therefore
    be able to import a module it has not seen before, not just index an existing entry.
    """
    try:
        return importlib.import_module(module_name)
    except Exception:  # noqa: BLE001 — fail closed; any import error means "can't tell"
        return None


@cache
def _module_functions(module_name: str) -> dict[str, str]:
    """Every module-level ``def``/``async def`` in ``module_name``, name -> source."""
    module = _import_app_module(module_name)
    if module is None:
        return {}
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


def _deferred_names(source: str) -> set[str]:
    """Names in ``source`` used ONLY as the receiver of a deferred Celery dispatch call.

    ``some_task.delay(...)`` schedules ``some_task`` to run LATER, in a worker process — it
    does not run it now. A name reached only that way must not be followed into its own
    source when asking whether the CALLER reaches the resolver, because the caller's
    request path never executes it — that was the false "redundant exemption" verdict on
    ``POST /api/files/bulk-export/prepare``. A name that is ALSO called directly somewhere
    in the same source is not deferred-only and is still followed (see
    ``test_a_directly_called_helper_still_counts``).

    ``textwrap.dedent`` is required, not cosmetic: some handler sources are nested
    ``def``s (a method, a local closure, a test's own inline handler) and
    ``ast.parse`` raises ``IndentationError`` on those without it.
    """
    try:
        tree = ast.parse(textwrap.dedent(source))
    except SyntaxError:  # pragma: no cover - defensive; a real handler always parses
        return set()
    deferred: set[str] = set()
    direct: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _DEFERRED_DISPATCH:
            receiver = func.value
            if isinstance(receiver, ast.Name):
                deferred.add(receiver.id)
            elif isinstance(receiver, ast.Attribute):
                deferred.add(receiver.attr)
        elif isinstance(func, ast.Name):
            direct.add(func.id)
        elif isinstance(func, ast.Attribute):
            direct.add(func.attr)
    return deferred - direct


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
        deferred = _deferred_names(source)
        for name, helper_source in functions.items():
            if name in seen or name not in source or name in deferred:
                continue
            seen.add(name)
            pending.append(helper_source)
        # A helper re-exported from another module (chat/export -> export_redaction).
        for alias, target_module in imported_names.items():
            if alias in seen or alias not in source or alias in deferred:
                continue
            seen.add(alias)
            target = _module_functions(target_module).get(alias)
            if target:
                pending.append(target)
    return False


@cache
def _imported_resolver_aliases(module_name: str) -> dict[str, str]:
    """``from app.x.y import z`` in ``module_name`` -> ``{"z": "app.x.y"}`` for app modules.

    Recorded from SOURCE, unconditionally — NOT gated on whether ``node.module`` happens
    to already be in ``sys.modules``. That gate used to be the whole bug: a function-local
    import (``subtitles.py``'s ``prepare_bulk_export`` importing
    ``prepare_bulk_subtitles_task`` inside its own body, right before dispatching it) is
    exactly the shape this exists to catch, and whether some EARLIER, unrelated test in the
    same process happened to import ``app.tasks.media_download`` first must not change the
    verdict — that is what made the bulk-export route's "redundant exemption" verdict
    depend on test execution ORDER.
    """
    module = sys.modules.get(module_name)
    if module is None:
        return {}
    try:
        tree = ast.parse(inspect.getsource(module))
    except (OSError, TypeError, SyntaxError):  # pragma: no cover
        return {}
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith(_APP_MODULE_PREFIX)
        ):
            for alias in node.names:
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


def test_the_summary_export_resolves_a_policy():
    """Issue #885's route, asserted by name — the sweep above goes green if it is deleted.

    #885 was filed as a redaction bypass and the premise was wrong: this route calls the
    same ``_redacted_summary`` helper ``GET .../summary`` does, so there is one masking
    implementation for both, not two that could drift.
    """
    plane = dict((key, endpoint) for key, _path, endpoint in _plane())
    key = "GET /api/files/{file_uuid}/summary/export"
    assert key in plane
    assert _reaches_resolver(plane[key]), "summary export stopped resolving a redaction policy"


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
    """Must-fire control: a handler that resolves nothing must be reported as unresolved.

    ``_handler_that_resolves_nothing`` is a NESTED ``def`` (indented inside this test
    function), so its ``inspect.getsource()`` is not valid standalone Python — this is the
    exact shape ``_deferred_names``'s ``textwrap.dedent`` exists to survive
    (test_the_dedent_survives_a_nested_handler_source below pins that directly).
    """

    def _handler_that_resolves_nothing():
        return {"ok": True}

    assert not _reaches_resolver(_handler_that_resolves_nothing)


def test_the_dedent_survives_a_nested_handler_source():
    """A nested ``def``'s source is indented (this one four spaces, being a local function
    inside a test) and ``ast.parse`` raises ``IndentationError`` on that without a dedent
    first -- the exact shape ``test_the_resolver_scan_can_fail``'s inline handler has, and
    the reason ``_deferred_names`` dedents before parsing."""

    def _nested():
        return {"ok": True}

    source = inspect.getsource(_nested)
    assert source.startswith("    "), "setup invariant: the source really is indented"
    assert _deferred_names(source) == set()


# --------------------------------------------------------------------------------------
# Order-independence + deferred-dispatch regression tests (the fix for the false
# "redundant exemption" verdict on POST /api/files/bulk-export/prepare, which depended on
# whether an earlier test in the same process had already imported app.tasks.media_download)
# --------------------------------------------------------------------------------------


def _write_synthetic_package(tmp_path, monkeypatch, pkg_name: str, modules: dict[str, str]) -> None:
    """Create an importable package ``pkg_name`` under ``tmp_path`` with one file per
    ``modules`` entry (``{"mod.py": "<source>"}``), add ``tmp_path`` to ``sys.path``, and
    point :data:`_APP_MODULE_PREFIX` at it so the alias-recording code treats it as "ours".

    A synthetic package (rather than a real ``app.*`` module) is required for the
    order-independence tests specifically: by the time this suite runs, something else has
    almost certainly already imported any real ``app.*`` module, which would make the old,
    buggy ``node.module in sys.modules`` gate pass VACUOUSLY. Only a module guaranteed
    absent from ``sys.modules`` actually exercises the fix.
    """
    pkg_dir = tmp_path / pkg_name
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    for rel_name, source in modules.items():
        (pkg_dir / rel_name).write_text(source)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(f"{__name__}._APP_MODULE_PREFIX", f"{pkg_name}.")


def test_the_alias_map_does_not_depend_on_what_is_already_imported(tmp_path, monkeypatch):
    """The order-independence fix, isolated from the real app.

    ``outer.py`` imports ``helper_with_resolver`` from ``inner.py`` INSIDE a function body
    -- the exact shape of ``subtitles.py:295``'s function-local
    ``from app.tasks.media_download import prepare_bulk_subtitles_task``. The alias must be
    recorded even though ``inner`` has never been imported by anything: that is precisely
    the condition the old ``node.module in sys.modules`` gate got wrong, and it is why the
    bulk-export route's verdict used to depend on which OTHER test ran first.
    """
    pkg = f"synth_alias_{uuid.uuid4().hex}"
    _write_synthetic_package(
        tmp_path,
        monkeypatch,
        pkg,
        {
            "inner.py": (
                "def helper_with_resolver():\n"
                "    from app.services.redaction.config import resolve_effective_config\n"
                "    return resolve_effective_config\n"
            ),
            "outer.py": (
                f"def handler():\n"
                f"    from {pkg}.inner import helper_with_resolver\n"
                f"    return helper_with_resolver()\n"
            ),
        },
    )
    importlib.import_module(f"{pkg}.outer")
    assert f"{pkg}.inner" not in sys.modules, "setup invariant: the target must start absent"

    aliases = _imported_resolver_aliases(f"{pkg}.outer")

    assert aliases.get("helper_with_resolver") == f"{pkg}.inner"


def test_a_dispatched_celery_task_is_not_on_the_request_path(tmp_path, monkeypatch):
    """Must-fire: a handler that only DISPATCHES a task (``.delay()``) must not be reported
    as reaching the resolver just because the dispatched task's OWN body happens to mention
    it -- the route's request handler never runs that body; a worker does, later."""
    pkg = f"synth_deferred_{uuid.uuid4().hex}"
    _write_synthetic_package(
        tmp_path,
        monkeypatch,
        pkg,
        {
            "tasks.py": (
                "def some_task_body():\n"
                "    from app.services.redaction.config import resolve_effective_config\n"
                "    return resolve_effective_config\n"
            ),
            "routes.py": (
                f"def handler():\n"
                f"    from {pkg}.tasks import some_task_body\n"
                f"    some_task_body.delay()\n"
            ),
        },
    )
    routes = importlib.import_module(f"{pkg}.routes")

    assert not _reaches_resolver(routes.handler)


def test_a_directly_called_helper_still_counts(tmp_path, monkeypatch):
    """Must-stay-clean pair for the test above: the SAME helper, called SYNCHRONOUSLY
    instead of via ``.delay()``, must still be followed. Required alongside the must-fire
    test: a detector that always answered "not reachable" for any dispatched-looking name
    would pass that test for the wrong reason -- this is what proves it is actually reading
    the dispatch method, not just declining to follow anything."""
    pkg = f"synth_direct_{uuid.uuid4().hex}"
    _write_synthetic_package(
        tmp_path,
        monkeypatch,
        pkg,
        {
            "tasks.py": (
                "def some_task_body():\n"
                "    from app.services.redaction.config import resolve_effective_config\n"
                "    return resolve_effective_config\n"
            ),
            "routes.py": (
                f"def handler():\n"
                f"    from {pkg}.tasks import some_task_body\n"
                f"    return some_task_body()\n"
            ),
        },
    )
    routes = importlib.import_module(f"{pkg}.routes")

    assert _reaches_resolver(routes.handler)


def test_the_bulk_export_prepare_route_resolves_nothing_in_request():
    """Ground-truth pin, asserted directly rather than inferred from the sweep above: the
    bulk-export prepare route's own REQUEST handler resolves no policy at all -- the worker
    resolves it later, from a ``user_id`` -- and the route is still exempted for that reason.
    """
    plane = dict((key, endpoint) for key, _path, endpoint in _plane())
    key = "POST /api/files/bulk-export/prepare"
    assert key in plane
    assert not _reaches_resolver(plane[key]), (
        "the bulk-export prepare route's request handler must not appear to resolve a "
        "policy -- that call lives in the dispatched Celery task's body, which runs later "
        "in a worker process"
    )
    assert key in EXEMPT
