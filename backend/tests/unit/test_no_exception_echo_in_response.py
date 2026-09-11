"""Exception text must never be echoed verbatim into an HTTP response (#859, #891).

``str(e)`` (or anything derived from it) landing in an ``HTTPException`` detail, an
``ErrorHandler.*`` builder, or a ``raise <OpenTranscribeError subclass>(...)`` message
exposes internal details — file paths, DB connection strings, broker URLs with
embedded credentials, SQL fragments — to any caller who triggers the error. #859
originally flagged the API/endpoint layer; #891 is the identical defect one layer
down in the SERVICE layer, which #859's own remediation explicitly excluded.

Two independent, AST-based scanners live here, both allowlist-gated with a mandatory
written reason, matching this repo's ``test_ws_event_quarantine_discipline.py`` /
``test_ddl_marker_discipline.py`` pattern.

**Scanner 1 (raise-path) scans the WHOLE ``app/`` tree, not just ``app/api/``.**
This is the critical detail #891 is about: a raise made in ``app/services/``
propagates up and is rendered by ``main.py``'s ``OpenTranscribeError`` handler
regardless of which layer raised it (that handler renders ``exc.message`` verbatim
as the response ``detail`` — it is a RENDERER, not a sanitizer). Scoping the scan to
``app/api/`` only would silently miss the service layer forever, which is exactly
what let ``services/search/model_switch.py::dispatch_reindex_for_every_owner`` leak
a Redis broker URL (with an embedded password) into a 503 response for months.
``test_the_scan_root_includes_the_service_layer`` pins this: it fails loudly if
anyone ever "simplifies" the scan root back down to ``app/api/``.

**Scanner 2 (return-path) scans ``app/api/`` ONLY.** A value merely *returned* from a
service function isn't necessarily response-bound — many service functions return
status tuples/dicts used for internal logic, not just API responses — so only the
endpoint layer, which IS necessarily response-bound (a FastAPI handler's return value
IS the response body), is scanned for this construct. ``app/services/``'s ~60
similar-looking return-path patterns are a documented, deliberate residual — see the
follow-up issue filed alongside this gate; they are not covered here.

Taint tracking (shared by both scanners): the exception variable bound in
``except ... as e``, plus every local variable assigned from it — **including
through a function call** (``safe = clean(str(e))`` must still taint ``safe``).
There is deliberately NO trusted-sanitizer-name escape hatch: the investigation
found a function literally named ``create_user_friendly_error``
(``services/media_download_service.py``) that is NOT actually a sanitizer — it only
strips four literal prefixes on the non-auth-error branch. A name-based escape
hatch would hide exactly this class of false confidence, so the scanner never looks
at a callee's name, only at whether an expression's AST subtree references a
tainted variable.
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]
_APP_ROOT = _TESTS_ROOT.parent / "app"
_API_ROOT = _APP_ROOT / "api"

#: The names ErrorHandler exposes. Any of these called as ``ErrorHandler.<name>(...)``
#: is in scope; the scanner does not hardcode this list for matching (it matches any
#: attribute access on an object literally named ``ErrorHandler``), it exists only for
#: documentation.
_ERROR_HANDLER_OBJECT_NAME = "ErrorHandler"


def _iter_py_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def _rel_to_app(path: Path) -> str:
    return path.relative_to(_APP_ROOT).as_posix()


def _references_tainted(expr: ast.AST | None, tainted: frozenset[str]) -> bool:
    """True if any ``Name`` node inside *expr* is a member of *tainted*.

    Deliberately structural and name-blind: no allowance is made for a callee
    that "looks like" a sanitizer. See the module docstring.
    """
    if expr is None or not tainted:
        return False
    return any(isinstance(node, ast.Name) and node.id in tainted for node in ast.walk(expr))


def _taint_from_except_handler(
    node: ast.ExceptHandler, inherited: frozenset[str]
) -> frozenset[str]:
    """Fixed-point taint set visible inside one ``except`` handler's body.

    Seeds with the handler's own bound name (``except ... as e``) plus whatever was
    already tainted in an enclosing scope (a nested try/except inside an except
    suite keeps the outer name visible), then repeatedly walks every assignment in
    the handler's body until no new names are added. Handles ``Assign``,
    ``AnnAssign`` and the walrus operator; tuple-target assignments are not
    unpacked (not needed by any real call site found in this codebase).
    """
    tainted: set[str] = set(inherited)
    if node.name:
        tainted.add(node.name)

    changed = True
    while changed:
        changed = False
        for n in ast.walk(node):
            value: ast.AST | None = None
            target: ast.AST | None = None
            if isinstance(n, ast.Assign) and len(n.targets) == 1:
                target, value = n.targets[0], n.value
            elif (isinstance(n, ast.AnnAssign) and n.value is not None) or isinstance(
                n, ast.NamedExpr
            ):
                target, value = n.target, n.value
            else:
                continue
            if (
                isinstance(target, ast.Name)
                and target.id not in tainted
                and _references_tainted(value, frozenset(tainted))
            ):
                tainted.add(target.id)
                changed = True
    return frozenset(tainted)


def _call_target_name(call: ast.Call) -> str | None:
    """The bare name/attribute a Call targets (``HTTPException``, ``internal_error``, ...)."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _is_error_handler_call(call: ast.Call) -> bool:
    """True for ``ErrorHandler.<any_builder>(...)`` — matched on the OBJECT name."""
    return (
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == _ERROR_HANDLER_OBJECT_NAME
    )


