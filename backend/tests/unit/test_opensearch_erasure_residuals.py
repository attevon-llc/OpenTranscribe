"""``opensearch_erasure``: every eraser deletes the right documents and reports what survives.

A HEAD-vs-split mutation comparison of ``services/file_cleanup_service.py`` (repo cleanup,
wave b10) found most of the speaker-family erasers pinned by nothing the throwaway-DB suite
runs: the only suite that drives them against a cluster
(``integration/test_voiceprint_erasure_opensearch.py``) skips without OpenSearch, and every
other caller stubs them out. A survivor count that is ignored, an id sweep that addresses
``<uuid>`` instead of ``profile_<uuid>``, or a summary sweep that matches every file's
documents all left that suite green. These run the real erasers against a stand-in cluster
that answers by evaluating what it is asked:

* deletes by id are realtime, but ``count`` reads a SEARCHER, which keeps listing a deleted
  document until ``indices.refresh`` — the reason ``_count_surviving`` refreshes first;
* a ``delete_by_query`` whose scroll meets a document already deleted by id answers 409
  unless it was sent ``conflicts="proceed"``, as the real cluster does.
"""

from __future__ import annotations

from typing import Any

import pytest
from opensearchpy.exceptions import ConflictError
from opensearchpy.exceptions import NotFoundError

from app.services import opensearch_erasure as ose
from app.services.opensearch_service import settings as os_settings

SPEAKER_INDICES = ["speakers", "speakers_v4"]
SUMMARY_INDEX = os_settings.OPENSEARCH_SUMMARY_INDEX


def _matches(doc_id: str, doc: dict[str, Any], query: dict[str, Any]) -> bool:
    if "ids" in query:
        return doc_id in query["ids"]["values"]
    if "term" in query:
        ((field, value),) = query["term"].items()
        return bool(doc.get(field) == value)
    if "match_all" in query:
        return True
    raise AssertionError(f"the stand-in cannot evaluate {query!r}; teach it, don't skip it")


class _Indices:
    def __init__(self, cluster: _Cluster) -> None:
        self._cluster = cluster

    def exists(self, index: str) -> bool:
        return index in self._cluster.docs

    def refresh(self, index: str) -> None:
        self._cluster.calls.append(("refresh", index))
        self._cluster.searcher[index] = dict(self._cluster.docs.get(index, {}))


class _Cluster:
    """Live documents per index, plus the searcher's (stale until refresh) view of them."""

    def __init__(self, docs: dict[str, dict[str, dict[str, Any]]]) -> None:
        self.docs = {index: dict(found) for index, found in docs.items()}
        self.searcher = {index: dict(found) for index, found in docs.items()}
        self.indices = _Indices(self)
        self.calls: list[tuple[Any, ...]] = []
        self.failures: list[dict[str, Any]] = []

    def delete_now(self, index: str, doc_id: str) -> None:
        """A realtime by-id delete: gone at once, still listed by the searcher."""
        self.docs.get(index, {}).pop(doc_id, None)

    def count(self, index: str, body: dict[str, Any]) -> dict[str, int]:
        self.calls.append(("count", index))
        if index not in self.docs:
            raise NotFoundError(404, "index_not_found_exception", {})
        found = self.searcher.get(index, {})
        return {"count": sum(1 for i, d in found.items() if _matches(i, d, body["query"]))}

    def delete_by_query(self, index: str, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("delete_by_query", index, body["query"], kwargs))
        query = body["query"]
        listed = [i for i, d in self.searcher.get(index, {}).items() if _matches(i, d, query)]
        live = self.docs.get(index, {})
        if kwargs.get("conflicts") != "proceed" and any(i not in live for i in listed):
            raise ConflictError(409, "version_conflict_engine_exception", {})
        for doc_id in listed:
            live.pop(doc_id, None)
        if kwargs.get("refresh"):
            self.searcher[index] = dict(live)
        return {"deleted": len(listed), "failures": self.failures}


@pytest.fixture
def cluster(monkeypatch):
    """Install a stand-in as the erasers' client; the speaker indices are fixed to two."""

    def install(docs: dict[str, dict[str, dict[str, Any]]]) -> _Cluster:
        fake = _Cluster(docs)
        monkeypatch.setattr(ose, "_opensearch_client", lambda: fake)
        monkeypatch.setattr(
            "app.services.opensearch_service.speaker_maintenance.speaker_embedding_indices",
            lambda: list(SPEAKER_INDICES),
        )
        return fake

    return install


