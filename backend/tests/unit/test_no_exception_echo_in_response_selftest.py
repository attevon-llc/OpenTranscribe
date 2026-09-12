"""Guard the guard: every detector in test_no_exception_echo_in_response.py needs a
must-fire and a must-stay-clean case.

A detector that matches nothing reports zero findings, which is indistinguishable
from a clean tree — the exact failure mode issue #431 named in the other structural
auditors in this repo (each had detectors that silently matched nothing). Written as
source strings parsed with ``ast``, mirroring
``test_ws_event_quarantine_discipline_selftest.py``.
"""

from __future__ import annotations

import ast
import textwrap

from tests.unit.test_no_exception_echo_in_response import _APP_ROOT
from tests.unit.test_no_exception_echo_in_response import _collect_class_bases
from tests.unit.test_no_exception_echo_in_response import _ContainerTaintWalker
from tests.unit.test_no_exception_echo_in_response import _derive_error_subclasses
from tests.unit.test_no_exception_echo_in_response import _open_transcribe_error_subclasses_under
from tests.unit.test_no_exception_echo_in_response import _scan
from tests.unit.test_no_exception_echo_in_response import _scan_container_returns
from tests.unit.test_no_exception_echo_in_response import _TaintWalker

#: A minimal, synthetic OpenTranscribeError-shaped hierarchy for the must-fire cases
#: that need a real registered subclass. Kept separate from the app's real hierarchy
#: so these tests don't rot when a real exception class is renamed.
_SYNTHETIC_SUBCLASSES = frozenset({"OpenTranscribeError", "SearchIndexError", "StorageError"})


def _walk(source: str, error_subclasses: frozenset[str] = _SYNTHETIC_SUBCLASSES) -> _TaintWalker:
    tree = ast.parse(textwrap.dedent(source))
    walker = _TaintWalker(error_subclasses)
    walker.visit(tree)
    return walker


# --- Case 1: basic HTTPException(detail=f"...{e}...") ---

_BASIC_KEYWORD = """
    def handler():
        try:
            do_thing()
        except Exception as e:
            raise HTTPException(500, detail=f"boom: {e}")
"""


def test_basic_keyword_detail_must_fire() -> None:
    findings = _walk(_BASIC_KEYWORD).raise_findings
    assert findings == [("handler", 6)]


# --- Case 2: narrow except ValueError, str(e) via detail= ---

_NARROW_VALUE_ERROR = """
    def handler():
        try:
            do_thing()
        except ValueError as e:
            raise HTTPException(400, detail=str(e))
"""


def test_narrow_value_error_must_fire() -> None:
    findings = _walk(_NARROW_VALUE_ERROR).raise_findings
    assert findings == [("handler", 6)]


# --- Case 3: indirection through a plain local assignment ---

_SIMPLE_INDIRECTION = """
    def handler():
        try:
            do_thing()
        except Exception as e:
            msg = str(e)
            raise HTTPException(500, detail=msg)
"""


def test_simple_indirection_must_fire() -> None:
    findings = _walk(_SIMPLE_INDIRECTION).raise_findings
    assert findings == [("handler", 7)]


# --- Case 4: indirection THROUGH A CALL — the no-escape-hatch pin ---
#
# The investigation found a function literally named `create_user_friendly_error`
# (services/media_download_service.py) that is NOT a sanitizer — it only strips four
# literal prefixes on the non-auth-error branch. A name-based escape hatch for
# "looks like a cleaner" would have hidden exactly this false confidence, so the
# scanner is deliberately name-blind: it taints `safe` here purely because its value
# expression's AST subtree contains a reference to the tainted `e`, regardless of
# what `clean(...)` is called or does.

_CALL_INDIRECTION = """
    def handler():
        try:
            do_thing()
        except Exception as e:
            safe = clean(str(e))
            raise HTTPException(500, detail=safe)
"""


def test_call_indirection_must_fire_no_sanitizer_escape_hatch() -> None:
    findings = _walk(_CALL_INDIRECTION).raise_findings
    assert findings == [("handler", 7)]


# --- Case 5: ErrorHandler.<any_builder>(...) ---

_ERROR_HANDLER_BUILDER = """
    def handler():
        try:
            do_thing()
        except Exception as e:
            raise ErrorHandler.internal_error(f"failed: {e}")
"""


