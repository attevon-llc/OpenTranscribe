"""Every file-identifying WebSocket event must route through the quarantine guard.

Issue #908 found 20 ``send_ws_event`` call sites that carry ``MediaFile`` identity
(filename, title, thumbnail, a speaker tied to a recording) but bypassed the one
existing chokepoint (``notification_service.send_task_notification`` +
``takedown_service.is_notification_suppressed``) — so a live WebSocket push could
still name a quarantined file to a non-admin recipient even though the same file
404s on every read surface. ``app/utils/websocket_notify.py:send_ws_event_for_file``
is the fix; this module is the gate that keeps a new unguarded call site from
reintroducing the leak.

Two independent scanners live here, both AST-based and both allowlist-gated with a
mandatory written reason, matching this repo's ``test_ddl_marker_discipline.py``
pattern:

  1. Every call to ``send_ws_event`` (the raw, quarantine-blind primitive), matched
     three ways: a plain ``Name`` call, an ``Attribute`` call
     (``websocket_notify.send_ws_event(...)``), and a call through an aliased
     import (``from ... import send_ws_event as _push``).
  2. Every raw ``.publish("websocket_notifications", ...)`` call — the four
     call sites that publish directly to the Redis channel rather than going
     through ``send_ws_event`` at all, and are therefore invisible to scanner 1
     (issue #908, finding A).

Both scanners are keyed ``"<path relative to app/>::<enclosing function>"`` — never
by line number, which drifts on every unrelated edit to the file. The module that
DEFINES the two primitives (``app/utils/websocket_notify.py``) is excluded from
both scans: its own internal calls ARE the implementation, not a call site that
needs a guard.
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]
_APP_ROOT = _TESTS_ROOT.parent / "app"

#: The module that defines the two primitives. Its own internal
#: ``send_ws_event`` call (inside ``send_ws_event_for_file``) and its own
#: ``.publish("websocket_notifications", ...)`` call are the implementation,
#: not a site this gate polices.
_DEFINING_MODULE = "utils/websocket_notify.py"

_RAW_CHANNEL = "websocket_notifications"

#: (expected_count, written reason) per ``"<path>::<function>"`` key.
#:
#: All entries below are the EXEMPT sites from issue #908's inventory (corpus-wide
#: counters, group/collection identity, admin-migration status with no single
#: MediaFile, or a payload whose file identity is a fallback for "there is no file
#: to guard") plus the DMCA owner-notice chokepoint itself. A new key here is a new
#: place quarantine can leak — write the reason like you mean it.
_DIRECT_SEND_ALLOWLIST: dict[str, tuple[int, str]] = {
    "api/endpoints/combined_speaker_migration.py::stop_combined_migration": (
        1,
        "admin migration control-plane event (stopped/running), no MediaFile identity",
    ),
    "api/endpoints/embedding_migration.py::stop_migration": (
        1,
        "admin migration control-plane event, no MediaFile identity",
    ),
    "api/endpoints/embedding_migration.py::force_complete_migration": (
        1,
        "admin migration control-plane event, no MediaFile identity",
    ),
    "api/endpoints/groups.py::add_member": (
        1,
        "group-membership event; identifies a Group and a User, never a MediaFile",
    ),
    "api/endpoints/groups.py::remove_member": (
        1,
        "group-membership event; identifies a Group and a User, never a MediaFile",
    ),
    "api/endpoints/media_collections.py::_notify_share_event": (
        1,
        "collection-share event; identifies a MediaCollection, never a MediaFile",
    ),
    "api/endpoints/media_collections.py::delete_collection_share": (
        1,
        "collection-share event; identifies a MediaCollection, never a MediaFile",
    ),
    "api/endpoints/speaker_attribute_migration.py::stop_attribute_migration": (
        1,
        "admin migration control-plane event, no MediaFile identity",
    ),
    "api/endpoints/speakers.py::verify_speaker_identification": (
        1,
        "fallback branch only: the speaker has no media_file at all "
        "(media_file_uuid is None), so there is no file a quarantine could hide. "
        "The primary send in this function IS guarded via send_ws_event_for_file.",
    ),
    "api/endpoints/speakers.py::confirm_speaker_gender": (
        1,
        "fallback branch only: the speaker has no media_file at all "
        "(media_file_uuid is None), so there is no file a quarantine could hide. "
        "The primary send in this function IS guarded via send_ws_event_for_file.",
    ),
    "services/notification_service.py::send_task_notification": (
        1,
        "this IS the pre-#908 quarantine chokepoint — it calls "
        "takedown_service.is_notification_suppressed itself before this line",
    ),
    "services/notification_service.py::send_file_cache_invalidation": (
        1,
        "a cache INVALIDATION is the inverse of disclosure — guarding it would "
        "be wrong, not merely unnecessary",
    ),
    "tasks/combined_speaker_analysis_task.py::_notify_migration_admin": (
        1,
        "issue #908 finding B fix: routes to the correctly-resolved admin "
        "recipient (never falls back to account id 1); the payload is a "
        "corpus-wide migration status with a failed-file UUID list sent back "
        "to the admin who requested the run, same shape as the "
        "already-exempt opensearch_integrity_task.py::_notify",
    ),
    "tasks/embedding_consistency_repair.py::_check_repair_completion": (
        1,
        "admin consistency-repair corpus-wide status, no single MediaFile identity",
    ),
    "tasks/embedding_migration_v4.py::_notify_migration_admin": (
        1,
        "issue #908 finding B fix: routes to the correctly-resolved admin "
        "recipient (never falls back to account id 1); the payload is a "
        "corpus-wide migration status with a failed-file UUID list sent back "
        "to the admin who requested the run, same shape as the "
        "already-exempt opensearch_integrity_task.py::_notify",
    ),
    "tasks/embedding_migration_v4.py::finalize_v4_migration_task": (
        2,
        "admin migration finalization status, no single MediaFile identity",
    ),
    "tasks/opensearch_integrity_task.py::_notify": (
        1,
        "the exemplar this issue's finding B fix copies: already gated on "
        "`user_id is None`, corpus-wide data-integrity status",
    ),
    "tasks/reindex_task.py::_send_reindex_progress": (
        1,
        "corpus-wide reindex progress counters, no single MediaFile identity",
    ),
    "tasks/reindex_task.py::_handle_reindex_completion": (
        2,
        "corpus-wide reindex completion counters, no single MediaFile identity",
    ),
    "tasks/speaker_attribute_migration_task.py::_notify_migration_admin": (
        1,
        "issue #908 finding B fix: routes to the correctly-resolved admin "
        "recipient (never falls back to account id 1); the payload is a "
        "corpus-wide migration status with a failed-file UUID list sent back "
        "to the admin who requested the run, same shape as the "
        "already-exempt opensearch_integrity_task.py::_notify",
    ),
    "tasks/speaker_clustering.py::_send_clustering_progress": (
        1,
        "corpus-wide re-clustering progress for the calling user, no single MediaFile identity",
    ),
    "tasks/speaker_clustering.py::_send_clustering_complete": (
        1,
        "corpus-wide re-clustering completion for the calling user, no single MediaFile identity",
    ),
    "tasks/speaker_clustering.py::_send_clustering_error": (
        1,
        "corpus-wide re-clustering error for the calling user, no single MediaFile identity",
    ),
    "tasks/speaker_embedding_consistency.py::speaker_embedding_consistency_check_task": (
        2,
        "admin consistency-check corpus-wide progress/status, no single MediaFile identity",
    ),
    "tasks/speaker_embedding_consistency.py::stop_consistency_repair": (
        1,
        "admin consistency-repair control-plane event, no MediaFile identity",
    ),
    "tasks/speaker_merge_task.py::process_speaker_merge_background": (
        1,
        "fallback branch only: the surviving speaker has no media_file at all "
        "(media_file_uuid is None), so there is no file a quarantine could "
        "hide. The primary send in this function IS guarded via "
        "send_ws_event_for_file.",
    ),
    "tasks/speaker_update_task.py::process_speaker_update_background": (
        1,
        "fallback branch only: the speaker has no media_file at all "
        "(media_file_uuid is None), so there is no file a quarantine could "
        "hide. The primary send in this function IS guarded via "
        "send_ws_event_for_file.",
    ),
    "tasks/watch_source_tasks.py::_notify_scan_complete": (
        1,
        "watch-source scan-completion event; identifies a WatchSource, never a MediaFile",
    ),
}

#: (expected_count, written reason) per ``"<path>::<function>"`` key, for the raw
#: ``websocket_notifications`` channel publish scanner (issue #908, finding A).
_RAW_PUBLISH_ALLOWLIST: dict[str, tuple[int, str]] = {
    "tasks/utility.py::update_gpu_stats": (
        1,
        "deployment-wide GPU stats broadcast, no MediaFile identity",
    ),
    "services/redis_cache_service.py::_push_invalidation": (
        1,
        "a cache INVALIDATION is the inverse of disclosure — guarding it "
        "would be wrong, not merely unnecessary (same reasoning as "
        "notification_service.send_file_cache_invalidation)",
    ),
    "services/video_processing_service.py::_send_download_progress": (
        1,
        "issue #908 finding A fix: guarded inline via "
        "takedown_service.is_notification_suppressed(file_id, user_id) "
        "before this publish call, in the same function",
    ),
    "services/progress_tracker.py::emit_progress_notification": (
        1,
        "counters only today (issue #908, finding A) — every caller passes "
        "corpus-wide progress data, never a single MediaFile's identity. "
        "This scanner exists so a FUTURE file-identifying payload here "
        "cannot slip through silently: raising this function's payload to "
        "carry file identity must come with a matching allowlist change, "
        "which is the point where a reviewer is forced to ask whether it "
        "needs the guard.",
    ),
    "api/websockets.py::publish_notification": (
        1,
        "generic publish helper; its one caller (speaker_update.py's "
        "speakers_bulk_updated event) carries a speaker display_name and a "
        "count but no media_file_id at all — a genuinely corpus-wide bulk "
        "operation across many speakers/files, not tied to one file",
    ),
}


def _iter_app_modules() -> list[Path]:
    return sorted(_APP_ROOT.rglob("*.py"))


def _rel(path: Path) -> str:
    return path.relative_to(_APP_ROOT).as_posix()


def _send_ws_event_aliases(tree: ast.Module) -> frozenset[str]:
    """Local names bound to ``send_ws_event`` via ``from ... import ... as ...``."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            if alias.name == "send_ws_event":
                aliases.add(alias.asname or alias.name)
    return frozenset(aliases)


