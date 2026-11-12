"""Why each user-triggered task is, or is not, on the replay allowlist (issue #1159).

A replayed task runs a second time, possibly after a first run that got most of the way
through. For the allowlisted search-index tasks that must change nothing: the second run sends
the same writes, derived from Postgres and the arguments alone, with no counter, append or
accumulate in the script. ``process_speaker_merge_background`` averages a voiceprint, which is
not a fixed point by itself, so the average stamps the source it consumed in the same document
write and a replay that finds the stamp does not average again. ``download.media_playlist``
claims each download in Postgres before sending it, so a replay sends nothing twice.

These use a recording OpenSearch client; a live double-run against a real index is a separate,
stack-bound check.
"""

from __future__ import annotations

import contextlib
import re
from types import SimpleNamespace
from typing import Any

import pytest

from app.core import task_replay as tr

#: Painless constructs that make a script depend on the document's previous value.
_ACCUMULATING = re.compile(r"\+=|\+\+|\.add\(|\.addAll\(|\.append\(|\*=")


class RecordingClient:
    """Records every ``update_by_query`` and answers with a fixed response."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.indices = SimpleNamespace(refresh=lambda **_kw: None)

    def update_by_query(self, *, index: str, body: dict[str, Any], **kwargs: Any) -> dict:
        self.bodies.append({"index": index, "body": body, "kwargs": kwargs})
        return {"updated": 1, "version_conflicts": 0}


class _Rows:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def join(self, *_a: Any, **_k: Any) -> _Rows:
        return self

    def filter(self, *_a: Any, **_k: Any) -> _Rows:
        return self

    def all(self) -> list[tuple[Any, ...]]:
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class FakeDb:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def query(self, *_a: Any) -> _Rows:
        return _Rows(self._rows)


def _scope(rows: list[tuple[Any, ...]]):
    @contextlib.contextmanager
    def session_scope():
        yield FakeDb(rows)

    return session_scope


@pytest.fixture
def client(monkeypatch) -> RecordingClient:
    fake = RecordingClient()
    monkeypatch.setattr("app.services.opensearch_service.get_opensearch_client", lambda: fake)
    monkeypatch.setattr("app.services.chat.retrieval_cache.bump_corpus_version", lambda: None)
    return fake


def _assert_second_run_repeats_the_first(
    client: RecordingClient, first: dict, second: dict, *, check_script: bool = True
):
    assert client.bodies, "the task issued no write, so the comparison below proves nothing"
    half = len(client.bodies) // 2
    assert len(client.bodies) == 2 * half
    assert client.bodies[:half] == client.bodies[half:], "a replay wrote something different"
    assert first == second
    for call in client.bodies if check_script else []:
        source = call["body"]["script"]["source"]
        assert not _ACCUMULATING.search(source), f"script depends on the previous value: {source}"


def test_access_index_update_is_a_pure_overwrite(client, monkeypatch):
    from app.services.permission_service import PermissionService
    from app.tasks.search_file_field_tasks import update_file_access_index

    monkeypatch.setattr("app.db.session_utils.session_scope", _scope([(3,)]))
    monkeypatch.setattr(
        PermissionService, "get_users_with_file_access", staticmethod(lambda _db, _fid: [1, 2])
    )

    first = update_file_access_index.run([7])
    second = update_file_access_index.run([7])

    _assert_second_run_repeats_the_first(client, first, second)
    assert first["status"] == "success"
    assert client.bodies[0]["body"]["script"]["params"] == {"ids": [1, 2], "cids": [3]}


def test_tag_index_update_is_a_pure_overwrite(client, monkeypatch):
    from app.tasks.search_file_field_tasks import update_file_tags_index

    monkeypatch.setattr("app.db.session_utils.session_scope", _scope([(7, "alpha"), (7, "beta")]))

    first = update_file_tags_index.run([7])
    second = update_file_tags_index.run([7])

    _assert_second_run_repeats_the_first(client, first, second)
    assert client.bodies[0]["body"]["script"]["params"] == {"tags": ["alpha", "beta"]}


def _patch_rename_context(monkeypatch, *, current_name=None, current_title=None):
    from app.tasks import rename_propagation_task as rp

    monkeypatch.setattr(rp, "_current_speaker_name", lambda _sid: current_name)
    monkeypatch.setattr(rp, "_current_file_title", lambda _uuid: current_title)
    monkeypatch.setattr(rp, "_file_if_voiceprints_off", lambda _uuid: None)
    monkeypatch.setattr(rp, "_finish", lambda *_a, **_k: None)
    monkeypatch.setattr(rp, "_retry_on_conflicts", lambda *_a, **_k: False)
    return rp


def test_speaker_rename_replay_rewrites_the_same_documents(client, monkeypatch):
    rp = _patch_rename_context(monkeypatch, current_name="Alice")

    first = rp.propagate_speaker_rename.run("f-uuid", ["SPEAKER_00"], "Alice", 5)
    second = rp.propagate_speaker_rename.run("f-uuid", ["SPEAKER_00"], "Alice", 5)

    # The roster is rebuilt into a fresh list from the stored one and written only when it
    # differs, so it is not an accumulator even though it calls ``add`` on that new list.
    _assert_second_run_repeats_the_first(client, first, second, check_script=False)
    source = client.bodies[0]["body"]["script"]["source"]
    assert "new ArrayList()" in source and "rebuilt.equals(ctx._source.speakers)" in source
    # Only documents that still carry a stale name are selected, so after a finished run
    # the same request matches nothing: the replay of a completed rename is a no-op.
    assert client.bodies[0]["body"]["script"]["params"]["old_names"] == ["SPEAKER_00"]


def test_title_rename_replay_rewrites_the_same_documents(client, monkeypatch):
    rp = _patch_rename_context(monkeypatch, current_title="Q3 review")

    first = rp.propagate_title_rename.run("f-uuid", "Q3 review")
    second = rp.propagate_title_rename.run("f-uuid", "Q3 review")

    _assert_second_run_repeats_the_first(client, first, second)
    assert client.bodies[0]["body"]["script"]["params"] == {"title": "Q3 review"}


def test_a_replayed_rename_writes_the_name_current_at_replay_time(client, monkeypatch):
    """The replay carries the dispatched name; a rename since then must win, not be undone."""
    rp = _patch_rename_context(monkeypatch, current_title="Newer title")

    rp.propagate_title_rename.run("f-uuid", "Older title")

    assert client.bodies[0]["body"]["script"]["params"] == {"title": "Newer title"}


def test_digest_regeneration_replay_issues_the_same_index_calls(monkeypatch):
    from app.services.search import indexing_service as idx
    from app.tasks import search_indexing_task
    from app.tasks.rename_digest_task import regenerate_rename_digests

    calls: list[tuple[str, int, str]] = []

    class FakeIndexer:
        def _index_digest_plane(self, *, file_id, file_uuid, **_kw):
            calls.append(("digest", file_id, file_uuid))

        def _index_summary_plane(self, *, file_id, file_uuid, **_kw):
            calls.append(("summary", file_id, file_uuid))

    meta: dict[str, Any] = {
        "title": "t",
        "tag_names": [],
        "upload_time": None,
        "language": "en",
        "content_type": "audio/wav",
        "duration": 1.0,
        "file_size": 1,
        "collection_ids": [],
        "accessible_user_ids": [],
        "availability": "available",
    }

    class _Db:
        def query(self, *_a):
            return self

        def filter(self, *_a):
            return self

        def first(self):
            return SimpleNamespace(id=9, user_id=4)

    @contextlib.contextmanager
    def scope():
        yield _Db()

    stubs = {
        (idx, "TranscriptIndexingService"): FakeIndexer,
        ("app.services.search.pipelines", "is_neural_pipeline_available"): lambda: False,
        ("app.db.session_utils", "session_scope"): scope,
        (search_indexing_task, "extract_file_index_metadata"): lambda *_a: meta,
        ("app.services.file_availability", "filter_suppressed_file_uuids"): lambda f, _u: f,
        ("app.utils.websocket_notify", "send_ws_event_for_file"): lambda *_a, **_k: None,
    }
    for (target, attr), stub in stubs.items():
        if isinstance(target, str):
            monkeypatch.setattr(f"{target}.{attr}", stub)
        else:
            monkeypatch.setattr(target, attr, stub)

    first = regenerate_rename_digests.run(["u-1"], "Alice", 5)
    n = len(calls)
    second = regenerate_rename_digests.run(["u-1"], "Alice", 5)

    assert n == 2, "the first run rebuilt neither plane"
    assert calls[:n] == calls[n:], "a replay rebuilt something else"
    assert first == second


def test_every_replayable_user_task_is_pinned_by_a_test_here():
    pinned = {
        "update_file_access_index",
        "update_file_tags_index",
        "propagate_speaker_rename",
        "propagate_title_rename",
        "regenerate_rename_digests",
        "process_speaker_merge_background",
        "process_speaker_update_background",
        "download.media_playlist",
    }
    assert pinned <= tr.REPLAYABLE_TASKS


class _VoiceprintStore:
    """The speaker index as the merge sees it: whole documents keyed by speaker uuid."""

    def __init__(self, monkeypatch, *, failing_removes: int = 0) -> None:
        self.docs: dict[str, dict[str, Any]] = {
            "source": {"embedding": [1.0, 0.0]},
            "target": {"embedding": [0.0, 1.0]},
        }
        self.failing_removes = failing_removes
        for name in ("get_speaker_embedding", "get_speaker_document"):
            monkeypatch.setattr(f"app.services.opensearch_service.{name}", getattr(self, name))
        monkeypatch.setattr("app.services.opensearch_service.add_speaker_embedding", self.add)
        monkeypatch.setattr("app.services.opensearch_service.remove_speaker_embedding", self.remove)

    def get_speaker_embedding(self, uuid: str) -> list[float] | None:
        doc = self.docs.get(uuid)
        return list(doc["embedding"]) if doc else None

    def get_speaker_document(self, uuid: str) -> dict[str, Any] | None:
        doc = self.docs.get(uuid)
        if doc is None:
            return None
        return {
            "embedding": list(doc["embedding"]),
            "segment_count": 1,
            "merged_from": list(doc.get("merged_from") or []),
        }

    def add(self, *, speaker_uuid: str, embedding: list[float], **kw: Any) -> None:
        # A real index write replaces the document, so only what this call sends survives.
        doc: dict[str, Any] = {"embedding": embedding}
        if kw.get("merged_from"):
            doc["merged_from"] = kw["merged_from"]
        self.docs[speaker_uuid] = doc

    def remove(self, uuid: str) -> bool:
        if self.failing_removes:
            self.failing_removes -= 1
            raise ConnectionError("worker lost before the source delete")
        return self.docs.pop(uuid, None) is not None


_MERGE_TARGET = {
    "uuid": "target",
    "id": 2,
    "user_id": 1,
    "name": "n",
    "profile_id": None,
    "media_file_id": None,
    "display_name": None,
}


def test_the_speaker_merge_voiceprint_average_is_a_fixed_point(monkeypatch):
    """A merge replayed after it finished changes nothing."""
    from app.api.endpoints import speakers_merge

    store = _VoiceprintStore(monkeypatch)

    speakers_merge._merge_speaker_embeddings("source", _MERGE_TARGET)
    after_first = list(store.docs["target"]["embedding"])
    speakers_merge._merge_speaker_embeddings("source", _MERGE_TARGET)

    assert after_first == [0.5, 0.5]
    assert store.docs["target"]["embedding"] == after_first
    assert "source" not in store.docs


def test_a_merge_lost_between_the_average_and_the_source_delete_is_not_averaged_twice(
    monkeypatch,
):
    """The average landed, the source delete did not; the replay must not average it in again."""
    from app.api.endpoints import speakers_merge

    store = _VoiceprintStore(monkeypatch, failing_removes=1)

    speakers_merge._merge_speaker_embeddings("source", _MERGE_TARGET)
    assert "source" in store.docs, "the simulated loss did not leave the source behind"
    assert store.docs["target"]["embedding"] == [0.5, 0.5]

    speakers_merge._merge_speaker_embeddings("source", _MERGE_TARGET)

    # A second average would have written 0.25 * target + 0.75 * source = [0.75, 0.25].
    assert store.docs["target"]["embedding"] == [0.5, 0.5]
    assert store.docs["target"]["merged_from"] == ["source"]
    assert "source" not in store.docs


def test_a_later_merge_into_the_same_target_keeps_the_earlier_stamp(monkeypatch):
    """A replay of the first merge that runs after a second one must still see its stamp."""
    from app.api.endpoints import speakers_merge

    store = _VoiceprintStore(monkeypatch, failing_removes=1)
    store.docs["other"] = {"embedding": [1.0, 1.0]}

    speakers_merge._merge_speaker_embeddings("source", _MERGE_TARGET)
    speakers_merge._merge_speaker_embeddings("other", _MERGE_TARGET)
    settled = list(store.docs["target"]["embedding"])
    speakers_merge._merge_speaker_embeddings("source", _MERGE_TARGET)

    assert store.docs["target"]["merged_from"] == ["source", "other"]
    assert store.docs["target"]["embedding"] == settled


def test_the_speaker_write_stores_the_merge_stamp_in_the_same_document(monkeypatch):
    from app.services.opensearch_service import client as os_client
    from app.services.opensearch_service import speaker_write

    indexed: list[dict[str, Any]] = []

    def index(**kw: Any) -> dict[str, Any]:
        indexed.append(kw["body"])
        return {}

    monkeypatch.setattr(os_client, "opensearch_client", SimpleNamespace(index=index))
    monkeypatch.setattr(speaker_write, "ensure_indices_exist", lambda: None)
    monkeypatch.setattr(speaker_write, "assert_voiceprint_write_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(speaker_write, "availability_by_file", lambda _ids: {})
    voice = [0.5] * 256

    speaker_write.add_speaker_embedding(2, "t", 1, "n", voice, merged_from=["s"])
    speaker_write.add_speaker_embedding(2, "t", 1, "n", voice)

    assert indexed[0]["merged_from"] == ["s"]
    assert indexed[0]["embedding"] == voice
    assert "merged_from" not in indexed[1]


def test_the_speaker_document_read_returns_the_merge_stamp(monkeypatch):
    from app.services.opensearch_service import client as os_client
    from app.services.opensearch_service import speaker_read

    docs = {
        "stamped": {"embedding": [1.0], "merged_from": ["s"]},
        "plain": {"embedding": [1.0]},
    }
    fake = SimpleNamespace(get=lambda **kw: {"_source": docs[kw["id"]]})
    monkeypatch.setattr(os_client, "opensearch_client", fake)
    monkeypatch.setattr(speaker_read, "ensure_indices_exist", lambda: None)

    stamped = speaker_read.get_speaker_document("stamped")
    plain = speaker_read.get_speaker_document("plain")
    assert stamped is not None and plain is not None
    assert stamped["merged_from"] == ["s"]
    assert plain["merged_from"] == []


def test_a_replayed_matching_pass_does_not_relabel_a_speaker_it_already_labeled(monkeypatch):
    """The speaker update's matching pass, run twice: the second run writes no identity again.

    Re-applying a match restamps ``identity_source_at``, which appends a second "confirmed"
    row to the append-only identity history, and re-touches the profile. Everything else the
    pass writes is an overwrite with the same value (``record_suggestion``), a keep-the-max
    upsert (``store_speaker_match``) or a rebuild from Postgres (``_update_profile_embedding``).
    """
    import uuid as uuid_module
    from unittest.mock import create_autospec

    from sqlalchemy.orm import Session

    from app.api.endpoints import speaker_update as su
    from app.models.speaker import Speaker
    from app.services.speaker_rename_tracker import SpeakerRenameTracker

    speaker = Speaker(
        id=5,
        uuid=uuid_module.uuid4(),
        user_id=1,
        media_file_id=3,
        name="SPEAKER_01",
        verified=False,
    )
    side_effects: list[str] = []
    monkeypatch.setattr(su.speaker_lookups, "speakers_by_id", lambda _db, _ids: {5: speaker})
    monkeypatch.setattr(su, "store_speaker_match", lambda *_a: side_effects.append("match"))
    monkeypatch.setattr(su, "touch_profile_interaction", lambda *_a: side_effects.append("touch"))
    monkeypatch.setattr(su, "_update_profile_embedding", lambda *_a: side_effects.append("rebuild"))
    monkeypatch.setattr(su, "_get_profile_uuid", lambda _db, _pid: "profile-uuid")
    trigger = {
        "id": 1,
        "uuid": "trigger-uuid",
        "user_id": 1,
        "organization_id": None,
        "display_name": "Dana Lee",
        "profile_id": 7,
        "profile_uuid": "profile-uuid",
        "embedding_model": "m",
    }
    scored = [{"id": 5, "similarity": 0.9}]
    db = create_autospec(Session, instance=True)

    first = su._persist_match_results(db, trigger, scored, SpeakerRenameTracker())
    stamped_at = speaker.identity_source_at
    second = su._persist_match_results(db, trigger, scored, SpeakerRenameTracker())

    assert first == second == (1, 0, [first[2][0]])
    assert (speaker.display_name, speaker.profile_id, speaker.verified) == ("Dana Lee", 7, True)
    assert stamped_at is not None
    assert speaker.identity_source_at is stamped_at, "the replay restamped the identity"
    assert side_effects == ["match", "touch", "rebuild", "match"]


def test_the_speaker_labeling_workflow_run_twice_changes_nothing_the_second_time(
    db_session, normal_user, monkeypatch
):
    """``process_speaker_update_background``'s matching step against Postgres, run twice."""
    import uuid as uuid_module

    from app.api.endpoints import speaker_update as su
    from app.api.endpoints.speakers_profile_workflow import _handle_speaker_labeling_workflow
    from app.models.media import MediaFile
    from app.models.speaker import Speaker
    from app.models.speaker import SpeakerMatch
    from app.models.speaker import SpeakerProfile
    from app.models.speaker_identity_event import SpeakerIdentityEvent

    files = [
        MediaFile(
            user_id=normal_user.id,
            filename=f"replay-{n}.wav",
            storage_path=f"test/{uuid_module.uuid4().hex}.wav",
            file_size=1,
            content_type="audio/wav",
            status="completed",
        )
        for n in range(2)
    ]
    db_session.add_all(files)
    db_session.commit()
    labeled = Speaker(
        user_id=normal_user.id,
        media_file_id=files[0].id,
        name="SPEAKER_00",
        display_name="Dana Lee",
    )
    other = Speaker(user_id=normal_user.id, media_file_id=files[1].id, name="SPEAKER_00")
    db_session.add_all([labeled, other])
    db_session.commit()
    monkeypatch.setattr(su, "get_speaker_embedding", lambda _uuid: [1.0, 0.0, 0.0])
    monkeypatch.setattr(su, "_push_speaker_documents", lambda _docs: None)
    monkeypatch.setattr(su, "_send_bulk_update_notification", lambda *_a: None)

    def snapshot() -> tuple:
        db_session.expire_all()
        row = db_session.get(Speaker, other.id)
        assert row is not None
        events = (
            db_session.query(SpeakerIdentityEvent)
            .filter(SpeakerIdentityEvent.speaker_id.in_([labeled.id, other.id]))
            .count()
        )
        profiles = (
            db_session.query(SpeakerProfile)
            .filter(SpeakerProfile.user_id == normal_user.id, SpeakerProfile.name == "Dana Lee")
            .count()
        )
        matches = db_session.query(SpeakerMatch).filter(SpeakerMatch.speaker2_id == other.id)
        return (
            row.display_name,
            row.profile_id,
            row.verified,
            row.identity_source,
            row.identity_source_at,
            events,
            profiles,
            matches.count(),
        )

    first = _handle_speaker_labeling_workflow(db_session, labeled.id, "Dana Lee", normal_user.id)
    after_first = snapshot()
    second = _handle_speaker_labeling_workflow(db_session, labeled.id, "Dana Lee", normal_user.id)

    assert first == second == {"auto_applied_count": 1, "suggested_count": 0}
    assert after_first[0] == "Dana Lee" and after_first[6] == 1
    assert snapshot() == after_first