def test_error_handler_builder_must_fire() -> None:
    findings = _walk(_ERROR_HANDLER_BUILDER).raise_findings
    assert findings == [("handler", 6)]


# --- Case 6: a real OpenTranscribeError subclass ---

_SUBCLASS_RAISE = """
    def handler():
        try:
            do_thing()
        except Exception as e:
            raise StorageError(f"could not write: {e}")
"""


def test_subclass_raise_must_fire() -> None:
    findings = _walk(_SUBCLASS_RAISE).raise_findings
    assert findings == [("handler", 6)]


# --- Case 7: a raise of a subclass TWO levels down the hierarchy ---
# StorageError -> SearchIndexError -> OpenTranscribeError is two levels below the
# base — pins that the transitive derivation (not just direct subclasses) is what
# the raise-scanner is checking against.


def test_two_levels_deep_subclass_must_fire() -> None:
    findings = _walk(_SUBCLASS_RAISE, error_subclasses=_SYNTHETIC_SUBCLASSES).raise_findings
    assert findings == [("handler", 6)]
    # And StorageError really is two hops below the root in this synthetic hierarchy.
    hierarchy_source = """
        class OpenTranscribeError(Exception):
            pass

        class SearchIndexError(OpenTranscribeError):
            pass

        class StorageError(SearchIndexError):
            pass
    """
    tree = ast.parse(textwrap.dedent(hierarchy_source))
    base_map = _collect_class_bases(tree)
    assert base_map["StorageError"] == {"SearchIndexError"}
    assert base_map["SearchIndexError"] == {"OpenTranscribeError"}
    derived = _derive_error_subclasses(base_map)
    assert "StorageError" in derived


# --- Case 8: return-path echo ---

_RETURN_ECHO = """
    def handler():
        try:
            do_thing()
        except Exception as e:
            return {"success": False, "message": str(e)}
"""


def test_return_path_echo_must_fire() -> None:
    findings = _walk(_RETURN_ECHO).return_findings
    assert findings == [("handler", 6)]


# --- Case 9: positional (not keyword) detail argument ---

_POSITIONAL_DETAIL = """
    def handler():
        try:
            do_thing()
        except Exception as e:
            raise HTTPException(500, str(e))
"""


def test_positional_detail_must_fire() -> None:
    findings = _walk(_POSITIONAL_DETAIL).raise_findings
    assert findings == [("handler", 6)]


# --- Case 10a: nested-function attribution ---

_NESTED_FUNCTION = """
    def outer():
        try:
            do_thing()
        except Exception as e:
            def inner():
                raise HTTPException(500, detail=str(e))
            inner()
"""


def test_nested_function_attributes_to_the_innermost_function() -> None:
    findings = _walk(_NESTED_FUNCTION).raise_findings
    assert findings == [("inner", 7)]


# --- Case 10b: module-level attribution ---

_MODULE_LEVEL = """
    try:
        do_thing()
    except Exception as e:
        raise HTTPException(500, detail=str(e))
"""


def test_module_level_attributes_to_module() -> None:
    findings = _walk(_MODULE_LEVEL).raise_findings
    assert findings == [("<module>", 5)]


# --- Case 11: MUST STAY CLEAN — log then a clean re-raise ---
#
# The core case proving a log line plus a clean re-raise is not a finding: the real
# fix pattern this whole gate exists to require.

_LOG_THEN_CLEAN_RERAISE = """
    def handler():
        try:
            do_thing()
        except Exception as e:
            logger.exception(f"failed: {e}")
            raise ErrorHandler.internal_error("Could not do the thing.") from e
"""


def test_log_then_clean_reraise_must_stay_clean() -> None:
    walker = _walk(_LOG_THEN_CLEAN_RERAISE)
    assert walker.raise_findings == []
    assert walker.return_findings == []


# --- Case 12: MUST STAY CLEAN — except with no `as` binding ---

_NO_BINDING = """
    def handler():
        try:
            do_thing()
        except Exception:
            raise HTTPException(500, detail="Could not do the thing.")
"""


def test_no_exception_binding_must_stay_clean() -> None:
    assert _walk(_NO_BINDING).raise_findings == []


# --- Case 13: MUST STAY CLEAN — a local that never touched the exception ---

_UNRELATED_LOCAL = """
    def handler():
        other_local = "a fixed literal, never assigned from e"
        try:
            do_thing()
        except Exception as e:
            logger.exception(str(e))
            raise HTTPException(500, detail=other_local)
"""


