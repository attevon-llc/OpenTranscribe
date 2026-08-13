"""A GDPR erasure is verified across **both** planes of the chunks index (#403).

``file_cleanup_service._erase_transcript_chunks`` deletes a file's documents from
``transcript_chunks`` and then counts what survived, because
``delete_transcript_chunks`` returns ``0`` for "no chunks", "index absent" and "the
delete failed" alike — the count is the only evidence the erasure is complete.

Since index v6 that index holds two kinds of document under a ``doc_type``
discriminator, and a digest section is **verbatim transcript text**. So a survivor
count restricted to the chunk plane would report a clean sweep while the digest of
an erased recording stayed indexed, searchable and retrievable: the erasure audits
as complete while the recording's own words survive. That failure has already
shipped once in this repo one file over (``security(gdpr): erasure reported SUCCESS
while transcript text survived``), which is why it is pinned here rather than left
to the AST sweep in ``test_chunk_plane_compat_arm.py`` — that sweep can see that a
decision was *made*, never that it was the right one.

The OpenSearch stand-in evaluates the query it is handed against a small document
set, so the assertions are about which documents are found, not about which body was
sent. ``test_the_stand_in_can_tell_the_planes_apart`` is the control that makes that
worth anything: it proves the evaluator distinguishes the chunk-plane predicate from
the file-plane one, so a regression to the former really would turn these red.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.ingest_artifacts import index_mapping as digest_mapping
from app.services.search import indexing_service as svc

FILE_UUID = "11111111-2222-3333-4444-555555555555"
OTHER_UUID = "99999999-8888-7777-6666-555555555555"


# ---------------------------------------------------------------------------
# A stand-in that really evaluates the predicate
# ---------------------------------------------------------------------------


def _matches(doc: dict[str, Any], clause: dict[str, Any]) -> bool:
    """Evaluate the small subset of query DSL the plane builders emit."""
    if "term" in clause:
        ((field, value),) = clause["term"].items()
        return bool(doc.get(field) == value)
    if "exists" in clause:
        return clause["exists"]["field"] in doc
    if "range" in clause:
        ((field, bounds),) = clause["range"].items()
        return field in doc and doc[field] >= bounds["gte"]
    if "bool" in clause:
        node = clause["bool"]
        if any(not _matches(doc, sub) for sub in node.get("filter", [])):
            return False
        must_not = node.get("must_not")
        if must_not is not None:
            subs = must_not if isinstance(must_not, list) else [must_not]
            if any(_matches(doc, sub) for sub in subs):
                return False
        should = node.get("should")
        if should:
            hits = sum(1 for sub in should if _matches(doc, sub))
            if hits < node.get("minimum_should_match", 1):
                return False
        return True
    raise AssertionError(f"the stand-in cannot evaluate {clause!r} — teach it, don't skip it")


class _FakeIndices:
    def __init__(self, store: _FakeOpenSearch) -> None:
        self._store = store
        self.refreshes = 0

    def exists(self, index: str) -> bool:  # noqa: ARG002 - one index in this harness
        return self._store.index_present

    def refresh(self, index: str) -> None:  # noqa: ARG002
        self.refreshes += 1


class _FakeOpenSearch:
    """Holds documents and answers ``count``/``delete_by_query`` by evaluating them.

    ``delete_scope`` is what a *broken* delete looks like: pass the chunk-plane
    predicate and the digests are stranded, which is the state the count exists to
    catch.
    """

    def __init__(
        self,
        docs: list[dict[str, Any]],
        *,
        delete_scope: Any = None,
        index_present: bool = True,
        count_raises: Exception | None = None,
    ) -> None:
        self.docs = list(docs)
        self.index_present = index_present
        self._delete_scope = delete_scope
        self._count_raises = count_raises
        self.indices = _FakeIndices(self)
        self.cache_bumps = 0

    def __bool__(self) -> bool:
        return True

    def record_cache_bump(self) -> None:
        """Stands in for ``_invalidate_chat_retrieval_cache``, which would want Redis."""
        self.cache_bumps += 1

    def delete_by_query(self, index: str, body: dict[str, Any], **_: Any) -> dict[str, int]:  # noqa: ARG002
        scope = self._delete_scope(FILE_UUID) if self._delete_scope else body["query"]
        doomed = [doc for doc in self.docs if _matches(doc, scope)]
        self.docs = [doc for doc in self.docs if doc not in doomed]
        return {"deleted": len(doomed)}

    def count(self, index: str, body: dict[str, Any]) -> dict[str, int]:  # noqa: ARG002
        if self._count_raises is not None:
            raise self._count_raises
        return {"count": sum(1 for doc in self.docs if _matches(doc, body["query"]))}


def _chunk(file_uuid: str = FILE_UUID, *, legacy: bool = False) -> dict[str, Any]:
    doc = {"file_uuid": file_uuid, "chunk_index": 0, "content": "what was said"}
    if not legacy:
        doc["doc_type"] = digest_mapping.DOC_TYPE_CHUNK
    return doc


def _digest(file_uuid: str = FILE_UUID) -> dict[str, Any]:
    return {
        "file_uuid": file_uuid,
        "doc_type": digest_mapping.DOC_TYPE_DIGEST,
        "digest_section": 0,
        "chunk_index": digest_mapping.digest_chunk_index(0),
        "content": "a verbatim sentence from the recording",
    }


@pytest.fixture
def cluster(monkeypatch):
    """Install a stand-in cluster in place of the module-global client, and hand it back."""

    def _install(fake: _FakeOpenSearch) -> _FakeOpenSearch:
        monkeypatch.setattr(svc, "opensearch_client", fake)
        # Contained in production (its failure must never fail indexing) and it
        # would reach for Redis here; counted so the invalidation is still asserted.
        monkeypatch.setattr(svc, "_invalidate_chat_retrieval_cache", fake.record_cache_bump)
        return fake

    return _install


def _erase() -> list[tuple[str, str]]:
    """Run the erasure step under test, collecting whatever it could not prove gone."""
    from app.services.file_cleanup_service import _erase_transcript_chunks

    residual: list[tuple[str, str]] = []
    _erase_transcript_chunks(FILE_UUID, lambda stage, err: residual.append((stage, str(err))))
    return residual


# ---------------------------------------------------------------------------
# The control: the evaluator can tell the planes apart
# ---------------------------------------------------------------------------


def test_the_stand_in_can_tell_the_planes_apart() -> None:
    """Without this, every assertion below could pass on an evaluator that matches all."""
    digest, chunk, legacy = _digest(), _chunk(), _chunk(legacy=True)

    file_plane = svc.file_plane_query(FILE_UUID)
    chunk_plane = svc.chunk_plane_query(FILE_UUID)

    assert [_matches(doc, file_plane) for doc in (digest, chunk, legacy)] == [True, True, True]
    assert [_matches(doc, chunk_plane) for doc in (digest, chunk, legacy)] == [False, True, True]
    assert not _matches(_digest(OTHER_UUID), file_plane), "another file's digest is not this file's"


# ---------------------------------------------------------------------------
# The behaviour under test
# ---------------------------------------------------------------------------


def test_a_stranded_digest_is_reported_as_a_residual(cluster) -> None:
    """The regression this exists for: the delete took the chunks and left the digest.

    A chunk-plane survivor count answers 0 here, so the erasure would report
    success with a verbatim summary of the deleted recording still indexed.
    """
    fake = cluster(_FakeOpenSearch([_chunk(), _digest()], delete_scope=svc.chunk_plane_query))

    residual = _erase()

    assert [doc["doc_type"] for doc in fake.docs] == [digest_mapping.DOC_TYPE_DIGEST], (
        "the harness did not strand a digest, so nothing is being proven"
    )
    assert residual == [("transcript_chunks", "1 document(s) survive")]


def test_a_clean_sweep_reports_nothing(cluster) -> None:
    """The other control: a check that always fires is not a check."""
    fake = cluster(_FakeOpenSearch([_chunk(), _digest(), _chunk(OTHER_UUID)]))

    residual = _erase()

    assert fake.docs == [_chunk(OTHER_UUID)], "the delete must take both planes, and only this file"
    assert residual == []
    assert fake.cache_bumps == 1, (
        "an erasure must invalidate the chat corpus cache, or chat can quote the "
        "erased recording for the length of the retrieval-cache TTL"
    )


def test_a_legacy_chunk_written_before_v6_is_still_counted(cluster) -> None:
    """Pre-v6 documents carry no ``doc_type``; a bare term would miss every one."""
    fake = cluster(
        _FakeOpenSearch([_chunk(legacy=True)], delete_scope=lambda _uuid: {"term": {"nope": 1}})
    )

    residual = _erase()

    assert fake.docs, "the harness deleted the document it was supposed to strand"
    assert residual == [("transcript_chunks", "1 document(s) survive")]


def test_an_unreachable_cluster_is_a_residual_not_a_clean_sweep(cluster) -> None:
    """ "I could not ask" must never audit as "nothing is there"."""
    fake = cluster(_FakeOpenSearch([_digest()], count_raises=ConnectionError("refused")))

    residual = _erase()

    assert len(residual) == 1
    stage, message = residual[0]
    assert stage == "transcript_chunks"
    assert message.startswith("could not verify: "), message


def test_an_absent_index_is_the_cluster_answering_zero(cluster) -> None:
    """A deployment that never had the index has nothing to erase — not an error."""
    fake = cluster(_FakeOpenSearch([], index_present=False))

    assert _erase() == []


def test_the_count_refreshes_before_reading(cluster) -> None:
    """A ``count`` is a search: unrefreshed survivors would read as gone (#435)."""
    fake = cluster(_FakeOpenSearch([_chunk(), _digest()]))

    _erase()

    assert fake.indices.refreshes == 1