def _placeholder(**over):
    from app.models.media import FileStatus

    meta = {"from_playlist": True, "playlist_id": "PL1"}
    meta.update(over.pop("meta", {}))
    return SimpleNamespace(
        **{"status": FileStatus.QUEUED, "storage_path": "", "metadata_raw": meta, **over}
    )


def test_a_replayed_playlist_adopts_placeholders_it_never_dispatched():
    from app.services.media_download_playlist import _is_unstarted_playlist_placeholder

    info = {"playlist_id": "PL1"}
    assert _is_unstarted_playlist_placeholder(_placeholder(), info)


def test_a_replayed_playlist_leaves_dispatched_or_foreign_placeholders_alone():
    from app.models.media import FileStatus
    from app.services.media_download_playlist import _is_unstarted_playlist_placeholder

    info = {"playlist_id": "PL1"}
    assert not _is_unstarted_playlist_placeholder(
        _placeholder(meta={"download_dispatched": True}), info
    )
    assert not _is_unstarted_playlist_placeholder(_placeholder(status=FileStatus.DOWNLOADING), info)
    assert not _is_unstarted_playlist_placeholder(_placeholder(meta={"playlist_id": "PL2"}), info)
    assert not _is_unstarted_playlist_placeholder(_placeholder(meta={"from_playlist": False}), info)