class _EnclosingFunctionWalker(ast.NodeVisitor):
    """Walks a module tracking the innermost enclosing function for each Call."""

    def __init__(self) -> None:
        self._stack: list[str] = ["<module>"]
        self.direct_send_findings: list[tuple[str, int]] = []
        self.raw_publish_findings: list[tuple[str, int]] = []
        self.send_ws_event_for_file_findings: list[tuple[str, int, ast.Call]] = []
        self._send_aliases: frozenset[str] = frozenset()

    def configure(self, send_aliases: frozenset[str]) -> None:
        self._send_aliases = send_aliases

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    # noqa reason: `visit_AsyncFunctionDef` is dispatched by name from
    # `ast.NodeVisitor.generic_visit` ("visit_" + node.__class__.__name__), and
    # `ast.AsyncFunctionDef` is itself mixedCase in the stdlib `ast` module — a
    # snake_case alias here would simply never be called.
    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]  # noqa: N815

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        func = node.func
        name: str | None = None
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr

        if name == "send_ws_event" or (
            isinstance(func, ast.Name) and func.id in self._send_aliases
        ):
            self.direct_send_findings.append((self._stack[-1], node.lineno))
        elif name == "send_ws_event_for_file":
            self.send_ws_event_for_file_findings.append((self._stack[-1], node.lineno, node))
        elif name == "publish":
            for arg in node.args:
                if (
                    isinstance(arg, ast.Constant)
                    and isinstance(arg.value, str)
                    and arg.value == _RAW_CHANNEL
                ):
                    self.raw_publish_findings.append((self._stack[-1], node.lineno))
                    break

        self.generic_visit(node)


