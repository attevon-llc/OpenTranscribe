"""The settle rule — the thing that decides whether a number may be recorded.

`await_settled` exists because polling the chunk total alone reported deltas of
223 / 357 / 591 chunks between runs over a corpus nobody had changed. Every one
was a measurement taken while a reindex was still walking the file list: a
plateau in a rising count is indistinguishable from the end of it.

Each test below is one way the harness previously certified a corpus that was
not ready, so a regression here does not look like a broken test — it looks like
a metric delta.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.eval.harness import index_reader

FILE_UUIDS = ["uuid-a", "uuid-b", "uuid-c"]


class ScriptedClient:
    """An OpenSearch client that replays a fixed sequence of observations."""

    def __init__(self, observations: list[dict[str, int]]) -> None:
        self._observations = list(observations)
        self.bodies: list[dict[str, Any]] = []
        self.refreshes = 0
        self.indices = self  # `client.indices.refresh(...)` reaches the same object

    def refresh(self, *, index: str) -> None:  # noqa: ARG002 - signature parity
        self.refreshes += 1

    def search(self, *, index: str, body: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        # The distinct-file count is a SECOND request, a paged `composite` walk, so
        # it must not advance the observation cursor — otherwise every poll would
        # consume two scripted observations. It replays the CURRENT observation's
        # file count as that many buckets, which is what makes the count exact
        # (`cardinality` estimated it, and undercounted a real 1,984-file corpus
        # by one, forever — see index_reader._observe).
        if "u" in body.get("aggs", {}):
            observation = self._observations[
                min(max(len(self.bodies) - 1, 0), len(self._observations) - 1)
            ]
            buckets = [{"key": {"f": f"uuid-{n}"}} for n in range(observation["files"])]
            return {"aggregations": {"u": {"buckets": buckets}}}

        self.bodies.append(body)
        # The last observation repeats forever, so a test asserting a timeout
        # does not depend on how many polls fit inside it.
        observation = self._observations[min(len(self.bodies) - 1, len(self._observations) - 1)]
        aggregations: dict[str, Any] = {}
        if "stale" in body.get("aggs", {}):
            aggregations["stale"] = {"doc_count": observation.get("stale", 0)}
        return {
            "hits": {"total": {"value": observation["chunks"]}},
            "aggregations": aggregations,
        }


def _settle(client: ScriptedClient, **kwargs: Any) -> dict[str, Any]:
    return index_reader.await_settled(
        client,
        "transcript_chunks",
        FILE_UUIDS,
        expected_files=len(FILE_UUIDS),
        interval_s=0.0,
        timeout_s=0.05,
        **kwargs,
    )


def test_a_complete_and_stable_corpus_settles() -> None:
    client = ScriptedClient([{"files": 3, "chunks": 900}, {"files": 3, "chunks": 900}])
    settled = _settle(client)
    assert settled["files"] == 3
    assert settled["chunks"] == 900
    assert settled["polls"] == 2, "Two identical polls are the minimum evidence of stability."
    assert client.refreshes == 2, "Each poll must refresh, or it reads a stale searcher."


def test_it_waits_out_a_corpus_that_is_still_growing() -> None:
    client = ScriptedClient(
        [
            {"files": 3, "chunks": 300},
            {"files": 3, "chunks": 700},
            {"files": 3, "chunks": 900},
            {"files": 3, "chunks": 900},
        ]
    )
    settled = index_reader.await_settled(
        client,
        "transcript_chunks",
        FILE_UUIDS,
        expected_files=len(FILE_UUIDS),
        interval_s=0.0,
        timeout_s=5.0,
    )
    assert settled["chunks"] == 900
    assert settled["polls"] == 4


def test_a_stable_but_incomplete_corpus_never_settles() -> None:
    """The reindex has deleted a file's chunks and not yet written the new ones."""
    client = ScriptedClient([{"files": 2, "chunks": 600}])
    with pytest.raises(index_reader.IndexNotSettledError) as excinfo:
        _settle(client)
    assert "only 2 of 3 files" in str(excinfo.value)


def test_a_dispatched_but_unstarted_reindex_never_settles() -> None:
    """Complete, stable, and entirely the PREVIOUS index.

    Celery queue latency outlasts two poll intervals easily. Without the
    `since` arm the settle check certifies the old corpus as the new one and the
    "after" measurement is byte-identical to the "before" for the best possible
    reason: it is the same index.
    """
    client = ScriptedClient([{"files": 3, "chunks": 900, "stale": 900}])
    with pytest.raises(index_reader.IndexNotSettledError) as excinfo:
        _settle(client, since="2026-08-13T00:00:00+00:00")
    assert "900 chunk(s) predate this run" in str(excinfo.value)


def test_the_since_arm_is_only_armed_when_asked() -> None:
    """A plain measurement run must not demand a freshly written corpus."""
    client = ScriptedClient([{"files": 3, "chunks": 900, "stale": 900}] * 2)
    settled = _settle(client)
    assert settled["chunks"] == 900
    # The observation body now carries NO aggs at all unless `since` asks for the
    # stale arm — the distinct-file count moved to its own composite request — so
    # absence of the key is the stronger form of "the arm is not armed".
    assert all("stale" not in body.get("aggs", {}) for body in client.bodies)


def test_the_observation_is_scoped_to_the_corpus_and_is_never_a_hybrid_body() -> None:
    client = ScriptedClient([{"files": 3, "chunks": 900}] * 2)
    _settle(client, since="2026-08-13T00:00:00+00:00")
    body = client.bodies[0]
    assert body["query"] == {"bool": {"filter": [{"terms": {"file_uuid": FILE_UUIDS}}]}}, (
        "Counting the whole index instead of the corpus makes the settle check "
        "pass on somebody else's documents."
    )
    assert body["size"] == 0
    assert body["track_total_hits"] is True, "A capped total silently plateaus at 10,000."
    assert "hybrid" not in repr(body), (
        "OpenSearch 3.4 throws inside score-ranker-processor when aggs meet a "
        "hybrid body — measurement code included."
    )
    stale = body["aggs"]["stale"]["filter"]["bool"]["should"]
    assert {"bool": {"must_not": {"exists": {"field": "indexed_at"}}}} in stale, (
        "A document with no indexed_at was certainly not written by this run."
    )