def _dispatch_with(monkeypatch, *, claimed: bool) -> tuple[bool, list[str], list[Any]]:
    """Dispatch one placeholder, recording the order of the claim, its commit and the send."""
    from unittest.mock import create_autospec

    from sqlalchemy.orm import Session

    from app.models.media import MediaFile
    from app.tasks import media_playlist_processing as mpp

    events: list[str] = []
    statements: list[Any] = []
    monkeypatch.setattr(
        mpp.process_youtube_url_task, "apply_async", lambda **_kw: events.append("send")
    )

    def execute(statement: Any) -> SimpleNamespace:
        statements.append(statement)
        events.append("claim")
        return SimpleNamespace(scalar_one_or_none=lambda: 7 if claimed else None)

    db = create_autospec(Session, instance=True)
    db.execute.side_effect = execute
    db.commit.side_effect = lambda: events.append("commit")
    mf = MediaFile(id=7, source_url="https://x", title="t", metadata_raw={"a": 1})
    return mpp._dispatch_playlist_video_task(mf, 1, db), events, statements


def test_playlist_dispatch_commits_its_claim_before_the_download_is_sent(monkeypatch):
    """A loss after the send cannot leave the placeholder unmarked for a replay to resend."""
    from sqlalchemy.dialects import postgresql

    dispatched, events, statements = _dispatch_with(monkeypatch, claimed=True)

    assert dispatched is True
    assert events == ["claim", "commit", "send"]
    (claim,) = statements
    sql = str(claim.compile(dialect=postgresql.dialect()))
    # Conditional on the mark being absent, and it adds the mark: one atomic statement.
    assert sql.startswith("UPDATE media_file SET metadata_raw=")
    assert "coalesce(media_file.metadata_raw" in sql and " ? " in sql and " || " in sql
    assert sql.endswith("RETURNING media_file.id")