@functools.cache
def _scan() -> tuple[dict[str, list[int]], dict[str, list[int]], list[tuple[str, int]]]:
    """Return (direct_send_by_key, raw_publish_by_key, for_file_selector_findings).

    ``for_file_selector_findings`` is ``(key, lineno)`` for every
    ``send_ws_event_for_file`` call that passes NO selector keyword at all
    (``file_id=``/``file_uuid=``/``file_uuids=``) — a purely syntactic check;
    the wrapper itself is what actually enforces "exactly one" at runtime.
    """
    direct_send: dict[str, list[int]] = {}
    raw_publish: dict[str, list[int]] = {}
    no_selector: list[tuple[str, int]] = []

    for path in _iter_app_modules():
        rel = _rel(path)
        if rel == _DEFINING_MODULE:
            continue
        source = path.read_text()
        if "send_ws_event" not in source and _RAW_CHANNEL not in source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - a syntax error fails collection anyway
            continue

        walker = _EnclosingFunctionWalker()
        walker.configure(_send_ws_event_aliases(tree))
        walker.visit(tree)

        for fn, lineno in walker.direct_send_findings:
            direct_send.setdefault(f"{rel}::{fn}", []).append(lineno)
        for fn, lineno in walker.raw_publish_findings:
            raw_publish.setdefault(f"{rel}::{fn}", []).append(lineno)
        for fn, lineno, call in walker.send_ws_event_for_file_findings:
            selector_kwargs = {"file_id", "file_uuid", "file_uuids"}
            given = {kw.arg for kw in call.keywords if kw.arg in selector_kwargs}
            if not given:
                no_selector.append((f"{rel}::{fn}", lineno))

    return direct_send, raw_publish, no_selector