def test_unrelated_local_must_stay_clean() -> None:
    assert _walk(_UNRELATED_LOCAL).raise_findings == []


# --- Case 14: MUST STAY CLEAN — not a response-bound construct ---
#
# Pins that the scanner doesn't over-fire on an arbitrary exception that is not
# HTTPException, not an ErrorHandler builder, and not an OpenTranscribeError subclass.

_NOT_RESPONSE_BOUND = """
    def handler():
        try:
            do_thing()
        except Exception as e:
            raise RuntimeError(f"provider failed: {e}")
"""


def test_non_response_bound_raise_must_stay_clean() -> None:
    assert _walk(_NOT_RESPONSE_BOUND).raise_findings == []


# --- Case 15: MUST STAY CLEAN for scanner 2's app/api/-only root ---
#
# The WALKER is root-agnostic (it doesn't know what file it is scanning); the
# app/api/-only scope for the return-path scanner is enforced by `_scan()`'s own
# iteration (`_iter_py_files(_API_ROOT)`), not by the walker. So this pins the
# property two ways: the walker itself WOULD flag this shape if asked to look at it
# (proving the detector is not silently broken), and the real `_scan()` output
# provably contains no services/-prefixed return-path key at all.


def test_a_services_module_return_echo_would_be_flagged_by_the_walker() -> None:
    """The walker's own detector fires on this shape — it is the ROOT that excludes it."""
    findings = _walk(_RETURN_ECHO).return_findings
    assert findings == [("handler", 6)]


def test_the_real_return_scan_never_touches_services() -> None:
    """`_scan()`'s return-path pass is rooted at app/api/ only (see its docstring)."""
    _, return_by_key = _scan()
    assert not any(k.startswith("services/") for k in return_by_key), (
        "the return-path scanner found a services/-prefixed key — its root was "
        "supposed to be app/api/ only; this is a #859/#891 scope regression"
    )


# --- Case 16a: MUST STAY CLEAN — type(e).__name__ alone carries no message text ---
#
# The 4a prune: this is the repo's own approved remedy for this class of finding
# (precedented at llm_context_window.py:249 / fs_events/detection.py:200), and a
# scanner that fires on its own prescribed fix teaches people to route around it.

_TYPE_NAME_ONLY = """
    def handler():
        try:
            do_thing()
        except Exception as e:
            return {"detail": f"failed ({type(e).__name__})"}
"""


def test_type_name_only_must_stay_clean() -> None:
    assert _walk(_TYPE_NAME_ONLY).return_findings == []


# --- Case 16b: MUST FIRE — type(e).__name__ ALONGSIDE the raw exception ---
#
# The case that stops the 4a prune becoming a bypass: a bare `{e}` sitting beside
# the class-name shape in the same f-string must still taint, because only the
# `type(...).__name__` subtree is structurally message-text-free.

_TYPE_NAME_PLUS_RAW = """
    def handler():
        try:
            do_thing()
        except Exception as e:
            return {"detail": f"failed ({type(e).__name__}): {e}"}
"""


def test_type_name_plus_raw_exception_must_still_fire() -> None:
    findings = _walk(_TYPE_NAME_PLUS_RAW).return_findings
    assert findings == [("handler", 6)]


# --- Case 17a: MUST FIRE — assigned in the handler, returned outside it ---
#
# The 4b fix: `_taint_stack` alone goes empty the instant the except suite closes,
# so a `return r` several lines below a handler that built `r` from `str(e)` was a
# scanner-invisible false negative. This is exactly the shape found live at
# services/directory_sync_service.py:407, services/backup_service.py:822/884/983,
# and services/media_mirror_engine.py:317 (among others).

_ASSIGN_IN_HANDLER_RETURN_OUTSIDE = """
    def handler():
        try:
            r = ok()
        except Exception as e:
            r = {"error": str(e)}
        return r
"""


def test_assign_in_handler_return_outside_must_fire() -> None:
    findings = _walk(_ASSIGN_IN_HANDLER_RETURN_OUTSIDE).return_findings
    assert findings == [("handler", 7)]


# --- Case 17b: MUST STAY CLEAN — same shape, no `as` binding ---

_ASSIGN_IN_HANDLER_RETURN_OUTSIDE_NO_BINDING = """
    def handler():
        try:
            r = ok()
        except Exception:
            r = {"error": "failed"}
        return r
"""