def _httpexception_tainted(call: ast.Call, tainted: frozenset[str]) -> bool:
    """``HTTPException(...)``'s ``detail=`` keyword or 2nd positional arg."""
    for kw in call.keywords:
        if kw.arg == "detail" and _references_tainted(kw.value, tainted):
            return True
    return len(call.args) >= 2 and _references_tainted(call.args[1], tainted)


def _any_arg_tainted(call: ast.Call, tainted: frozenset[str]) -> bool:
    """Any positional or keyword argument references a tainted name."""
    return any(_references_tainted(a, tainted) for a in call.args) or any(
        _references_tainted(kw.value, tainted) for kw in call.keywords
    )


def _is_response_bound_finding(
    call: ast.Call, tainted: frozenset[str], error_subclasses: frozenset[str]
) -> bool:
    """True when *call* is a response-bound construct carrying tainted text."""
    if not tainted or not isinstance(call, ast.Call):
        return False
    target = _call_target_name(call)
    if target == "HTTPException":
        return _httpexception_tainted(call, tainted)
    if _is_error_handler_call(call):
        return _any_arg_tainted(call, tainted)
    if target in error_subclasses:
        return _any_arg_tainted(call, tainted)
    return False


class _TaintWalker(ast.NodeVisitor):
    """Walks a module tracking (enclosing function, in-scope taint) for Raise/Return."""

    def __init__(self, error_subclasses: frozenset[str]) -> None:
        self._error_subclasses = error_subclasses
        self._func_stack: list[str] = ["<module>"]
        self._taint_stack: list[frozenset[str]] = [frozenset()]
        self.raise_findings: list[tuple[str, int]] = []
        self.return_findings: list[tuple[str, int]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._func_stack.append(node.name)
        self.generic_visit(node)
        self._func_stack.pop()

    # noqa reason: dispatched by name from ast.NodeVisitor.generic_visit
    # ("visit_" + node.__class__.__name__); ast.AsyncFunctionDef is itself
    # mixedCase in the stdlib ast module, so a snake_case alias is never called.
    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]  # noqa: N815

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:  # noqa: N802
        tainted = _taint_from_except_handler(node, self._taint_stack[-1])
        self._taint_stack.append(tainted)
        self.generic_visit(node)
        self._taint_stack.pop()

    def visit_Raise(self, node: ast.Raise) -> None:  # noqa: N802
        tainted = self._taint_stack[-1]
        if isinstance(node.exc, ast.Call) and _is_response_bound_finding(
            node.exc, tainted, self._error_subclasses
        ):
            self.raise_findings.append((self._func_stack[-1], node.lineno))
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:  # noqa: N802
        tainted = self._taint_stack[-1]
        if node.value is not None and _references_tainted(node.value, tainted):
            self.return_findings.append((self._func_stack[-1], node.lineno))
        self.generic_visit(node)


