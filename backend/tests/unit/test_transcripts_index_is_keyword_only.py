"""The ``transcripts`` index has no vector plane, and must not pretend to (#542).

``search_transcripts`` took a ``use_semantic`` flag that defaulted to **True** and
built an ANN ``knn`` query from a sentence-transformers embedding. That query could
never have succeeded:

* ``transcripts.embedding`` is mapped ``knn_vector`` with **no ``method``**, so the
  field has no HNSW graph and OpenSearch rejects an ANN query against it with
  ``400 … Field 'embedding' is not built for ANN search`` — measured on the live
  cluster;
* no document carries an ``embedding`` at all: ``index_transcript`` omits the field
  whenever it is ``None``, and every caller passes ``None``.

The fallback was worse than the failure: on any embedding error it substituted a
**zero vector**, which ``cosinesimil`` rejects outright.

It survived because nothing called it — the parameter had zero callers anywhere in
the tree. That is exactly how a parameter that is accepted and silently does nothing
persists (#437's HTTP 200, #64's ignored ``enable_thinking: false``), so it was
removed rather than defaulted off.

The 400 it produced is not harmless, either: it carries the literal string
``search_phase_execution_exception``, which ``_is_index_corruption_error`` matches.
The first version of the #540 kNN probe classified this index as **corrupt** because
of it, and would have rebuilt an intact index on every health tick.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

TRANSCRIPTS_PY = (
    Path(__file__).resolve().parents[2]
    / "app"
    / "services"
    / "opensearch_service"
    / "transcripts.py"
)


def test_the_module_builds_no_knn_query() -> None:
    """No ANN query may be constructed against this index, by any path.

    Asserted over the source so a future reader cannot reintroduce the shape behind a
    different flag name. The failure mode is silent: OpenSearch answers 400 and the
    call site sees an exception it will most likely log and swallow.
    """
    source = TRANSCRIPTS_PY.read_text()

    assert '"knn"' not in source, "this index has no HNSW graph; an ANN query 400s"
    assert "_get_sentence_transformer" not in source, (
        "no query embedding is needed for a keyword-only index"
    )


def test_no_zero_vector_fallback_remains() -> None:
    """``cosinesimil`` rejects a zero vector outright.

    The removed fallback substituted one on any embedding failure, so the degraded
    path was guaranteed to fail even on an index that *could* serve ANN queries.
    """
    source = TRANSCRIPTS_PY.read_text()

    assert "SENTENCE_TRANSFORMER_DIMENSION" not in source
    assert "[0.0] *" not in source


def test_index_transcript_still_accepts_an_embedding_but_no_caller_supplies_one() -> None:
    """The writer keeps its optional parameter; the point is that nothing fills it.

    Documented rather than removed: the parameter is harmless (the field is simply
    omitted when ``None``), and removing it would change a signature the Protocol
    also declares. The vestigial mapping should go on the next index version bump.
    """
    from app.services.opensearch_service import index_transcript

    assert "embedding" in inspect.signature(index_transcript).parameters

    app_root = TRANSCRIPTS_PY.parents[2]  # the `app` package, NOT backend/ (venv, mutants)
    callers_passing_embedding: list[str] = []
    for path in sorted(app_root.rglob("*.py")):
        if path == TRANSCRIPTS_PY:
            continue
        # No try/except: every file under `app/` must be valid UTF-8 Python. Swallowing
        # a parse error here would silently shrink the set being searched, which is how
        # a sweep reports "no callers" for a tree it never finished reading.
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "index_transcript"
                and any(kw.arg == "embedding" for kw in node.keywords)
            ):
                callers_passing_embedding.append(str(path.relative_to(app_root)))

    assert not callers_passing_embedding, (
        "a caller now supplies an embedding — the transcripts index would need a real "
        "ANN mapping before that means anything: " + ", ".join(callers_passing_embedding)
    )


def test_the_module_exposes_no_search_function_at_all() -> None:
    """This module writes; it does not search (#542).

    The deleted ``search_transcripts`` had zero callers and a semantic branch the
    mapping could not serve, and it hid behind a live, working function of the same
    name in ``api/endpoints/search.py``. Reintroducing a searcher here would recreate
    both problems at once — a second implementation of a capability
    ``files/filtering.py`` already provides, under a name that greps to the wrong file.
    """
    import app.services.opensearch_service as package
    from app.services.opensearch_service import transcripts

    public = {n for n in vars(transcripts) if not n.startswith("_")}
    searchers = {n for n in public if "search" in n.lower()}

    assert not searchers, f"this module must not search: {sorted(searchers)}"
    assert not hasattr(package, "search_transcripts"), (
        "the package re-export came back; nothing consumed it and it shadowed the real "
        "endpoint's name"
    )


def test_the_search_service_protocol_promises_only_what_exists() -> None:
    """A Protocol member no implementation honours advertises a phantom capability."""
    from app.services.interfaces import SearchService

    members = {n for n in vars(SearchService) if not n.startswith("_")}

    assert "search_transcripts" not in members
    assert {"index_transcript", "remove_speaker_embedding"} <= members
