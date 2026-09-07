"""The propagation/repair plane must resolve a speaker's label with the SAME rule
the chunk-index writers use (issue #605).

Eight repair/propagation call sites (``rename_propagation_task.py``, ``speakers.py``,
``speaker_update.py``, ``speaker_matching_service.py``, ``speaker_clustering_service.py``)
computed the chunk plane's "current"/"old" name with an ad hoc ``display_name or
name`` chain that stopped agreeing with the write plane
(``app/tasks/search_indexing_task.py::resolve_chunk_speaker_name``,
``app/tasks/reindex_task.py::_resolve_reindex_speaker_name``) the day the writers
started trusting a confident LLM/embedding suggestion. A propagation task computing
the wrong "old name" narrows an ``update_by_query`` to a filter that matches
nothing — it logs ``status: success`` and the drift survives silently. This is the
exact shape observed on speaker 74070: ``name='SPEAKER_01'``, ``display_name=None``,
``suggested_name='Joe Rogan (Host)'``, ``confidence≈0.9`` — indexed as
``'Joe Rogan (Host)'`` by the write plane, but every repair site still computed
``'SPEAKER_01'``.

``app/utils/speaker_labels.py::canonical_speaker_label_for_row`` is now the ONE
resolver every repair/propagation call site imports, so this suite pins it against
the write plane directly rather than re-deriving the comparison per call site. The
dispatch-level (API/db-backed) consequences of the fix are covered in
``tests/api/test_rename_propagation_dispatch.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.tasks.reindex_task import _resolve_reindex_speaker_name
from app.tasks.search_indexing_task import resolve_chunk_speaker_name
from app.utils.speaker_labels import UNKNOWN_SPEAKER_LABEL
from app.utils.speaker_labels import canonical_speaker_label_for_row


def _speaker(**kwargs):
    defaults = dict(name="SPEAKER_01", display_name=None, suggested_name=None, confidence=None)
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_the_rename_propagation_plane_agrees_with_the_chunk_index_writer():
    """The exact shape of speaker 74070 (issue #605): a confident suggestion with
    no ``display_name`` at all. Red before the fix: ``'SPEAKER_01' != 'Joe Rogan
    (Host)'`` — every repair site computed the raw diarizer label while the
    indexer had already written the suggestion."""
    speaker = _speaker(name="SPEAKER_01", suggested_name="Joe Rogan (Host)", confidence=0.9)

    assert canonical_speaker_label_for_row(speaker) == resolve_chunk_speaker_name(speaker)
    assert canonical_speaker_label_for_row(speaker) == _resolve_reindex_speaker_name(speaker)
    assert canonical_speaker_label_for_row(speaker) == "Joe Rogan (Host)"


def test_a_suggestion_below_threshold_is_a_control_not_a_finding():
    """Control, must stay green both before AND after the fix. Without it, the
    test above would also pass for a resolver that blindly prefers ANY suggestion
    regardless of confidence, rather than the specific ``>= 0.75`` rule."""
    speaker = _speaker(name="SPEAKER_01", suggested_name="Joe Rogan (Host)", confidence=0.74)

    assert canonical_speaker_label_for_row(speaker) == "SPEAKER_01"
    assert canonical_speaker_label_for_row(speaker) == resolve_chunk_speaker_name(speaker)


def test_a_speaker_with_no_name_at_all_still_propagates():
    """Every field empty must resolve to ``UNKNOWN_SPEAKER_LABEL`` — a non-empty
    string — not the empty string that ``dispatch_speaker_rename``'s ``if not
    new_name: return 0`` guard would silently drop, dropping the rename instead
    of writing the label a reindex would actually produce."""
    speaker = _speaker(name=None, display_name=None, suggested_name=None, confidence=None)

    assert canonical_speaker_label_for_row(speaker) == UNKNOWN_SPEAKER_LABEL
    assert canonical_speaker_label_for_row(speaker) == resolve_chunk_speaker_name(speaker)
    assert canonical_speaker_label_for_row(speaker)  # non-empty — the dispatch guard's floor


def test_a_drifted_write_plane_would_be_caught_by_the_agreement_test_above(monkeypatch):
    """Guards the guard, mirroring ``test_canonical_speaker_label.py``'s own
    control: revert the WRITE plane's resolver in-process (restored by
    monkeypatch's own teardown — no source file is edited) and prove the
    agreement assertion above would have failed had the drift been real."""
    import app.tasks.search_indexing_task as sit

    def _old_ad_hoc_resolution(speaker):
        if speaker is None:
            return "Unknown"
        return str(speaker.display_name or speaker.name or "Unknown")

    monkeypatch.setattr(sit, "resolve_chunk_speaker_name", _old_ad_hoc_resolution)

    speaker = _speaker(name="SPEAKER_01", suggested_name="Joe Rogan (Host)", confidence=0.9)

    assert sit.resolve_chunk_speaker_name(speaker) == "SPEAKER_01"
    assert canonical_speaker_label_for_row(speaker) == "Joe Rogan (Host)"
    assert sit.resolve_chunk_speaker_name(speaker) != canonical_speaker_label_for_row(speaker), (
        "the drifted (old) write-plane resolution must disagree with the propagation "
        "plane's canonical resolver, proving the agreement test above is load-bearing "
        "rather than vacuous"
    )