def _collect_class_bases(tree: ast.Module) -> dict[str, set[str]]:
    """``{class_name: {base_name, ...}}`` for every ``ClassDef`` in *tree*."""
    bases: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        names: set[str] = set()
        for b in node.bases:
            if isinstance(b, ast.Name):
                names.add(b.id)
            elif isinstance(b, ast.Attribute):
                names.add(b.attr)
        bases.setdefault(node.name, set()).update(names)
    return bases


def _derive_error_subclasses(
    base_map: dict[str, set[str]], root: str = "OpenTranscribeError"
) -> frozenset[str]:
    """Transitive closure of every class name that (transitively) subclasses *root*.

    Derived dynamically from the real class hierarchy under ``app/`` — never
    hardcoded — so a new ``OpenTranscribeError`` subclass is covered automatically.
    """
    subclasses = {root}
    changed = True
    while changed:
        changed = False
        for cls, cls_bases in base_map.items():
            if cls not in subclasses and cls_bases & subclasses:
                subclasses.add(cls)
                changed = True
    return frozenset(subclasses)


def _open_transcribe_error_subclasses_under(root: Path) -> frozenset[str]:
    base_map: dict[str, set[str]] = {}
    for path in _iter_py_files(root):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - a syntax error fails collection anyway
            continue
        for cls, cls_bases in _collect_class_bases(tree).items():
            base_map.setdefault(cls, set()).update(cls_bases)
    return _derive_error_subclasses(base_map)


@functools.cache
def _scan() -> tuple[dict[str, list[int]], dict[str, list[int]]]:
    """Return ``(raise_by_key, return_by_key)``.

    ``raise_by_key`` is scanned over the WHOLE ``app/`` tree (the #891 requirement).
    ``return_by_key`` is scanned over ``app/api/`` ONLY (see module docstring).
    """
    error_subclasses = _open_transcribe_error_subclasses_under(_APP_ROOT)

    raise_by_key: dict[str, list[int]] = {}
    for path in _iter_py_files(_APP_ROOT):
        rel = _rel_to_app(path)
        source = path.read_text()
        if "raise " not in source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover
            continue
        walker = _TaintWalker(error_subclasses)
        walker.visit(tree)
        for fn, lineno in walker.raise_findings:
            raise_by_key.setdefault(f"{rel}::{fn}", []).append(lineno)

    return_by_key: dict[str, list[int]] = {}
    for path in _iter_py_files(_API_ROOT):
        rel = _rel_to_app(path)
        source = path.read_text()
        if "except" not in source or "return" not in source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover
            continue
        walker = _TaintWalker(error_subclasses)
        walker.visit(tree)
        for fn, lineno in walker.return_findings:
            return_by_key.setdefault(f"{rel}::{fn}", []).append(lineno)

    return raise_by_key, return_by_key


