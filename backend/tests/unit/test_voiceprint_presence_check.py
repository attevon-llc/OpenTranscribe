"""``existing_speaker_doc_ids`` answers per id, also while one id sits in two speaker indices.

During the v3 -> v4 migration a voiceprint can be a document in both. A multi-index ``ids``
search returns one hit per copy and ``size`` caps hits, so a size of one per id let the copies
crowd other ids out: they read as "no voiceprint" and the retention sweep skipped their files.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.services import gdpr_voiceprint_erasure as gve


class _TwoIndexClient:
    """Each index lists its documents in order; a search returns hits index by index, capped."""

    def __init__(self, contents: dict[str, list[str]]) -> None:
        self.contents = contents
        self.indices = SimpleNamespace(refresh=lambda index: {})

    def search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        wanted = set(body["query"]["ids"]["values"])
        hits = [
            {"_id": doc, "_index": name}
            for name in index.split(",")
            for doc in self.contents[name]
            if doc in wanted
        ]
        return {"hits": {"hits": hits[: body["size"]]}}


def test_an_id_in_both_indices_does_not_hide_another_id(monkeypatch):
    client = _TwoIndexClient({"speakers_v3": ["a"], "speakers_v4": ["a", "b"]})
    monkeypatch.setattr("app.services.opensearch_service.opensearch_client", client)
    monkeypatch.setattr(gve, "_speaker_index_names", lambda c: ["speakers_v3", "speakers_v4"])

    # Two ids, three hits ("a" twice): with room for only two hits, "b" was crowded out.
    assert gve.existing_speaker_doc_ids(["a", "b"]) == {"a", "b"}
    assert gve.existing_speaker_doc_ids(["a", "c"]) == {"a"}
