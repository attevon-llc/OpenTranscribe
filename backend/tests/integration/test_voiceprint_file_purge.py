"""A ``files`` voiceprint purge against the REAL speaker index (OpenSearch) and Postgres.

File A and file B each have one speaker, both linked to profile P. Purging file A must remove A's
speaker document, leave B's, keep P's row, and recompute P's embedding from B alone (its stored
vector equals B's voiceprint). The residual check of the pass must be all zero.

Rows live in the test savepoint; every document this test writes is deleted in ``finally``.
Needs the stack (``OPENSEARCH_PORT`` of an isolated ``--fresh`` stack).
"""

from __future__ import annotations

import math
import uuid
from typing import TYPE_CHECKING
from typing import cast

import pytest

from app.models.media import SpeakerProfile
from app.services.privacy import purge
from app.services.privacy.policy import clear_write_backstop_cache
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_profile
from tests.fixtures.speaker_cleanup_seams import make_speaker

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

DIM = 256


class _KeepOpen:
    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self) -> None:
        pass


def _unit(seed: int) -> list[float]:
    raw = [math.sin(seed * (i + 1) * 0.37) + (1.0 if i == seed else 0.0) for i in range(DIM)]
    norm = math.sqrt(sum(v * v for v in raw))
    return [v / norm for v in raw]


def _cosine(u: list[float], v: list[float]) -> float:
    dot = sum(a * b for a, b in zip(u, v, strict=True))
    return dot / (math.sqrt(sum(a * a for a in u)) * math.sqrt(sum(b * b for b in v)))


def test_a_file_purge_removes_its_voiceprints_and_recomputes_the_profile(db_session, normal_user):
    from app.services.gdpr_erasure_service import delete_speaker_docs_by_ids
    from app.services.opensearch_service import add_speaker_embedding
    from app.services.opensearch_service import get_speaker_embedding
    from app.services.opensearch_service import store_profile_embedding
    from app.services.opensearch_service.profiles import get_profile_embedding

    clear_write_backstop_cache()
    db = db_session
    file_a, file_b = make_file(db, normal_user), make_file(db, normal_user)
    profile = make_profile(db, normal_user, f"Pat {uuid.uuid4().hex[:6]}")
    sa = make_speaker(db, normal_user, file_a, profile_id=profile.id, display_name=profile.name)
    sb = make_speaker(db, normal_user, file_b, profile_id=profile.id, display_name=profile.name)
    db.commit()
    voice_a, voice_b = _unit(3), _unit(11)
    doc_ids = [str(sa.uuid), str(sb.uuid), f"profile_{profile.uuid}"]
    try:
        for speaker, voice, media_file in ((sa, voice_a, file_a), (sb, voice_b, file_b)):
            add_speaker_embedding(
                speaker_id=speaker.id,
                speaker_uuid=str(speaker.uuid),
                user_id=normal_user.id,
                name=speaker.name,
                embedding=voice,
                profile_id=profile.id,
                profile_uuid=str(profile.uuid),
                media_file_id=media_file.id,
            )
        averaged = [(a + b) / 2 for a, b in zip(voice_a, voice_b, strict=True)]
        assert store_profile_embedding(
            profile_id=profile.id,
            profile_uuid=str(profile.uuid),
            profile_name=profile.name,
            embedding=averaged,
            speaker_count=2,
            user_id=normal_user.id,
        )
        assert get_speaker_embedding(str(sa.uuid)) is not None  # the seed really landed

        result = purge.run_purge_pass(
            purge.PurgeScope(kind="files", file_ids=(file_a.id,)),
            [purge.VOICEPRINTS],
            session_factory=lambda: cast("Session", _KeepOpen(db)),
            reindex=lambda files: 0,
        )

        assert get_speaker_embedding(str(sa.uuid)) is None
        kept = get_speaker_embedding(str(sb.uuid))
        assert kept is not None and _cosine(kept, voice_b) == pytest.approx(1.0, abs=1e-5)
        assert db.query(SpeakerProfile).filter_by(id=profile.id).count() == 1
        stored = get_profile_embedding(str(profile.uuid))
        assert stored is not None
        assert _cosine(stored, voice_b) == pytest.approx(1.0, abs=1e-5)
        assert _cosine(stored, averaged) < 0.99  # it really changed from the A+B average
        residual = result.residual()
        assert residual.ok, (residual.counts, residual.unverified, result.errors)
        assert result.counts()["opensearch"]["speaker_docs_deleted"] >= 1
    finally:
        delete_speaker_docs_by_ids(doc_ids, errors=[], label="test teardown")