#: (expected_count, written reason) per ``"<path relative to app/>::<function>"`` key.
#: Every entry here raises/echoes OUR OWN authored message, never a caught library
#: exception's raw text — verified against the real source, not copied blind.
_RAISE_ALLOWLIST: dict[str, tuple[int, str]] = {
    "api/endpoints/admin_group_mappings.py::create_group_mapping": (
        1,
        "our own RoleNotGrantableError message (assert_grantable_role's refusal), "
        "never a caught library exception",
    ),
    "api/endpoints/admin_group_mappings.py::update_group_mapping": (
        1,
        "same RoleNotGrantableError re-check after a patch",
    ),
    "api/endpoints/admin_group_mappings.py::_ldap_claims_for": (
        1,
        "our own LdapDirectoryUnavailableError message — superuser-only diagnostic "
        "describing the admin's own directory configuration, not a caller-triggered leak",
    ),
    "api/endpoints/auth/dependencies.py::_authenticate_external_token": (
        1,
        "our own translated PermissionError message from sync_external_user_to_db "
        "(a refused link, e.g. unverified email match), not a raw library exception",
    ),
    "api/endpoints/auth_config.py::update_config_category": (
        1,
        "already has a written justification in the source: 'A rejected payload is "
        "the caller's fault and its detail is safe to return: it names the offending "
        "keys so the admin can fix the request' — a ValueError from our own "
        "bulk_update_category validator",
    ),
    "api/endpoints/auth_email_delivery.py::update_auth_mail_designation": (
        1,
        "our own ValueError from designation.set_designation (malformed UUID, "
        "unknown or disabled config) — a validator message, not a caught library error",
    ),
    "api/endpoints/backup_settings.py::update_backup_settings": (
        1,
        "our own ValueError from backup_service.update_settings — a validator message",
    ),
    "api/endpoints/directory_sync_settings.py::update_directory_sync_settings": (
        1,
        "our own ValueError from directory_sync_service.update_settings — a validator message",
    ),
    "api/endpoints/files/subtitles.py::get_subtitles": (
        1,
        "narrow ValueError from our own SubtitleService generator, not a caught "
        "third-party exception",
    ),
    "api/endpoints/files/transcript_export.py::export_transcript": (
        1,
        "narrow ValueError from our own build_export_content service, not a caught "
        "third-party exception",
    ),
    "api/endpoints/media_mirror_settings.py::update_mirror_settings": (
        1,
        "our own ValueError from media_mirror_service.update_settings — a validator message",
    ),
    "api/endpoints/search.py::_summary_search_payload": (
        1,
        "our own SummaryMaskingUnavailableError — a fail-closed masking-outage "
        "message we author, not a caught library exception",
    ),
    "api/endpoints/search.py::trigger_reindex": (
        1,
        "our own ReindexDispatchError message — only true now that "
        "services/search/model_switch.py::dispatch_reindex_for_every_owner (#891 "
        "exemplar) no longer interpolates the raw broker exception into that message",
    ),
    "api/endpoints/search.py::_switch_model": (
        3,
        "our own UnknownEmbeddingModelError / EmbeddingModelNotDeployedError / "
        "ReindexDispatchError messages, all authored by model_switch.py itself",
    ),
    "api/endpoints/speaker_clusters.py::analyze_outliers": (
        1,
        "verified: SpeakerClusteringService.analyze_gender_outliers raises only the "
        "fixed literal 'Cluster not found', never a path/uuid-carrying message",
    ),
    "api/endpoints/speaker_clusters.py::unassign_speakers": (
        1,
        "verified: SpeakerClusteringService.unassign_speakers raises only fixed "
        "literals like 'No valid speakers found'",
    ),
    "api/endpoints/tags/_common.py::_apply": (
        1,
        "our own TagNotFoundError message (the InvalidTagNameError branch beside it "
        "raises a fixed literal and is not tainted at all)",
    ),
    "api/endpoints/tags/sharing.py::create_tag_share": (
        1,
        "our own TagShareError message from services/tag_sharing.share_tag",
    ),
    "api/endpoints/watch_sources.py::browse_directories": (
        1,
        "verified: folder_browser.list_directories raises only three fixed-literal "
        "ValueErrors, never a host path",
    ),
    "services/email_service.py::_send_via_env_smtp": (
        1,
        "already scrubs every embedded email address via _scrub(str(e)) before "
        "interpolating, per this module's own documented contract "
        "(reason = _scrub(str(e)); raise EmailDeliveryError(f'...{reason}') from e)",
    ),
    "services/search/model_switch.py::apply_embedding_model_switch": (
        1,
        "narrow re-wrap of our OWN ReindexDispatchError — 'The embedding model was "
        "switched, but the re-embed could not be queued: {e}' where e is itself the "
        "already-sanitized ReindexDispatchError from dispatch_reindex_for_every_owner "
        "(#891 exemplar, fixed in this same change)",
    ),
}