_DIRECT_SEND, _RAW_PUBLISH, _NO_SELECTOR = _scan()


def test_the_scanner_actually_finds_call_sites() -> None:
    """A scanner that matches nothing is indistinguishable from a clean tree.

    Per this repo's audit-tooling rule (see test_ddl_marker_discipline.py /
    scripts/audit-tests.py), any structural gate needs a must-fire assertion
    against the REAL tree, not just its own synthetic selftest cases.
    """
    assert len(_DIRECT_SEND) >= 25, (
        f"expected at least 25 distinct direct send_ws_event call sites in the "
        f"real tree, found {len(_DIRECT_SEND)} — either the scanner regressed or "
        "the codebase genuinely eliminated most of them (update this floor "
        "deliberately if so)"
    )
    assert len(_RAW_PUBLISH) >= 4, (
        f"expected at least 4 raw websocket_notifications publish call sites, "
        f"found {len(_RAW_PUBLISH)}"
    )


def test_no_unallowlisted_direct_send_ws_event_call_sites() -> None:
    """A direct send_ws_event call site not on the allowlist is a new leak.

    Route it through send_ws_event_for_file(file_id=... | file_uuid=... |
    file_uuids=...), or — if it genuinely carries no MediaFile identity — add
    a `_DIRECT_SEND_ALLOWLIST` entry here with a written reason.
    """
    unallowlisted = sorted(set(_DIRECT_SEND) - set(_DIRECT_SEND_ALLOWLIST))
    assert not unallowlisted, (
        "These call a quarantine-blind send_ws_event with no allowlist entry. "
        "Route through send_ws_event_for_file, or add a reasoned "
        "_DIRECT_SEND_ALLOWLIST entry if this event genuinely carries no "
        "MediaFile identity:\n  " + "\n  ".join(unallowlisted)
    )


def test_a_site_may_not_exceed_its_allowlisted_count() -> None:
    """A NEW unguarded call added to an already-allowlisted function is still a leak."""
    over: list[str] = []
    for key, findings in _DIRECT_SEND.items():
        expected = _DIRECT_SEND_ALLOWLIST.get(key)
        if expected is not None and len(findings) > expected[0]:
            over.append(f"{key} (found {len(findings)}, allowlisted {expected[0]})")
    assert not over, (
        "These functions have MORE direct send_ws_event calls than their "
        "allowlist entry accounts for — a new call site was added without "
        "updating the count (or without routing it through "
        "send_ws_event_for_file):\n  " + "\n  ".join(over)
    )


