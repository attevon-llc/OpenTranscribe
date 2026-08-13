"""Run construction: what the harness records about each retrieved document.

The metric is computed from ``RunDoc``s, and two of their fields are not
decoration. ``doc_id`` must be the id the index actually holds, or a run cannot
be joined to qrels or replayed; ``doc_type`` is what the tie-break sorts on, and
tie-break order is the mechanism by which the Stage 3 gate could have been passed
by document *naming* alone (``'d'`` outsorts every digit, and trec_eval breaks
ties by docid descending).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.ingest_artifacts.index_mapping import digest_document_id
from tests.eval.harness.runner import _to_run_docs

FILE = "3f2a9c10-0000-0000-0000-000000000000"


@dataclass
class _Hit:
    file_uuid: str
    chunk_index: int
    score: float = 0.5


def test_a_chunk_hit_keeps_the_chunk_id_scheme() -> None:
    (doc,) = _to_run_docs([_Hit(FILE, 7)])
    assert doc.doc_id == f"{FILE}_7"
    assert doc.doc_type == "chunk"
    assert doc.chunk_index == 7


def test_a_digest_hit_is_labelled_and_named_the_way_the_index_holds_it() -> None:
    """The sentinel is the only signal available: hits carry no ``doc_type``.

    Naming it ``{uuid}_-1`` would be an id no document has, so the run could not
    be joined to anything; labelling it ``chunk`` would let it win a tie against
    a real chunk on sort order it has no claim to.
    """
    (doc,) = _to_run_docs([_Hit(FILE, -1)])
    assert doc.doc_id == digest_document_id(FILE, 0), "section 0's sentinel is -1"
    assert doc.doc_type == "digest"
    assert _to_run_docs([_Hit(FILE, -3)])[0].doc_id == digest_document_id(FILE, 2)


def test_the_tie_break_puts_a_chunk_ahead_of_a_digest_at_equal_score() -> None:
    """Ascending ``doc_type`` — 'chunk' < 'digest' — and it must not depend on the id."""
    chunk, digest = _to_run_docs([_Hit(FILE, 0), _Hit(FILE, -1)])
    assert chunk.tie_key() < digest.tie_key()
