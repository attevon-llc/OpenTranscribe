"""Every place that turns a speaker into a printed name asks the voiceprint switch (#1280).

A speaker's name can have been written by a voiceprint (``Speaker.identity_source``), and while
the file's voiceprints are off that name must not reach an index, an export, a prompt, a roster
or a response. The label functions take the file's decision (``voiceprint_names=``) or a
session / ``PrivacyView`` to ask it through. A call that passes neither silently prints the
unredacted name, so this test fails on one that is not in ``_ALLOWED`` with a written reason.

Two rules, both by AST over ``backend/app``:

1. every call of a label function passes ``voiceprint_names=`` (or, for the row form, a session
   or ``PrivacyView`` as its second argument);
2. a query that reads ``Speaker.display_name`` as a column also reads
   ``Speaker.identity_source``: without it the row cannot say who wrote the name, and the label
   function refuses it (``TypeError``) the first time the switch is off.

The detectors have a must-fire and a must-stay-clean case each (a detector that matches nothing
reports a clean tree).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2] / "app"

#: Label functions that need the file's decision. ``canonical_speaker_label_for_row`` also
#: accepts it as a second positional argument (a session or a ``PrivacyView``).
LABEL_FUNCTIONS = frozenset(
    {
        "canonical_speaker_label",
        "canonical_speaker_label_for_row",
        "get_speaker_name",
        "resolve_chunk_speaker_name",
        "_resolve_reindex_speaker_name",
        "speaker_namer",
        "compute_speaker_stats",
        "build_transcript_and_stats",
        "build_full_transcript",
        "build_speaker_segments",
    }
)

#: ``<file under app/>::<enclosing function>`` -> why the call is policy-correct without the
#: argument. An entry nothing matches fails the run (delete it); a new call site needs a reason,
#: never a bare entry.
_ALLOWED: dict[str, str] = {
    # --- before/after label captures of a rename or a link change -------------------------
    # The tracker compares what the index holds with what the index will hold. Both labels are
    # the policy-aware ones (the row is an ORM instance with its own session), and the writer
    # sets `identity_source` BEFORE the "after" label is taken.
    "api/endpoints/files/reprocess.py::clear_selective_data": "tracker before/after capture",
    "api/endpoints/speaker_update.py::_apply_high_confidence_match": "tracker before/after capture",
    "api/endpoints/speaker_update.py::_persist_match_results": "tracker before/after capture",
    "api/endpoints/speakers.py::_reject_speaker_suggestion": "tracker before/after capture",
    "api/endpoints/speakers.py::update_speaker": "tracker before/after capture",
    "scripts/batch_speaker_matching.py::batch_process_speaker_matches": "tracker capture",
    "services/speaker_cleanup.py::join_cluster_identity": "tracker before/after capture",
    "services/speaker_cleanup.py::unlink_from_profile": "tracker before/after capture",
    "services/speaker_clustering_service.py::batch_verify_speakers": "tracker capture",
    "services/speaker_clustering_service.py::promote_cluster_to_profile": "tracker capture",
    "services/speaker_matching_service.py::_handle_no_speaker_match": "tracker capture",
    "services/speaker_matching_service.py::_handle_speaker_match": "tracker capture",
    "services/speaker_matching_service.py::assign_speaker_to_profile": "tracker capture",
    "services/speaker_profile_rename.py::apply_profile_name_to_speakers": "tracker capture",
    "tasks/speaker_identification_task.py::_store_speaker_predictions": "tracker capture",
    # --- labels computed from rows an endpoint already redacted -----------------------------
    "services/formatting_service.py::format_transcript_segment": (
        "reads the segment's serialized speaker, which the endpoint redacted "
        "(redact_response_speakers) before validating it"
    ),
    "api/endpoints/files/crud.py::_resolve_segment_speaker_name": (
        "called with rows the endpoint redacted first (redact_response_speakers)"
    ),
    # --- the raw diarizer label is not a display name -------------------------------------
    "tasks/speaker_identification_task.py::_load_identification_inputs": (
        "build_full_transcript / build_speaker_segments label speakers with Speaker.name, the "
        "diarizer label nothing rewrites; no voiceprint decision exists to make"
    ),
}


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _enclosing(tree: ast.AST) -> dict[int, str]:
    """``id(call node)`` -> name of the innermost enclosing function (or ``<module>``)."""
    owner: dict[int, str] = {}

    def visit(node: ast.AST, current: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = (
                child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else current
            )
            if isinstance(child, ast.Call):
                owner[id(child)] = current
            visit(child, name)

    visit(tree, "<module>")
    return owner


def _passes_the_decision(call: ast.Call, name: str) -> bool:
    if any(kw.arg == "voiceprint_names" for kw in call.keywords):
        return True
    if name == "canonical_speaker_label_for_row" and len(call.args) >= 2:
        return True  # a session or PrivacyView to ask the policy through
    if name == "canonical_speaker_label":
        # The pure function with nothing to hide: `canonical_speaker_label(None)` or a bare
        # diarizer name, no display name / suggestion keywords.
        return not call.keywords
    return False


def undecided_label_calls(source: str, relpath: str) -> list[str]:
    """``<relpath>::<function>`` of every label call that carries no decision."""
    tree = ast.parse(source)
    owner = _enclosing(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _called_name(node)
        if name in LABEL_FUNCTIONS and not _passes_the_decision(node, name):
            found.append(f"{relpath}::{owner.get(id(node), '<module>')}")
    return sorted(set(found))


def _speaker_columns(call: ast.Call) -> set[str]:
    return {
        arg.attr
        for arg in call.args
        if isinstance(arg, ast.Attribute)
        and isinstance(arg.value, ast.Name)
        and arg.value.id == "Speaker"
    }


def name_queries_without_provenance(source: str, relpath: str) -> list[str]:
    """``<relpath>::<function>`` of every query that selects the name but not its provenance."""
    tree = ast.parse(source)
    owner = _enclosing(tree)
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _called_name(node) in {"query", "select"}):
            continue
        columns = _speaker_columns(node)
        if "display_name" in columns and "identity_source" not in columns:
            found.append(f"{relpath}::{owner.get(id(node), '<module>')}")
    return sorted(set(found))


def _scan(detector) -> dict[str, str]:
    hits: dict[str, str] = {}
    for path in sorted(APP.rglob("*.py")):
        relpath = path.relative_to(APP).as_posix()
        for key in detector(path.read_text(encoding="utf-8"), relpath):
            hits[key] = relpath
    return hits


# --- the detectors fire and stay quiet where they should ---------------------------------


def test_the_call_detector_fires_on_a_label_call_with_no_decision():
    source = "def f(row):\n    return get_speaker_name(row)\n"

    assert undecided_label_calls(source, "x.py") == ["x.py::f"]


@pytest.mark.parametrize(
    "call",
    [
        "get_speaker_name(row, voiceprint_names=decided)",
        "canonical_speaker_label_for_row(row, view)",
        "canonical_speaker_label(None)",
        "speaker_namer(speakers, 'x', voiceprint_names=decided)",
    ],
)
def test_the_call_detector_stays_quiet_when_the_decision_is_passed(call):
    assert undecided_label_calls(f"def f(row):\n    return {call}\n", "x.py") == []


def test_the_pure_label_with_a_name_to_hide_needs_a_decision():
    source = "def f(s):\n    return canonical_speaker_label(s.name, display_name=s.display_name)\n"

    assert undecided_label_calls(source, "x.py") == ["x.py::f"]


def test_the_query_detector_fires_on_a_name_without_its_provenance():
    source = "def f(db):\n    return db.query(Speaker.id, Speaker.name, Speaker.display_name)\n"

    assert name_queries_without_provenance(source, "x.py") == ["x.py::f"]


def test_the_query_detector_stays_quiet_when_the_provenance_is_selected():
    source = (
        "def f(db):\n"
        "    return db.query(Speaker.id, Speaker.display_name, Speaker.identity_source)\n"
    )

    assert name_queries_without_provenance(source, "x.py") == []
    assert name_queries_without_provenance("def f(db):\n    db.query(Speaker.id)\n", "x.py") == []


# --- the tree ----------------------------------------------------------------------------


def test_every_label_call_in_the_app_carries_the_file_decision():
    hits = _scan(undecided_label_calls)

    assert hits, "the scan found no label calls at all: the detector is not looking"
    missing = sorted(k for k in hits if k not in _ALLOWED)
    assert not missing, f"label calls with no voiceprint decision and no written reason: {missing}"


def test_no_allowlisted_call_site_is_stale():
    hits = set(_scan(undecided_label_calls))

    assert sorted(k for k in _ALLOWED if k not in hits) == []


#: Queries that read `display_name` without `identity_source`, each with the reason the name
#: never reaches a surface the switch governs.
QUERY_ALLOWED: dict[str, str] = {
    "api/endpoints/speaker_update.py::trigger_retroactive_matching": (
        "a voiceprint stage: it runs only while the gate allows voiceprints, and the name is "
        "only compared against the trigger's, never printed"
    ),
    "api/endpoints/speakers.py::_load_profile_speaker_names": (
        "feeds the speaker-document sync (voiceprint index), whose documents the purge deletes "
        "and whose reads are closed while voiceprints are off"
    ),
    "services/speaker_cleanup.py::_resync_speaker_docs": (
        "same speaker-document sync: the voiceprint index is erased by the purge, not hidden"
    ),
    "utils/transcript_comparison.py::export_baseline": (
        "developer regression snapshot of the stored rows, written to local disk by "
        "`baseline_export_task`; not a user surface"
    ),
    "utils/transcript_comparison.py::export_word_reference": (
        "developer regression reference for diarization comparison; not a user surface"
    ),
}


def test_every_query_that_reads_a_speaker_name_reads_its_provenance():
    hits = _scan(name_queries_without_provenance)

    missing = sorted(k for k in hits if k not in QUERY_ALLOWED)
    assert not missing, f"queries that select display_name without identity_source: {missing}"


def test_no_allowlisted_query_is_stale():
    hits = set(_scan(name_queries_without_provenance))

    assert sorted(k for k in QUERY_ALLOWED if k not in hits) == []