@pytest.fixture
def residual() -> list[tuple[str, str]]:
    return []


def _fail(into: list[tuple[str, str]]):
    return lambda stage, err: into.append((stage, str(err)))


# ---------------------------------------------------------------------------
# _count_surviving
# ---------------------------------------------------------------------------


def test_the_count_refuses_to_answer_without_a_client(monkeypatch) -> None:
    """ "I could not ask" is not "nothing is there"."""
    monkeypatch.setattr(ose, "_opensearch_client", lambda: None)

    with pytest.raises(RuntimeError, match="OpenSearch client unavailable"):
        ose._count_surviving("speakers", {"ids": {"values": ["a"]}})


def test_an_absent_index_is_the_cluster_answering_zero(cluster) -> None:
    fake = cluster({"speakers": {"a": {}}})

    assert ose._count_surviving("speakers_v4", {"ids": {"values": ["a"]}}) == 0
    assert ("count", "speakers_v4") not in fake.calls


def test_the_count_refreshes_before_it_reads(cluster) -> None:
    """A by-id delete is realtime; the searcher lists it until a refresh."""
    fake = cluster({"speakers": {"a": {}, "b": {}}})
    fake.delete_now("speakers", "a")

    assert ose._count_surviving("speakers", {"ids": {"values": ["a", "b"]}}) == 1
    assert fake.calls.index(("refresh", "speakers")) < fake.calls.index(("count", "speakers"))


# ---------------------------------------------------------------------------
# voiceprints
# ---------------------------------------------------------------------------


def test_a_surviving_voiceprint_is_reported(cluster, residual, monkeypatch) -> None:
    """``remove_speaker_embedding`` answers the same for "absent" and "failed"."""
    cluster({"speakers": {"spk-1": {}}, "speakers_v4": {}})
    monkeypatch.setattr(
        "app.services.opensearch_service.remove_speaker_embedding", lambda speaker_uuid: False
    )

    ose._erase_speaker_docs(["spk-1"], _fail(residual))

    assert residual == [("speakers", "1 voiceprint doc(s) survive in speakers")]


def test_a_removed_voiceprint_is_a_clean_sweep(cluster, residual, monkeypatch) -> None:
    """CONTROL for the one above: a sweep that always reported would pass it too."""
    fake = cluster({"speakers": {"spk-1": {}}, "speakers_v4": {"spk-1": {}}})

    def remove(speaker_uuid: str) -> bool:
        for index in SPEAKER_INDICES:
            fake.delete_now(index, speaker_uuid)
        return True

    monkeypatch.setattr("app.services.opensearch_service.remove_speaker_embedding", remove)

    ose._erase_speaker_docs(["spk-1"], _fail(residual))

    assert residual == []


# ---------------------------------------------------------------------------
# profile embeddings
# ---------------------------------------------------------------------------


def _profiles_removed_by_id(fake: _Cluster, monkeypatch) -> None:
    """``remove_profile_embedding``: a realtime delete from the main index only."""

    def remove(profile_uuid: str) -> bool:
        fake.delete_now("speakers", f"profile_{profile_uuid}")
        return True

    monkeypatch.setattr("app.services.opensearch_service.remove_profile_embedding", remove)


def test_profile_embeddings_are_swept_from_every_speaker_index(
    cluster, residual, monkeypatch
) -> None:
    """The by-id remove misses a mid-migration copy; the sweep must reach it, by its doc id."""
    fake = cluster(
        {
            "speakers": {"profile_p1": {}, "p1": {}},
            "speakers_v4": {"profile_p1": {}, "profile_other": {}},
        }
    )
    _profiles_removed_by_id(fake, monkeypatch)

    ose._erase_profile_docs(["p1"], _fail(residual))

    assert residual == []
    assert fake.docs == {"speakers": {"p1": {}}, "speakers_v4": {"profile_other": {}}}


def test_the_profile_sweep_proceeds_past_a_document_deleted_by_id(
    cluster, residual, monkeypatch
) -> None:
    """The searcher still lists what the by-id delete removed; a 409 there means "gone"."""
    fake = cluster({"speakers": {"profile_p1": {}}, "speakers_v4": {}})
    _profiles_removed_by_id(fake, monkeypatch)

    ose._erase_profile_docs(["p1"], _fail(residual))

    assert residual == []
    sweeps = [call for call in fake.calls if call[0] == "delete_by_query"]
    assert sweeps and all(call[3]["conflicts"] == "proceed" for call in sweeps), sweeps