def test_a_placeholder_another_run_already_claimed_is_not_sent_again(monkeypatch):
    """Two overlapping playlist runs (a replay beside a slow original) send one download."""
    dispatched, events, _statements = _dispatch_with(monkeypatch, claimed=False)

    assert dispatched is False
    assert events == ["claim", "commit"]


def test_dispatching_one_placeholder_twice_sends_one_download(db_session, normal_user, monkeypatch):
    """The claim against Postgres: the second dispatch of the same placeholder finds the mark."""
    import uuid as uuid_module

    from app.models.media import FileStatus
    from app.models.media import MediaFile
    from app.tasks import media_playlist_processing as mpp

    sent: list[Any] = []
    monkeypatch.setattr(
        mpp.process_youtube_url_task, "apply_async", lambda **kw: sent.append(kw["args"])
    )
    mf = MediaFile(
        uuid=uuid_module.uuid4(),
        user_id=normal_user.id,
        filename="video.mp4",
        title="t",
        storage_path="",
        file_size=0,
        content_type="video/mp4",
        status=FileStatus.QUEUED,
        source_url="https://youtu.be/abc123",
        metadata_raw={"from_playlist": True},
    )
    db_session.add(mf)
    db_session.commit()

    assert mpp._dispatch_playlist_video_task(mf, normal_user.id, db_session) is True
    assert mpp._dispatch_playlist_video_task(mf, normal_user.id, db_session) is False

    db_session.refresh(mf)
    assert len(sent) == 1
    assert mf.metadata_raw is not None
    assert mf.metadata_raw["from_playlist"] is True
    assert mf.metadata_raw[mpp.DOWNLOAD_DISPATCHED_KEY] is True
    assert mf.status == FileStatus.QUEUED