#: (expected_count, written reason) per ``"<path>::<function>"`` key, return-path.
_RETURN_ALLOWLIST: dict[str, tuple[int, str]] = {
    "api/endpoints/asr_settings.py::test_asr_connection": (
        1,
        "already calls _sanitize_message(str(exc), api_key) before returning it",
    ),
    "api/endpoints/files/__init__.py::_ready_frame": (
        1,
        "re-surfaces e.detail of an HTTPException WE raised ourselves two lines "
        "above (the 422 'Transcript is not ready yet'), not a caught library error",
    ),
    "api/endpoints/llm_settings.py::get_ollama_models": (
        1,
        "narrow aiohttp.ClientError against a caller-SUPPLIED base_url; the dial "
        "result IS the product this endpoint exists to report",
    ),
    "api/endpoints/llm_settings.py::get_openai_compatible_models": (
        1,
        "same: narrow aiohttp.ClientError against a caller-supplied base_url, the "
        "dial result IS the product",
    ),
    "api/endpoints/llm_settings.py::get_anthropic_models": (
        1,
        "same: narrow aiohttp.ClientError, the dial result IS the product",
    ),
    "api/endpoints/watch_sources.py::test_multipart_regex": (
        1,
        "narrow ValueError echoing the caller's OWN regex back — the entire point "
        "of a regex-test endpoint",
    ),
}


def test_the_scanner_actually_finds_call_sites() -> None:
    """A scanner that matches nothing is indistinguishable from a clean tree."""
    raise_by_key, return_by_key = _scan()
    assert len(raise_by_key) >= 18, (
        f"expected at least 18 distinct raise-path findings in the real tree, "
        f"found {len(raise_by_key)} — either the scanner regressed or the codebase "
        "genuinely eliminated most of them (update this floor deliberately if so)"
    )
    assert len(return_by_key) >= 5, (
        f"expected at least 5 distinct return-path findings under app/api/, "
        f"found {len(return_by_key)}"
    )


def test_no_unallowlisted_raise_path_exception_echo() -> None:
    raise_by_key, _ = _scan()
    unallowlisted = sorted(set(raise_by_key) - set(_RAISE_ALLOWLIST))
    assert not unallowlisted, (
        "These raise-path sites echo caught exception text into a response-bound "
        "construct with no allowlist entry. Rewrite with a fixed literal (see "
        "app/utils/error_handlers.py::ErrorHandler), or add a reasoned "
        "_RAISE_ALLOWLIST entry if the message is genuinely our own authored "
        "text:\n  " + "\n  ".join(unallowlisted)
    )


def test_a_raise_site_may_not_exceed_its_allowlisted_count() -> None:
    raise_by_key, _ = _scan()
    over: list[str] = []
    for key, findings in raise_by_key.items():
        expected = _RAISE_ALLOWLIST.get(key)
        if expected is not None and len(findings) > expected[0]:
            over.append(f"{key} (found {len(findings)}, allowlisted {expected[0]})")
    assert not over, (
        "These functions have MORE tainted raise sites than their allowlist entry "
        "accounts for — a new leak was added beside an already-allowlisted one:\n  "
        + "\n  ".join(over)
    )


def test_the_raise_allowlist_is_honest() -> None:
    raise_by_key, _ = _scan()
    stale: list[str] = []
    inflated: list[str] = []
    unexplained: list[str] = []
    for key, (expected_count, reason) in sorted(_RAISE_ALLOWLIST.items()):
        actual = len(raise_by_key.get(key, []))
        if actual == 0:
            stale.append(key)
        elif actual < expected_count:
            inflated.append(f"{key} (allowlisted {expected_count}, found {actual})")
        if not reason.strip():
            unexplained.append(key)
    assert not stale, f"allowlist entries point at functions with no finding at all: {stale}"
    assert not inflated, f"allowlist entries claim more findings than exist: {inflated}"
    assert not unexplained, f"allowlist entries need a written reason: {unexplained}"


def test_no_unallowlisted_return_path_exception_echo() -> None:
    _, return_by_key = _scan()
    unallowlisted = sorted(set(return_by_key) - set(_RETURN_ALLOWLIST))
    assert not unallowlisted, (
        "These return-path sites (under app/api/) echo caught exception text into "
        "a returned value with no allowlist entry:\n  " + "\n  ".join(unallowlisted)
    )