def test_assign_in_handler_return_outside_no_binding_must_stay_clean() -> None:
    assert _walk(_ASSIGN_IN_HANDLER_RETURN_OUTSIDE_NO_BINDING).return_findings == []


# =============================================================================
# Scanner 4 (container return-path). Every case below is a shape scanners 1-3 are
# structurally blind to: the taint never reaches the return EXPRESSION, only an
# object the return expression names.
# =============================================================================


def _walk_container(source: str) -> _ContainerTaintWalker:
    tree = ast.parse(textwrap.dedent(source))
    walker = _ContainerTaintWalker()
    walker.visit(tree)
    return walker


# --- Case 18: MUST FIRE — list.append(), the recover_all_stuck_tasks shape ---
#
# The live defect this scanner exists for: api/endpoints/tasks.py's
# recover_all_stuck_tasks did `retry_failures.append({"error": str(e)})` and returned
# `retry_failures` verbatim, and the original three scanners saw nothing.

_APPEND_THEN_RETURN = """
    def handler():
        failures = []
        for item in items:
            try:
                do_thing(item)
            except Exception as e:
                failures.append({"item": item, "error": str(e)})
        return failures
"""


def test_container_append_then_return_must_fire() -> None:
    assert _walk_container(_APPEND_THEN_RETURN).container_findings == [("handler", 8)]


def test_container_append_then_return_is_invisible_to_the_return_scanner() -> None:
    """The blind spot, stated as a test rather than as prose in the docstring."""
    assert _walk(_APPEND_THEN_RETURN).return_findings == []


# --- Case 19: MUST FIRE — subscript assignment into a returned dict ---
#
# The api/endpoints/speakers.py::_collect_index_debug_documents shape.

_SUBSCRIPT_ASSIGN_THEN_RETURN = """
    def handler():
        section = {}
        try:
            do_thing()
        except Exception as e:
            section["error"] = str(e)
        return section
"""


def test_container_subscript_assign_must_fire() -> None:
    assert _walk_container(_SUBSCRIPT_ASSIGN_THEN_RETURN).container_findings == [("handler", 7)]


# --- Case 20: MUST FIRE — the mutation is BELOW the return in source order ---
#
# gdpr_erasure_service.py::erase_user returns at :457 and appends at :498. A
# scanner that decided at visit_Return would report nothing here, which is why
# scanner 4 defers the check to function exit.

_MUTATION_BELOW_RETURN = """
    def handler():
        summary = {"errors": []}
        if early:
            return summary
        try:
            do_thing()
        except Exception as e:
            summary["errors"].append({"error": str(e)})
        return summary
"""


def test_container_mutation_below_the_return_must_fire() -> None:
    # Reported at the MUTATION (:9), which sits BELOW the first `return summary`
    # (:5) — a scanner deciding at visit_Return would have found nothing here.
    findings = _walk_container(_MUTATION_BELOW_RETURN).container_findings
    assert findings == [("handler", 9)]


# --- Case 21: MUST FIRE — nested container via `summary["errors"].append(...)` ---
#
# Pins _container_base_name: the mutated object is a subscript OF the returned
# local, so the finding must be attributed to `summary`, not to nothing.

_NESTED_CONTAINER = """
    def handler():
        summary = {"errors": []}
        try:
            do_thing()
        except Exception as e:
            summary["errors"].append(str(e))
        return summary
"""


def test_nested_container_attributes_to_the_returned_local_must_fire() -> None:
    assert _walk_container(_NESTED_CONTAINER).container_findings == [("handler", 7)]


# --- Case 22: MUST FIRE — `+=` into a returned accumulator ---

_AUGASSIGN_THEN_RETURN = """
    def handler():
        report = ""
        try:
            do_thing()
        except Exception as e:
            report += str(e)
        return report
"""


def test_container_augassign_must_fire() -> None:
    assert _walk_container(_AUGASSIGN_THEN_RETURN).container_findings == [("handler", 7)]


# --- Case 23: MUST STAY CLEAN — the prescribed fix ---
#
# type(e).__name__ plus logger.exception is what this gate asks for; a gate that
# still fired here is a gate people learn to route around.

_CONTAINER_SANITIZED = """
    def handler():
        failures = []
        for item in items:
            try:
                do_thing(item)
            except Exception as e:
                logger.exception("failed on %s", item)
                failures.append({"item": item, "error": f"failed ({type(e).__name__})"})
        return failures
"""