def test_the_allowlist_is_honest() -> None:
    """Every allowlist entry must describe a real, currently-true fact."""
    stale: list[str] = []
    inflated: list[str] = []
    unexplained: list[str] = []
    for key, (expected_count, reason) in sorted(_DIRECT_SEND_ALLOWLIST.items()):
        actual = len(_DIRECT_SEND.get(key, []))
        if actual == 0:
            stale.append(key)
        elif actual < expected_count:
            inflated.append(f"{key} (allowlisted {expected_count}, found {actual})")
        if not reason.strip():
            unexplained.append(key)

    assert not stale, f"allowlist entries point at functions with no finding at all: {stale}"
    assert not inflated, f"allowlist entries claim more findings than exist: {inflated}"
    assert not unexplained, f"allowlist entries need a written reason: {unexplained}"


def test_every_send_ws_event_for_file_call_passes_a_selector() -> None:
    """A syntactic check: every call site names at least one selector keyword.

    This does not replace the wrapper's own runtime "exactly one" check
    (test_ws_event_file_wrapper.py covers that) — it exists so a call site
    that passes ZERO selectors (and would raise TypeError at runtime, on the
    very first invocation) is caught by the fast unit suite instead of only
    surfacing the first time that code path actually runs.
    """
    assert not _NO_SELECTOR, (
        "These send_ws_event_for_file call sites pass no file_id/file_uuid/"
        "file_uuids keyword at all and will raise TypeError at runtime:\n  "
        + "\n  ".join(f"{key} (line {lineno})" for key, lineno in _NO_SELECTOR)
    )


# --- Raw `websocket_notifications` channel publish scanner (finding A) ---


def test_no_unallowlisted_raw_websocket_notifications_publish() -> None:
    """A raw publish to the channel is invisible to the send_ws_event scanner above.

    Four sites publish directly to Redis rather than calling send_ws_event at
    all (issue #908, finding A) — a fifth appearing here with no allowlist
    entry is a new one of those, and needs the same "does this carry
    MediaFile identity" review as an unguarded send_ws_event call.
    """
    unallowlisted = sorted(set(_RAW_PUBLISH) - set(_RAW_PUBLISH_ALLOWLIST))
    assert not unallowlisted, (
        "These publish directly to the websocket_notifications channel with "
        "no allowlist entry. Review whether the payload carries MediaFile "
        "identity — if so it needs an inline quarantine-suppression check "
        "(see services/video_processing_service.py::_send_download_progress "
        "for the pattern); either way, add a reasoned "
        "_RAW_PUBLISH_ALLOWLIST entry:\n  " + "\n  ".join(unallowlisted)
    )


def test_a_raw_publish_site_may_not_exceed_its_allowlisted_count() -> None:
    over: list[str] = []
    for key, findings in _RAW_PUBLISH.items():
        expected = _RAW_PUBLISH_ALLOWLIST.get(key)
        if expected is not None and len(findings) > expected[0]:
            over.append(f"{key} (found {len(findings)}, allowlisted {expected[0]})")
    assert not over, (
        "These functions have MORE raw websocket_notifications publish calls "
        "than their allowlist entry accounts for:\n  " + "\n  ".join(over)
    )


def test_the_raw_publish_allowlist_is_honest() -> None:
    stale: list[str] = []
    inflated: list[str] = []
    unexplained: list[str] = []
    for key, (expected_count, reason) in sorted(_RAW_PUBLISH_ALLOWLIST.items()):
        actual = len(_RAW_PUBLISH.get(key, []))
        if actual == 0:
            stale.append(key)
        elif actual < expected_count:
            inflated.append(f"{key} (allowlisted {expected_count}, found {actual})")
        if not reason.strip():
            unexplained.append(key)

    assert not stale, f"allowlist entries point at functions with no finding at all: {stale}"
    assert not inflated, f"allowlist entries claim more findings than exist: {inflated}"
    assert not unexplained, f"allowlist entries need a written reason: {unexplained}"