def test_a_return_site_may_not_exceed_its_allowlisted_count() -> None:
    _, return_by_key = _scan()
    over: list[str] = []
    for key, findings in return_by_key.items():
        expected = _RETURN_ALLOWLIST.get(key)
        if expected is not None and len(findings) > expected[0]:
            over.append(f"{key} (found {len(findings)}, allowlisted {expected[0]})")
    assert not over, (
        "These functions have MORE tainted return sites than their allowlist entry "
        "accounts for:\n  " + "\n  ".join(over)
    )


def test_the_return_allowlist_is_honest() -> None:
    _, return_by_key = _scan()
    stale: list[str] = []
    inflated: list[str] = []
    unexplained: list[str] = []
    for key, (expected_count, reason) in sorted(_RETURN_ALLOWLIST.items()):
        actual = len(return_by_key.get(key, []))
        if actual == 0:
            stale.append(key)
        elif actual < expected_count:
            inflated.append(f"{key} (allowlisted {expected_count}, found {actual})")
        if not reason.strip():
            unexplained.append(key)
    assert not stale, f"allowlist entries point at functions with no finding at all: {stale}"
    assert not inflated, f"allowlist entries claim more findings than exist: {inflated}"
    assert not unexplained, f"allowlist entries need a written reason: {unexplained}"


def test_the_scan_root_includes_the_service_layer() -> None:
    """The #891 acceptance criterion, made mechanical.

    If someone "simplifies" the raise-path scan root back down to app/api/, both
    the allowlist and the findings would lose every services/-prefixed key. This
    fails loudly the moment that happens, rather than quietly losing coverage.
    """
    raise_by_key, _ = _scan()
    service_keys_found = {k for k in raise_by_key if k.startswith("services/")}
    service_keys_allowlisted = {k for k in _RAISE_ALLOWLIST if k.startswith("services/")}

    # Concrete, not just non-empty: a real service-layer finding must be among
    # the live findings, and a real allowlisted service-layer site must survive
    # beside it — a bare-truthy check on the two sets would still pass if an
    # unrelated services/ finding replaced both of these.
    #
    # NOTE: the #891 exemplar itself, dispatch_reindex_for_every_owner, is
    # fixed so thoroughly it no longer appears in raise_by_key AT ALL — its
    # raised message references only `len(tasks)`/`len(ordered)`, neither of
    # which is ever assigned from the caught exception, so there is nothing
    # left to taint. apply_embedding_model_switch is the sibling that still
    # legitimately re-wraps that (now-sanitized) ReindexDispatchError's `e`.
    assert "services/search/model_switch.py::apply_embedding_model_switch" in service_keys_found, (
        "the model_switch.py re-wrap of ReindexDispatchError is no longer a live "
        "raise-path finding — either it regressed to interpolating something new, "
        "or the scan root was narrowed back to app/api/ (update this test "
        "deliberately only in the latter case)"
    )
    assert "services/email_service.py::_send_via_env_smtp" in service_keys_allowlisted, (
        "the raise-path allowlist lost its services/-prefixed entry for the "
        "already-scrubbed email-delivery raise — same #891 regression check as "
        "above, applied to the allowlist rather than the live findings"
    )
    assert service_keys_found, (
        "the raise-path scanner found NO services/-prefixed findings — either the "
        "scan root was narrowed back to app/api/ (the #891 regression this test "
        "exists to catch), or the service layer genuinely has zero response-bound "
        "exception-echo sites left (update this test deliberately if so)"
    )
    assert service_keys_allowlisted, (
        "the raise-path allowlist has no services/-prefixed entry — same regression "
        "check as above, applied to the allowlist rather than the live findings"
    )


def test_logging_is_not_a_finding() -> None:
    """A plain log call with no accompanying raise/return must never be a finding."""
    source = """
        def handler():
            try:
                do_thing()
            except Exception as e:
                logger.error(f"failed: {e}")
                logger.exception("failed")
    """
    import textwrap

    tree = ast.parse(textwrap.dedent(source))
    walker = _TaintWalker(frozenset({"OpenTranscribeError"}))
    walker.visit(tree)
    assert walker.raise_findings == []
    assert walker.return_findings == []