def test_container_sanitized_with_type_name_must_stay_clean() -> None:
    assert _walk_container(_CONTAINER_SANITIZED).container_findings == []


# --- Case 24: MUST STAY CLEAN — the container is never returned ---
#
# A per-item failure list consumed by control flow or logging only. Without this
# case the scanner would flag every best-effort loop in the codebase.

_CONTAINER_NOT_RETURNED = """
    def handler():
        failures = []
        for item in items:
            try:
                do_thing(item)
            except Exception as e:
                failures.append(str(e))
        if failures:
            logger.error("%d items failed", len(failures))
        return {"failed": len(failures)}
"""


def test_container_that_is_never_returned_must_stay_clean() -> None:
    """Also the must-stay-clean half of the ``len(...)`` prune.

    The return here DOES name ``failures``, but only inside ``len(...)`` — a shape that
    reduces to an int and cannot carry message text. This case is why the prune exists:
    it fired before the prune was added, on the very pattern the gate wants people to
    write instead of returning the container.
    """
    assert _walk_container(_CONTAINER_NOT_RETURNED).container_findings == []


# --- Case 24b: MUST FIRE — len() BESIDE the container itself ---
#
# The sibling that stops the len-prune becoming a bypass, exactly as case 16b does
# for type(e).__name__: only the `len(...)` subtree is text-free, so a bare
# reference to the same container elsewhere in the return must still fire.

_LEN_PLUS_RAW_CONTAINER = """
    def handler():
        failures = []
        for item in items:
            try:
                do_thing(item)
            except Exception as e:
                failures.append(str(e))
        return {"failed": len(failures), "detail": failures}
"""


def test_len_beside_the_raw_container_must_still_fire() -> None:
    assert _walk_container(_LEN_PLUS_RAW_CONTAINER).container_findings == [("handler", 8)]


# --- Case 25: MUST STAY CLEAN — an ordinary method call is not a mutation ---
#
# Pins _CONTAINER_MUTATORS: `logger.error(str(e))` writes into nothing, and a
# scanner that treated any attribute call as a container write would flag every
# handler in the tree.

_LOG_CALL_IS_NOT_A_MUTATION = """
    def handler():
        logger = get_logger()
        try:
            do_thing()
        except Exception as e:
            logger.error(str(e))
        return logger
"""


def test_a_plain_method_call_is_not_a_container_mutation() -> None:
    assert _walk_container(_LOG_CALL_IS_NOT_A_MUTATION).container_findings == []


# --- Case 26: MUST STAY CLEAN — no `as` binding, nothing to taint ---

_CONTAINER_NO_BINDING = """
    def handler():
        failures = []
        try:
            do_thing()
        except Exception:
            failures.append({"error": "failed"})
        return failures
"""


def test_container_without_an_exception_binding_must_stay_clean() -> None:
    assert _walk_container(_CONTAINER_NO_BINDING).container_findings == []


# --- Case 27: the real scan is rooted at app/api/ + app/services/ only ---


def test_the_container_scan_never_touches_tasks() -> None:
    """Scanner 4's root excludes app/tasks/ — see its module-docstring reason."""
    container_by_key = _scan_container_returns()
    assert not any(k.startswith("tasks/") for k in container_by_key), (
        "the container-return scanner found a tasks/-prefixed key — its root is "
        "app/api/ + app/services/ only, because a Celery task's return value reaches "
        "no HTTP response body here (test_a_celery_result_is_never_read_into_a_response)"
    )
    assert any(k.startswith("api/") for k in container_by_key) and any(
        k.startswith("services/") for k in container_by_key
    ), "the container-return scanner must cover BOTH of its roots, not just one"


def test_the_open_transcribe_error_set_is_transitive() -> None:
    """The dynamically-derived subclass set over the REAL app/ tree.

    Must contain at least one real class that is two-or-more levels below
    OpenTranscribeError (proving the derivation is transitive, not just
    direct-subclass), and must never contain an unrelated builtin.
    """
    derived = _open_transcribe_error_subclasses_under(_APP_ROOT)
    assert "OpenTranscribeError" in derived
    # ReindexDispatchError(SearchIndexError) -> SearchIndexError(OpenTranscribeError):
    # two hops below the root, in the REAL codebase.
    assert "ReindexDispatchError" in derived
    assert "SearchIndexError" in derived
    assert "RuntimeError" not in derived
