"""``opensearch_erasure``: every eraser deletes the right documents and reports what survives.

A HEAD-vs-split mutation comparison of ``services/file_cleanup_service.py`` (repo cleanup,
wave b10) found most of the speaker-family erasers pinned by nothing the throwaway-DB suite
runs: the only suite that drives them against a cluster
(``integration/test_voiceprint_erasure_opensearch.py``) skips without OpenSearch, and every
other caller stubs them out. A survivor count that is ignored, an id sweep that addresses
``<uuid>`` instead of ``profile_<uuid>``, or a summary sweep that matches every file's
documents all left that suite green. These run the real erasers against a stand-in cluster
that answers by evaluating what it is asked (``fixtures/speaker_family_cluster.py``).

The speaker family (voiceprints, profile embeddings, cluster centroids) also has a request
budget: the erase runs on the speaker-merge request path, where it once cost about twenty
sequential requests per merge in a 100-user load run. It is now one ``_bulk``
delete, one refresh and one per-index survivor count, whatever the number of documents or
speaker indices.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services import opensearch_erasure as ose
from app.services.opensearch_service import settings as os_settings
from tests.fixtures.speaker_family_cluster import SpeakerFamilyCluster

# The real list has the alias, v3, v4 and the legacy v3 backup.
SPEAKER_INDICES = ["speakers", "speakers_v3", "speakers_v4", "speakers_v3_backup"]
SUMMARY_INDEX = os_settings.OPENSEARCH_SUMMARY_INDEX


@pytest.fixture
def cluster(monkeypatch):
    """Install a stand-in as the erasers' client, with the real speaker index names."""

    def install(
        docs: dict[str, dict[str, dict[str, Any]]], aliases: dict[str, str] | None = None
    ) -> SpeakerFamilyCluster:
        fake = SpeakerFamilyCluster(docs, aliases)
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
# the speaker family: voiceprints, profile embeddings, cluster centroids
# ---------------------------------------------------------------------------

# (eraser, residual stage, residual label, document id prefix)
FAMILY = [
    pytest.param(ose._erase_speaker_docs, "speakers", "voiceprint", "", id="voiceprints"),
    pytest.param(
        ose._erase_profile_docs, "profiles", "profile embedding", "profile_", id="profiles"
    ),
    pytest.param(ose._erase_cluster_docs, "clusters", "centroid", "cluster_", id="centroids"),
]


@pytest.mark.parametrize(("eraser", "stage", "label", "prefix"), FAMILY)
def test_every_copy_is_erased_from_every_speaker_index_by_its_doc_id(
    cluster, residual, eraser, stage, label, prefix
) -> None:
    """A mid-migration copy can sit in any speaker index; only the named documents go."""
    fake = cluster(
        {
            "speakers": {f"{prefix}a": {}, "a-unprefixed": {}},
            "speakers_v4": {f"{prefix}a": {}, f"{prefix}b": {}, f"{prefix}other": {}},
            "speakers_v3_backup": {f"{prefix}b": {}},
        }
    )

    eraser(["a", "b"], _fail(residual))

    assert residual == []
    assert fake.docs == {
        "speakers": {"a-unprefixed": {}},
        "speakers_v4": {f"{prefix}other": {}},
        "speakers_v3_backup": {},
    }


@pytest.mark.parametrize(("eraser", "stage", "label", "prefix"), FAMILY)
def test_the_erase_costs_three_requests_whatever_the_documents_and_indices(
    cluster, residual, eraser, stage, label, prefix
) -> None:
    """One bulk delete, one refresh, one survivor read. Not a request per id or per index."""
    fake = cluster({index: {f"{prefix}{n}": {} for n in range(5)} for index in SPEAKER_INDICES})

    eraser([str(n) for n in range(5)], _fail(residual))

    assert residual == []
    assert [call[0] for call in fake.calls] == ["bulk", "refresh", "search"], fake.calls
    assert fake.calls[0] == ("bulk", 5 * len(SPEAKER_INDICES))
    assert all(not docs for docs in fake.docs.values())


@pytest.mark.parametrize(("eraser", "stage", "label", "prefix"), FAMILY)
def test_a_document_still_inside_the_refresh_window_is_erased(
    cluster, residual, eraser, stage, label, prefix
) -> None:
    """Written but not yet searchable: a by-id delete removes it; a query sweep would not."""
    fake = cluster({"speakers_v4": {}})
    fake.docs["speakers_v4"][f"{prefix}a"] = {}  # live, but the searcher has not seen it

    eraser(["a"], _fail(residual))

    assert residual == []
    assert fake.docs["speakers_v4"] == {}


@pytest.mark.parametrize(("eraser", "stage", "label", "prefix"), FAMILY)
def test_a_refused_delete_is_reported_by_the_survivor_count(
    cluster, residual, eraser, stage, label, prefix
) -> None:
    """The bulk answer is per item and is not proof; the count names the index that kept it."""
    fake = cluster({"speakers_v3": {f"{prefix}a": {}}, "speakers_v4": {f"{prefix}a": {}}})
    fake.write_blocked.add("speakers_v4")

    eraser(["a"], _fail(residual))

    assert residual == [(stage, f"1 {label} doc(s) survive in speakers_v4")]
    assert fake.docs["speakers_v3"] == {}


@pytest.mark.parametrize(("eraser", "stage", "label", "prefix"), FAMILY)
def test_an_alias_and_its_index_count_a_survivor_once(
    cluster, residual, eraser, stage, label, prefix
) -> None:
    """``speakers`` is an alias of v3 or v4 on a migrated install; the survivor is one document."""
    fake = cluster({"speakers_v4": {f"{prefix}a": {}}}, aliases={"speakers": "speakers_v4"})
    fake.write_blocked.add("speakers_v4")

    eraser(["a"], _fail(residual))

    assert residual == [(stage, f"1 {label} doc(s) survive in speakers_v4")]


@pytest.mark.parametrize(("eraser", "stage", "label", "prefix"), FAMILY)
def test_an_unreachable_cluster_is_reported_for_the_delete_and_the_count(
    cluster, residual, monkeypatch, eraser, stage, label, prefix
) -> None:
    """ "I could not ask" is not "nothing is there", for either step."""
    cluster({"speakers": {}})
    monkeypatch.setattr(ose, "_opensearch_client", lambda: None)

    eraser(["a"], _fail(residual))

    assert residual == [
        (stage, "OpenSearch client unavailable"),
        (stage, f"could not verify {', '.join(SPEAKER_INDICES)}: OpenSearch client unavailable"),
    ]


@pytest.mark.parametrize(("eraser", "stage", "label", "prefix"), FAMILY)
def test_nothing_to_erase_sends_nothing(cluster, residual, eraser, stage, label, prefix) -> None:
    fake = cluster({"speakers": {"a": {}}})

    eraser([], _fail(residual))

    assert residual == []
    assert fake.calls == []


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
