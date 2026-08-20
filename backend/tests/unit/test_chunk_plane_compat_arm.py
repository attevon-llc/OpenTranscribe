"""Every reader of the chunks index must decide about ``doc_type`` (#403 Stage 3).

Index v6 put a second kind of document into ``transcript_chunks``. From that moment
every query against it is either "the chunk plane" or "all planes", and getting it
wrong is silent in both directions:

* a reader that forgets the discriminator counts digests as chunks — facet counts
  skew, autocomplete offers derived text, and a file left with only digests by a
  half-failed rebuild reads as *indexed*, so auto-repair never fires (addendum G4);
* a rewrite that *adds* the discriminator stops reaching digests — a share
  revocation leaves a readable digest behind, which is a permission leak, not a
  relevance bug (addendum G5).

And the arm has to be :func:`chunk_plane_clause`, never a bare
``{"term": {"doc_type": "chunk"}}``: **every chunk indexed before v6 carries no
``doc_type`` at all**, so the bare term matches none of them and the #400 prune
count returns 0 for an entire installed corpus. An explicit keyword mapping does
nothing for documents written before it existed.

The sweep is over the source rather than over a list of known call sites, because
the failure this guards is a *new* reader added later by someone who did not read
G3. Every deliberate exception is an allowlist entry with a written reason; an
entry whose function no longer matches fails the test, so the list can only shrink.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[3] / "backend" / "app"

#: Client methods that read or rewrite documents.
_OPENSEARCH_CALLS = {"search", "msearch", "count", "delete_by_query", "update_by_query"}

#: Anything that proves the author decided. The plane-query builders carry the
#: clause themselves, so naming one is as good as naming the clause.
_DECIDED = (
    "chunk_plane_clause",
    "chunk_plane_query",
    "digest_plane_query",
    "file_plane_query",
)

#: ``<module>::<function>`` -> why this reader must NOT carry the chunk-plane arm.
#: G5 is the load-bearing half of this list: these are the paths that keep a
#: digest's permissions and tenancy correct, and filtering them is the leak.
_ALLOWED: dict[str, str] = {
    "tasks/search_indexing_task.py::update_file_access_index": (
        "G5: the ACL rewrite keys on file_id and MUST reach digest documents. "
        "A digest excluded here keeps the ACL it was last stamped with."
    ),
    "tasks/search_indexing_task.py::update_file_tags_index": (
        "G5, same shape: tags are denormalised onto every document of the file, "
        "digests included, or a tag-scoped chat query silently skips them."
    ),
    "tasks/search_indexing_task.py::update_document_access_index": (
        "#T10: the document-plane sibling of update_file_access_index. Uses "
        "_document_plane_clause (a document's own chunks only), which is not one of "
        "the DECIDED markers above because it lives in this module, not "
        "indexing_service.py — same reasoning as update_file_access_index just above, "
        "mirrored for the other plane."
    ),
    "tasks/tenant_backfill_task.py::_backfill_transcript_chunks": (
        "G5: the tenant stamp keys on file_uuid and must reach every plane, or a "
        "digest stays personal-scope inside an organization."
    ),
    "tasks/tenant_backfill_task.py::_update_by_query": (
        "The generic helper the backfill above drives; same reason."
    ),
    "tasks/opensearch_integrity_task.py::_cleanup_index_by_field": (
        "A whole-file orphan sweep across several indices. Its unit of work is the "
        "file, not the plane — a chunk-only delete would strand the digests."
    ),
    "tasks/opensearch_integrity_task.py::get_index_overview": (
        "Reports index SIZE for the admin panel. Every document counts, including "
        "digests; filtering would under-report what the cluster is holding."
    ),
    "api/endpoints/search.py::_probe_index_health": (
        "A liveness probe — match_all/size 0 against each of eight indices, asking "
        "whether the index answers at all. It selects no documents to act on, and "
        "seven of the eight have no planes. Extracted from repair_indices, which "
        "held this same exemption before the session-lifetime split."
    ),
    "services/search/hybrid_search_service.py::count_matches": (
        "Takes its filter list as a parameter from _build_filters, which carries "
        "the arm. Building a second one here is what G3 warns against."
    ),
    "services/search/hybrid_search_service.py::_search_with_two_phase": (
        "Same: the filters are passed in, already armed by _build_filters."
    ),
}


def _module_key(path: Path) -> str:
    return path.relative_to(APP).as_posix()


def _functions_touching_the_chunks_index() -> list[tuple[str, str, bool]]:
    """``(key, source, decided)`` for every function that queries the chunks index."""
    results: list[tuple[str, str, bool]] = []
    for path in sorted(APP.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "OPENSEARCH_CHUNKS_INDEX" not in source:
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            segment = ast.get_source_segment(source, node) or ""
            calls = {
                call.func.attr
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr in _OPENSEARCH_CALLS
            }
            if not calls:
                continue
            # A call that builds no predicate of its own inherits one.
            if '"query"' not in segment and '"filter"' not in segment:
                continue
            key = f"{_module_key(path)}::{node.name}"
            results.append((key, segment, any(marker in segment for marker in _DECIDED)))
    return results


def test_the_sweep_finds_the_readers_it_is_meant_to_check() -> None:
    """Guard on the guard: a sweep that matches nothing passes everything."""
    keys = {key for key, _, _ in _functions_touching_the_chunks_index()}
    for expected in (
        "services/search/indexing_service.py::delete_transcript_chunks",
        "services/search/hybrid_search_service.py::get_available_filters",
        "services/search/hybrid_search_service.py::get_suggestions",
        "tasks/reindex_task.py::_cleanup_orphaned_chunks",
        "tasks/search_indexing_task.py::update_file_access_index",
    ):
        assert expected in keys, f"the sweep no longer reaches {expected} — it is broken"


def test_every_chunks_index_reader_decides_about_doc_type() -> None:
    undecided = [key for key, _, decided in _functions_touching_the_chunks_index() if not decided]
    unexplained = [key for key in undecided if key not in _ALLOWED]
    assert not unexplained, (
        "These query the chunks index without deciding which plane they mean. Add the "
        "compat-armed chunk_plane_clause(), or add an allowlist entry saying why the "
        "reader must see every plane (addendum G3/G4/G5): " + ", ".join(sorted(unexplained))
    )


def test_the_allowlist_cannot_outlive_its_subjects() -> None:
    """A stale exemption is an exemption nobody re-examined."""
    findings = {key for key, _, decided in _functions_touching_the_chunks_index() if not decided}
    stale = sorted(set(_ALLOWED) - findings)
    assert not stale, (
        "These allowlist entries no longer match an undecided reader — the function was "
        "renamed, deleted, or has since been armed. Delete the line: " + ", ".join(stale)
    )


def test_no_reader_uses_a_bare_doc_type_term_instead_of_the_compat_arm() -> None:
    """The one mistake that looks correct and breaks the whole installed corpus."""
    offenders: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        if "index_mapping" in path.as_posix():
            continue  # the module that DEFINES the clause
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # A dict *literal* in code, not the same characters inside a docstring
            # warning against it — `chunk_plane_query`'s own docstring names the
            # mistake, and a string search reported that as the mistake.
            if not isinstance(node, ast.Dict):
                continue
            entries = {
                key.value: getattr(value, "value", None)
                for key, value in zip(node.keys, node.values, strict=False)
                if isinstance(key, ast.Constant)
            }
            if entries.get("doc_type") == "chunk":
                offenders.append(f"{_module_key(path)}:{node.lineno}")
    assert not offenders, (
        "A bare doc_type term matches no document written before v6, so it silently "
        "excludes the entire installed corpus. Use chunk_plane_clause(): " + ", ".join(offenders)
    )


@pytest.mark.parametrize(
    ("builder", "expects_compat"),
    [("chunk_plane_query", True), ("digest_plane_query", False), ("file_plane_query", False)],
)
def test_the_plane_builders_agree_on_what_a_legacy_document_is(
    builder: str, expects_compat: bool
) -> None:
    """A legacy chunk (no ``doc_type``) belongs to the chunk plane and nowhere else."""
    from app.services.search import indexing_service

    body = getattr(indexing_service, builder)("some-uuid")
    rendered = repr(body)
    has_compat = "must_not" in rendered and "doc_type" in rendered
    assert has_compat is expects_compat, (
        f"{builder} {'lost' if expects_compat else 'grew'} the legacy-document arm"
    )
    if builder == "file_plane_query":
        assert "doc_type" not in rendered, (
            "file_plane_query must match every plane — it is what makes a file delete "
            "and a full rebuild leave nothing behind"
        )
