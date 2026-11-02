# mypy: disable-error-code="arg-type"
# This suite passes structural stand-ins (a fake session, a recording Path) to
# signatures that declare Session/Path. Declared once here rather than as a cast at
# every call site: a cast buries the thing being asserted, and widening a production
# signature to suit a test is worse. Same convention as
# tests/unit/test_proxy_identity_consistency.py.
"""The OpenSearch reindex task holds no DB transaction across its re-index round trips.

See ``tests/unit/_session_lifetime_trackers.py`` for the depth-tracking scope stand-in
and the measured incident behind this family of tests.
"""

import pytest

from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.tasks import reindex_task as rix
from tests.unit._session_lifetime_trackers import ScopeTracker
from tests.unit._session_lifetime_trackers import leak
from tests.unit._session_lifetime_trackers import make_transcribed_file


# --------------------------------------------------------------------------- #
# 5. reindex_batch — 50 OpenSearch round trips per page
# --------------------------------------------------------------------------- #
class _FakeRedis:
    def __init__(self):
        self.hashes: dict[tuple[str, str], int] = {}
        self.sets: dict[str, set] = {}
        self.expiries: dict[str, int] = {}

    def hget(self, key, field):
        return self.hashes.get((key, field))

    def hset(self, key, field, value):
        self.hashes[(key, field)] = value

    def hincrby(self, key, field, amount=1):
        value = int(self.hashes.get((key, field)) or 0) + amount
        self.hashes[(key, field)] = value
        return value

    def sadd(self, key, *values):
        self.sets.setdefault(key, set()).update(values)

    def expire(self, key, seconds):
        self.expiries[key] = seconds
        return key in self.sets or any(k == key for k, _ in self.hashes)


@pytest.fixture
def reindex_env(db_session, monkeypatch):
    """Patch ``reindex_task``'s Redis/progress/indexing seams; keep the DB real."""
    tracker = ScopeTracker(db_session)
    indexed: list[str] = []

    monkeypatch.setattr(rix, "session_scope", tracker.scope)
    monkeypatch.setattr(rix, "get_redis", lambda: _FakeRedis())
    monkeypatch.setattr(rix, "_send_reindex_progress", lambda *a, **kw: None)
    monkeypatch.setattr(rix, "_is_cancellation_requested", lambda user_id, run_id=None: False)
    monkeypatch.setattr(rix, "_handle_reindex_completion", lambda *a, **kw: None)

    class _FakeTracker:
        def __init__(self, **kwargs):
            pass

        @staticmethod
        def get_state(task_type, user_id):
            return None

        def start(self, message=""):
            pass

        def resume_from_state(self, state):
            pass

    monkeypatch.setattr("app.services.progress_tracker.ProgressTracker", _FakeTracker)

    class _FakeIndexing:
        def reindex_transcript(self, **kwargs):
            # A full re-chunk + embed + bulk write per file. Fifty of these used
            # to run inside one transaction.
            tracker.observe("reindex_transcript")
            indexed.append(kwargs["file_uuid"])
            return 7

    monkeypatch.setattr(
        "app.services.search.indexing_service.TranscriptIndexingService", _FakeIndexing
    )

    tracker.indexed = indexed
    return tracker


def test_reindex_batch_indexes_outside_the_session(db_session, normal_user, reindex_env):
    tracker = reindex_env
    indexed = tracker.indexed
    files = [make_transcribed_file(db_session, normal_user, speakers=1)[0] for _ in range(3)]

    stats = rix.reindex_batch_task([f.id for f in files], normal_user.id)

    assert stats["indexed"] == 3, stats
    assert stats["chunks"] == 21
    assert sorted(indexed) == sorted(str(f.uuid) for f in files)

    depths = [depth for label, depth in tracker.observations if label == "reindex_transcript"]
    assert depths == [0, 0, 0], leak(tracker, "reindex_transcript")
    assert tracker.opened >= 1, "the read phase must actually have opened a session"
    assert tracker.max_depth == 1


def test_reindex_page_loader_returns_plain_data(db_session, normal_user, monkeypatch):
    tracker = ScopeTracker(db_session)
    monkeypatch.setattr(rix, "session_scope", tracker.scope)
    media_file, _ = make_transcribed_file(db_session, normal_user, speakers=1)

    page = rix._load_reindex_page([media_file.id])

    assert tracker.depth == 0
    assert len(page) == 1
    file_uuid, metadata = page[0]
    assert file_uuid == str(media_file.uuid)
    assert metadata is not None
    assert len(metadata["segments"]) == 2
    assert all(isinstance(seg, dict) for seg in metadata["segments"])
    for value in metadata.values():
        assert not isinstance(value, (MediaFile, Speaker, TranscriptSegment))
