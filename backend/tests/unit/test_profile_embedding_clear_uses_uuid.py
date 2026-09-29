"""A profile left with no speakers must have its embedding removed — by UUID.

Profile embeddings are stored under ``profile_<uuid>``. ``_process_profile_with_no_speakers``
passed the profile's integer **id** to ``remove_profile_embedding``, which built the doc id
``profile_<int>``, matched nothing, and returned quietly. The profile's averaged voiceprint —
computed from speakers whose recordings may since have been purged — stayed indexed and
matchable. ``tests/integration/test_voiceprint_erasure_opensearch.py`` proves the same
against a real cluster; this is the CI-runnable half.
"""

from __future__ import annotations

import uuid as uuid_pkg
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit


def test_profile_with_no_speakers_clears_the_embedding_by_uuid(monkeypatch):
    from app.services import opensearch_service
    from app.services import profile_embedding_service as pes

    calls: list[str] = []
    monkeypatch.setattr(opensearch_service, "remove_profile_embedding", calls.append)
    profile: Any = SimpleNamespace(
        id=17, uuid=uuid_pkg.uuid4(), embedding_count=3, last_embedding_update=None
    )

    assert pes._process_profile_with_no_speakers(profile, 17) is True

    assert calls == [str(profile.uuid)]
    assert profile.embedding_count == 0
