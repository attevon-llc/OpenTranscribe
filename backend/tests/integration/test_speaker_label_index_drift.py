"""The indexed speaker label must equal what the resolver derives from the database.

**This test exists because it found a real misattribution that nothing else could see.**

Measured on the dev index: 53 of 54 ``speaker_id`` values agreed with
``canonical_speaker_label()``. One did not — ``speaker_id=2800`` carried
``speaker="Joe Rogan"`` across **398 chunks**, while its ``Speaker`` row is
``name=SPEAKER_00``, no ``display_name``, and a *suggested* name of "Joe Rogan" at
confidence **0.7006** — below ``DEFAULT_SUGGESTION_CONFIDENCE_THRESHOLD`` (0.75). So the
resolver says that speaker is **unidentified**, and the search index says he is Joe Rogan.

Why that matters more than a stale string: chat cites chunks. Those 398 would have been
attributed to a named person on the strength of a sub-threshold guess the product's own
rule rejects — which also sits against the standing "LLM speaker suggestions are never
auto-applied" rule.

**The cause is a class, not an instance.** Chunks are labelled at INDEX time, so every
change to the labelling rule leaves already-indexed documents behind. ``canonical_speaker_label``
(issue W2.0b) unified a rule that four planes previously disagreed on; the documents written
under the old rule were never recomputed. The same shape produced the milder ``"Unknown"``
vs ``"Unknown Speaker"`` split. Any future change to the resolver reopens it.

⚠️ **A unit test cannot catch this.** Both sides are live state — the label is in OpenSearch,
the truth is in Postgres — so the only way to see the disagreement is to compare the two
systems. That is why this lives in ``tests/integration`` and reads the real index.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.orm import Session

from app.models.media import Speaker
from app.utils.speaker_labels import canonical_speaker_label

pytestmark = pytest.mark.integration

_INDEX_SAMPLE_CAP = 1000


def _indexed_labels(client, index: str) -> dict[int, list[str]]:
    """``speaker_id`` -> the distinct ``speaker`` labels indexed against it."""
    resp = client.search(
        index=index,
        body={
            "size": 0,
            "query": {"bool": {"filter": [{"exists": {"field": "speaker_id"}}]}},
            "aggs": {
                "ids": {
                    "terms": {"field": "speaker_id", "size": _INDEX_SAMPLE_CAP},
                    "aggs": {"names": {"terms": {"field": "speaker", "size": 5}}},
                }
            },
        },
    )
    buckets = resp["aggregations"]["ids"]["buckets"]
    assert len(buckets) < _INDEX_SAMPLE_CAP, (
        f"hit the {_INDEX_SAMPLE_CAP} bucket cap — the sweep below would silently cover "
        "only part of the index. Raise the cap or paginate with a composite agg."
    )
    return {b["key"]: [n["key"] for n in b["names"]["buckets"]] for b in buckets}


@pytest.fixture
def _os_client():
    if os.getenv("SKIP_OPENSEARCH", "").lower() == "true":
        pytest.skip("needs a live OpenSearch (SKIP_OPENSEARCH) — this compares two systems")
    from app.services.opensearch_service import get_opensearch_client

    client = get_opensearch_client()
    if client is None:
        pytest.skip("no OpenSearch client available")
    return client


def test_every_indexed_speaker_label_matches_the_resolver(db_session: Session, _os_client):
    """The sweep that found the 398-chunk misattribution."""
    from app.core.config import settings

    index = settings.OPENSEARCH_CHUNKS_INDEX
    if not _os_client.indices.exists(index=index):
        pytest.skip("chunks index not present on this cluster")

    indexed = _indexed_labels(_os_client, index)
    if not indexed:
        pytest.skip("no chunk carries speaker_id yet — nothing to compare")

    drift: list[str] = []
    orphaned: list[int] = []
    for speaker_id, labels in indexed.items():
        row = db_session.query(Speaker).filter(Speaker.id == speaker_id).first()
        if row is None:
            orphaned.append(speaker_id)
            continue
        expected = canonical_speaker_label(
            row.name,
            display_name=row.display_name,
            suggested_name=row.suggested_name,
            confidence=row.confidence,
        )
        for label in labels:
            if label != expected:
                drift.append(
                    f"speaker_id={speaker_id}: index={label!r} but the DB row "
                    f"(name={row.name!r}, display={row.display_name!r}, "
                    f"suggested={row.suggested_name!r}@{row.confidence}) resolves to {expected!r}"
                )

    assert not drift, (
        "the chunk index disagrees with canonical_speaker_label() — chunks are attributed "
        "to a speaker the resolver would name differently. Reindex the affected files; do "
        "NOT relax this assertion.\n  " + "\n  ".join(drift[:20])
    )
    assert not orphaned, (
        f"{len(orphaned)} speaker_id(s) in the index have no Speaker row: {orphaned[:10]}. "
        "The index is referencing deleted speakers."
    )


def test_a_speaker_id_never_carries_two_different_labels(db_session: Session, _os_client):
    """One ``Speaker`` row is one person; two labels on one id means the write path drifted.

    Control for the sweep above: it compares index against DB, this one checks the index is
    self-consistent. Both can fail independently — a uniformly-wrong label passes this and
    fails the other.
    """
    from app.core.config import settings

    index = settings.OPENSEARCH_CHUNKS_INDEX
    if not _os_client.indices.exists(index=index):
        pytest.skip("chunks index not present on this cluster")

    indexed = _indexed_labels(_os_client, index)
    if not indexed:
        pytest.skip("no chunk carries speaker_id yet — nothing to compare")

    conflicted = {sid: labels for sid, labels in indexed.items() if len(labels) > 1}
    assert not conflicted, (
        "a single speaker_id resolves to more than one indexed label, so chunks written at "
        f"different times disagree about who spoke: {list(conflicted.items())[:10]}"
    )