def test_a_surviving_profile_embedding_is_reported(cluster, residual, monkeypatch) -> None:
    fake = cluster({"speakers": {"profile_p1": {}}, "speakers_v4": {}})
    # A remove and a sweep that both delete nothing: only the survivor count can tell.
    monkeypatch.setattr(
        "app.services.opensearch_service.remove_profile_embedding", lambda profile_uuid: False
    )
    monkeypatch.setattr(fake, "delete_by_query", lambda index, body, **kwargs: {"deleted": 0})

    ose._erase_profile_docs(["p1"], _fail(residual))

    assert residual == [("profiles", "1 profile embedding doc(s) survive in speakers")]


def test_an_unreachable_profile_index_is_reported(cluster, residual, monkeypatch) -> None:
    _profiles_removed_by_id(cluster({"speakers": {}, "speakers_v4": {}}), monkeypatch)
    monkeypatch.setattr(ose, "_opensearch_client", lambda: None)

    ose._erase_profile_docs(["p1"], _fail(residual))

    assert residual == [
        ("profiles", f"could not verify {index}: OpenSearch client unavailable")
        for index in SPEAKER_INDICES
    ]


# ---------------------------------------------------------------------------
# cluster centroids
# ---------------------------------------------------------------------------


def test_centroids_are_swept_from_every_speaker_index_by_their_doc_id(cluster, residual) -> None:
    fake = cluster(
        {
            "speakers": {"cluster_c1": {}, "c1": {}},
            "speakers_v4": {"cluster_c1": {}, "cluster_other": {}},
        }
    )

    ose._erase_cluster_docs(["c1"], _fail(residual))

    assert residual == []
    assert fake.docs == {"speakers": {"c1": {}}, "speakers_v4": {"cluster_other": {}}}


def test_a_surviving_centroid_is_reported(cluster, residual, monkeypatch) -> None:
    fake = cluster({"speakers": {"cluster_c1": {}}, "speakers_v4": {}})
    monkeypatch.setattr(fake, "delete_by_query", lambda index, body, **kwargs: {"deleted": 0})

    ose._erase_cluster_docs(["c1"], _fail(residual))

    assert residual == [("clusters", "1 centroid doc(s) survive in speakers")]


def test_an_unreachable_centroid_index_is_reported(cluster, residual, monkeypatch) -> None:
    cluster({"speakers": {}, "speakers_v4": {}})
    monkeypatch.setattr(ose, "_opensearch_client", lambda: None)

    ose._erase_cluster_docs(["c1"], _fail(residual))

    assert residual == [
        ("clusters", f"could not erase/verify {index}: OpenSearch client unavailable")
        for index in SPEAKER_INDICES
    ]


# ---------------------------------------------------------------------------
# legacy summaries
# ---------------------------------------------------------------------------


def test_only_this_files_summaries_are_swept(cluster, residual) -> None:
    fake = cluster({SUMMARY_INDEX: {"s1": {"file_id": 7}, "s2": {"file_id": 8}}})

    ose._erase_summary_docs(7, _fail(residual))

    assert residual == []
    assert fake.docs[SUMMARY_INDEX] == {"s2": {"file_id": 8}}


def test_an_unreachable_summary_index_is_reported(residual, monkeypatch) -> None:
    monkeypatch.setattr(ose, "_opensearch_client", lambda: None)

    ose._erase_summary_docs(7, _fail(residual))

    assert residual == [("transcript_summaries", "OpenSearch client unavailable")]


# ---------------------------------------------------------------------------
# the per-file composition
# ---------------------------------------------------------------------------


def test_purge_reports_an_unreadable_speaker_list_as_a_residual(monkeypatch) -> None:
    """A failed enumeration is still a miss: the embeddings are unaccounted for."""
    monkeypatch.setattr(ose, "_erase_speaker_docs", lambda uuids, fail: None)
    monkeypatch.setattr(ose, "_erase_transcript_doc", lambda uuid, fail: None)
    monkeypatch.setattr(ose, "_erase_transcript_chunks", lambda uuid, fail: None)
    monkeypatch.setattr(ose, "_erase_summary_docs", lambda file_id, fail: None)

    residual = ose._cleanup_opensearch_for_file(
        {"file_id": 1, "speaker_uuids": [], "speaker_read_error": "DetachedInstanceError"},
        "some-uuid",
    )
    assert any(entry["stage"] == "speakers" for entry in residual), residual
