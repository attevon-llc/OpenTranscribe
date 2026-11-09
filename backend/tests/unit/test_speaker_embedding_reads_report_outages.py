"""An index outage must not read as "no voiceprints" (profile centroids, suggestions, clustering)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.models.speaker import Speaker
from app.services import profile_embedding_service as pes
from app.services.opensearch_service.client import OpenSearchUnavailableError


def test_profile_embedding_collection_asks_for_a_strict_read():
    seen: dict[str, object] = {}

    def fake(uuids, **kw):
        seen.update(kw)
        raise OpenSearchUnavailableError("down")

    with (
        patch("app.services.opensearch_service.get_speaker_embeddings_batch", fake),
        pytest.raises(OpenSearchUnavailableError),
    ):
        pes._collect_speaker_embeddings([Speaker(id=1, uuid="u1")])
    assert seen == {"strict": True}
