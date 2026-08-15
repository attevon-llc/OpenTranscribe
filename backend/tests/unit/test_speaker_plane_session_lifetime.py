# mypy: disable-error-code="arg-type"
# This suite passes structural stand-ins (a fake session, plain dicts) to signatures
# that declare Session/Speaker. Declared once here rather than as a cast at every call
# site — same convention as tests/unit/test_task_session_lifetime.py.
"""The speaker plane must not hold a DB transaction across OpenSearch or MinIO work.

Sibling of ``test_task_session_lifetime.py``. That module covers the Celery tasks
found in the first two sweeps; this one covers the sixteen findings
``scripts/audit-session-lifetime.py`` catalogued in the **speaker** plane, which
had been allowlisted as ``BACKLOG`` rather than fixed:

===========================================================  =========================
scope                                                        slow work formerly inside
===========================================================  =========================
``process_speaker_update_background``                        3 OpenSearch fan-outs + a
                                                             MinIO cache purge, all in
                                                             ONE ``session_scope``
``process_speaker_merge_background``                         voiceprint average, MinIO
                                                             purge, index merge, ditto
``speakers._sync_profile_rename_to_opensearch``              1 index write per linked
                                                             speaker on the caller's
                                                             session
``speakers._clear_video_cache_for_speaker`` / ``_clear_speaker_video_cache``
                                                             storage client + object
                                                             deletes on the caller's
                                                             session
``speakers.debug_cross_media_data``                          2 index searches on the
                                                             REQUEST session
``speaker_update._apply_high_confidence_match``              index write per match
``speaker_update._sync_suggestion_speakers_to_opensearch``   index write per suggestion
``speaker_update.trigger_retroactive_matching``              1 voiceprint READ per
                                                             speaker the user owns,
                                                             interleaved with writes
``smart_speaker_suggestion_service._get_profile_suggestions_optimized``
``smart_speaker_suggestion_service.consolidate_suggestions_batch``
                                                             kNN probes reachable with
                                                             a caller session open
===========================================================  =========================

Why it matters (measured, twice in one day, on two different workers): a plain
SELECT takes ACCESS SHARE for the life of its transaction, so such a hold queues
every ``ALTER TABLE`` — i.e. it hangs an Alembic upgrade, and dev runs migrations
automatically on backend startup — pins the vacuum horizon on
``transcript_segment``, and burns a pool connection.

Two shapes of test, because the plane has two shapes of session:

* **Tasks** own their sessions. Those tests swap the module's ``session_scope``
  for the depth-tracking stand-in from ``test_task_session_lifetime`` and have
  every slow-call stub report the open-scope depth *at the moment it runs*. Each
  also asserts at least two scopes were opened, so a task that stopped touching
  the DB entirely cannot pass.
* **Request/caller-owned sessions** (``trigger_retroactive_matching``,
  ``_sync_profile_rename_to_opensearch``, ``debug_cross_media_data``) cannot open
  or close a scope — the fix there is to finish the DB work and **commit** before
  the network hop. Those tests count ``commit()`` calls on the very session the
  function was handed and assert each slow call ran in a LATER commit generation
  than the DB work it depends on. Under the savepoint harness a commit does not
  end the outer transaction, so the generation counter is the observable proxy;
  it is falsifiable in exactly the way that matters — move the push back above the
  commit and the counts collapse.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
import uuid as uuid_mod
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.api.endpoints import speaker_update as su
from app.api.endpoints import speakers as spk
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerProfile
from app.services import smart_speaker_suggestion_service as smart
from app.tasks import speaker_merge_task as smt
from app.tasks import speaker_update_task as spt
from tests.unit.test_task_session_lifetime import _leak
from tests.unit.test_task_session_lifetime import _ScopeTracker

_AUDITOR = Path(__file__).resolve().parents[3] / "scripts" / "audit-session-lifetime.py"

#: EMPTY, and that is the point. This held
#: ``ProfileEmbeddingService.calculate_profile_similarity``, whose ``db`` parameter was
#: DEAD — nothing in the body read it — so the fix was to delete the parameter, not to
#: restructure anything. Closing it made this test fail, which forced the allowlist line
#: to be deleted in the same change. Exactly what the set is for: add a scope here only
#: while it genuinely cannot be fixed, and let closing it break the test.
_KNOWN_RESIDUAL_SCOPES: set[str] = set()

_SPEAKER_PLANE_FILES = (
    "api/endpoints/speakers.py",
    "api/endpoints/speaker_update.py",
    "tasks/speaker_update_task.py",
    "tasks/speaker_merge_task.py",
    "services/smart_speaker_suggestion_service.py",
    "services/profile_embedding_service.py",
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
class _CommitTracker:
    """Records the ``commit()`` generation each slow call runs in.

    The scope-depth stand-in cannot be used where the session is the CALLER's: the
    function under test never opens one. What it can do — and what the fix does —
    is finish its statements and commit, releasing ACCESS SHARE, before the network
    call. Two calls sharing a generation ran inside one transaction; different
    generations prove they did not.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._real_commit = session.commit
        self.commits = 0
        self.timeline: list[tuple[str, int]] = []

    def commit(self) -> None:
        self.commits += 1
        self._real_commit()

    def observe(self, label: str) -> None:
        self.timeline.append((label, self.commits))

    def at(self, label: str) -> list[int]:
        """Commit generations in which ``label`` was observed."""
        return [generation for name, generation in self.timeline if name == label]


def _held(tracker: _CommitTracker, label: str) -> str:
    return f"a DB transaction was still open across the slow phase ({label}): {tracker.timeline}"


def _make_file(db_session, user, name: str = "meeting") -> MediaFile:
    media_file = MediaFile(
        uuid=str(uuid_mod.uuid4()),
        user_id=user.id,
        filename=f"{name}.mp4",
        storage_path=f"test/{name}-{uuid_mod.uuid4().hex[:8]}.mp4",
        content_type="video/mp4",
        file_size=1000,
    )
    db_session.add(media_file)
    db_session.flush()
    return media_file


def _make_profile(db_session, user, name: str = "Ada") -> SpeakerProfile:
    profile = SpeakerProfile(
        uuid=str(uuid_mod.uuid4()), user_id=user.id, name=f"{name}-{uuid_mod.uuid4().hex[:6]}"
    )
    db_session.add(profile)
    db_session.flush()
    return profile


def _make_speaker(db_session, user, media_file, name="SPEAKER_00", **kwargs) -> Speaker:
    speaker = Speaker(
        uuid=str(uuid_mod.uuid4()),
        media_file_id=media_file.id,
        user_id=user.id,
        name=name,
        **kwargs,
    )
    db_session.add(speaker)
    db_session.flush()
    return speaker


# --------------------------------------------------------------------------- #
# 1. process_speaker_update_background — 3 OpenSearch fan-outs + a MinIO purge
# --------------------------------------------------------------------------- #
@pytest.fixture
def update_task_env(db_session, monkeypatch):
    """Patch the update task's slow seams so each reports the open-scope depth."""
    tracker = _ScopeTracker(db_session)
    recorded: dict = {}

    monkeypatch.setattr(spt, "session_scope", tracker.scope)

    def _embeddings(db, *args, **kwargs):
        # Legitimately DB work (it writes profile rows) — belongs INSIDE a scope.
        tracker.observe("profile_embeddings")
        recorded["embedding_args"] = args

    monkeypatch.setattr(spk, "_handle_profile_embedding_updates", _embeddings)

    def _speaker_name(speaker_uuid, display_name):
        tracker.observe("os_speaker_name")
        recorded["speaker_name"] = (speaker_uuid, display_name)

    monkeypatch.setattr(spk, "_update_opensearch_speaker_name", _speaker_name)

    def _profile_info(payload):
        tracker.observe("os_profile_info")
        recorded["profile_info"] = payload

    monkeypatch.setattr(spk, "_push_speaker_profile_info", _profile_info)

    def _display_names(rows):
        tracker.observe("os_profile_rename")
        recorded["rename_rows"] = rows

    monkeypatch.setattr(spk, "_push_speaker_display_names", _display_names)

    def _workflow(db, speaker_id, display_name):
        # Also DB work: it commits the profile assignment before matching.
        tracker.observe("labeling_workflow")
        recorded["workflow"] = (speaker_id, display_name)
        return {"auto_applied_count": 2, "suggested_count": 1}

    monkeypatch.setattr(spk, "_handle_speaker_labeling_workflow", _workflow)

    def _cache(media_file_id):
        tracker.observe("clear_video_cache")
        recorded["cache_file_id"] = media_file_id

    monkeypatch.setattr(spk, "_clear_video_cache_for_speaker", _cache)
    monkeypatch.setattr(
        "app.utils.websocket_notify.send_ws_event",
        lambda user_id, event, data: recorded.setdefault("ws", []).append((event, data)),
    )

    tracker.recorded = recorded
    return tracker


def test_speaker_update_task_syncs_the_index_outside_the_session(
    db_session, normal_user, update_task_env
):
    """The regression: every OpenSearch/MinIO step runs with zero scopes open."""
    tracker = update_task_env
    profile = _make_profile(db_session, normal_user)
    media_file = _make_file(db_session, normal_user)
    speaker = _make_speaker(
        db_session, normal_user, media_file, profile_id=profile.id, display_name="Ada"
    )

    result = spt.process_speaker_update_background.apply(
        kwargs={
            "speaker_uuid": str(speaker.uuid),
            "user_id": normal_user.id,
            "display_name": "Ada",
            "speaker_id": speaker.id,
            "old_profile_id": None,
            "new_profile_id": profile.id,
            "was_auto_labeled": False,
            "display_name_changed": True,
            "media_file_id": media_file.id,
            "renamed_profile_id": profile.id,
        }
    ).get()

    assert result["status"] == "success", result

    observed = tracker.seen
    for label in (
        "os_speaker_name",
        "os_profile_info",
        "os_profile_rename",
        "clear_video_cache",
    ):
        assert observed[label] == 0, _leak(tracker, label)

    # The two genuinely DB-bound steps are *supposed* to be inside a scope.
    # Pinning that stops the fix regressing into "the task stopped using the DB",
    # which would satisfy the assertions above for the wrong reason.
    assert observed["profile_embeddings"] == 1, tracker.observations
    assert observed["labeling_workflow"] == 1, tracker.observations

    assert tracker.opened >= 4, (
        "expected separate scopes for the plan, the embedding update, the rename "
        f"read, the labeling workflow and the notification read; got {tracker.opened}"
    )
    assert tracker.max_depth == 1, "session scopes must not nest"
    assert tracker.depth == 0

    # The read phase really did produce what the index phases were sent, so
    # "zero scopes open" cannot be satisfied by sending nothing.
    recorded = tracker.recorded
    assert recorded["speaker_name"] == (str(speaker.uuid), "Ada")
    assert recorded["profile_info"]["speaker_uuid"] == str(speaker.uuid)
    assert recorded["profile_info"]["profile_uuid"] == str(profile.uuid)
    assert recorded["rename_rows"] == [(str(speaker.uuid), "Ada")]
    assert recorded["cache_file_id"] == media_file.id
    assert recorded["workflow"] == (speaker.id, "Ada")

    # ...and the completion event carries the identifiers re-read afterwards.
    event, data = recorded["ws"][0]
    assert event == "speaker_processing_complete"
    assert data["profile_id"] == str(profile.uuid)
    assert data["media_file_id"] == str(media_file.uuid)
    assert result["auto_applied_count"] == 2


def test_speaker_update_read_phase_returns_plain_data(db_session, normal_user, update_task_env):
    """``_load_update_plan`` must not hand back live ORM instances.

    An instance escaping the scope can lazy-load during the OpenSearch phase and
    silently reopen a transaction, reintroducing the leak invisibly.
    """
    tracker = update_task_env
    profile = _make_profile(db_session, normal_user)
    media_file = _make_file(db_session, normal_user)
    speaker = _make_speaker(
        db_session, normal_user, media_file, profile_id=profile.id, display_name="Ada"
    )

    plan = spt._load_update_plan(str(speaker.uuid), None, True)

    assert tracker.depth == 0
    assert plan is not None
    assert plan["display_name"] == "Ada"
    assert plan["profile_id"] == profile.id
    assert plan["profile_sync"]["profile_uuid"] == str(profile.uuid)
    assert plan["profile_sync"]["verified"] is False
    for value in plan.values():
        assert not isinstance(value, (MediaFile, Speaker, SpeakerProfile))
    for value in plan["profile_sync"].values():
        assert not isinstance(value, (MediaFile, Speaker, SpeakerProfile))


def test_speaker_update_plan_skips_the_profile_write_when_nothing_changed(
    db_session, normal_user, update_task_env
):
    """``profile_sync`` is ``None`` when neither name nor profile moved.

    Same early return the pre-split ``_update_opensearch_profile_info`` made — the
    split must not turn a no-op into an index write.
    """
    profile = _make_profile(db_session, normal_user)
    media_file = _make_file(db_session, normal_user)
    speaker = _make_speaker(db_session, normal_user, media_file, profile_id=profile.id)

    plan = spt._load_update_plan(str(speaker.uuid), profile.id, False)

    assert plan is not None
    assert plan["profile_sync"] is None


# --------------------------------------------------------------------------- #
# 2. process_speaker_merge_background — voiceprint average + MinIO + index merge
# --------------------------------------------------------------------------- #
@pytest.fixture
def merge_task_env(db_session, monkeypatch):
    tracker = _ScopeTracker(db_session)
    recorded: dict = {}

    monkeypatch.setattr(smt, "session_scope", tracker.scope)

    def _merge_embeddings(source_speaker_uuid, target):
        # Two OpenSearch reads, a numpy mean and an index write.
        tracker.observe("voiceprint_average")
        recorded["merge_args"] = (source_speaker_uuid, target)

    monkeypatch.setattr(spk, "_merge_speaker_embeddings", _merge_embeddings)

    def _cache(affected):
        tracker.observe("clear_video_cache")
        recorded["cache_files"] = set(affected)

    monkeypatch.setattr(spk, "_clear_speaker_video_cache", _cache)

    def _index_merge(source_uuid, target_uuid):
        tracker.observe("index_merge")
        recorded["index_merge"] = (source_uuid, target_uuid)

    monkeypatch.setattr(spk, "_update_opensearch_speaker_merge", _index_merge)
    monkeypatch.setattr(
        spk,
        "_update_profile_embeddings_after_merge",
        lambda db, *a: tracker.observe("profile_embeddings"),
    )
    monkeypatch.setattr(
        spk,
        "_refresh_analytics_after_merge",
        lambda db, *a: tracker.observe("analytics"),
    )
    monkeypatch.setattr(
        "app.utils.websocket_notify.send_ws_event",
        lambda user_id, event, data: recorded.setdefault("ws", []).append((event, data)),
    )

    tracker.recorded = recorded
    return tracker


def test_speaker_merge_task_runs_index_and_storage_work_outside_the_session(
    db_session, normal_user, merge_task_env
):
    tracker = merge_task_env
    profile = _make_profile(db_session, normal_user)
    source_file = _make_file(db_session, normal_user, "merge-source")
    target_file = _make_file(db_session, normal_user, "merge-target")
    target = _make_speaker(
        db_session,
        normal_user,
        target_file,
        "SPEAKER_01",
        profile_id=profile.id,
        display_name="Ada",
    )
    source_uuid = str(uuid_mod.uuid4())

    result = smt.process_speaker_merge_background.apply(
        kwargs={
            "source_speaker_uuid": source_uuid,
            "target_speaker_uuid": str(target.uuid),
            "user_id": normal_user.id,
            "source_speaker_id": 424242,
            "source_profile_id": None,
            "target_profile_id": profile.id,
            "media_file_ids": [source_file.id, target_file.id],
        }
    ).get()

    assert result["status"] == "success", result

    observed = tracker.seen
    for label in ("voiceprint_average", "clear_video_cache", "index_merge"):
        assert observed[label] == 0, _leak(tracker, label)

    # The Postgres-side follow-ups belong inside a (short) scope.
    assert observed["profile_embeddings"] == 1, tracker.observations
    assert observed["analytics"] == 1, tracker.observations
    assert tracker.opened >= 2, f"expected a read scope and a write scope, got {tracker.opened}"
    assert tracker.max_depth == 1, "session scopes must not nest"
    assert tracker.depth == 0

    # The survivor reached the OpenSearch phase as PLAIN DATA — a live Speaker
    # would lazy-load mid-round-trip and open a transaction underneath it.
    merged_source, merged_target = tracker.recorded["merge_args"]
    assert merged_source == source_uuid
    assert not isinstance(merged_target, Speaker)
    assert merged_target["uuid"] == str(target.uuid)
    assert merged_target["id"] == target.id
    assert merged_target["profile_id"] == profile.id
    assert tracker.recorded["cache_files"] == {source_file.id, target_file.id}
    assert tracker.recorded["index_merge"] == (source_uuid, str(target.uuid))

    event, data = tracker.recorded["ws"][0]
    assert event == "speaker_processing_complete"
    assert data["media_file_id"] == str(target_file.uuid)


def test_merge_plan_returns_plain_data(db_session, normal_user, merge_task_env):
    tracker = merge_task_env
    profile = _make_profile(db_session, normal_user)
    media_file = _make_file(db_session, normal_user)
    target = _make_speaker(
        db_session, normal_user, media_file, profile_id=profile.id, display_name="Ada"
    )

    plan = smt._load_merge_plan(str(target.uuid))

    assert tracker.depth == 0
    assert plan is not None
    assert plan["uuid"] == str(target.uuid)
    assert plan["media_file_uuid"] == str(media_file.uuid)
    assert plan["display_name"] == "Ada"
    for value in plan.values():
        assert not isinstance(value, (MediaFile, Speaker, SpeakerProfile))


# --------------------------------------------------------------------------- #
# 3. _clear_video_cache_for_speaker — its OWN short session, storage client first
# --------------------------------------------------------------------------- #
def test_clear_video_cache_opens_its_own_short_session(db_session, normal_user, monkeypatch):
    """The purge no longer runs on the task's transaction.

    NO residual remains. The filename is resolved in a short scope this helper owns,
    that scope CLOSES, and the deletes then go through ``clear_derived_cache``, which
    takes no session at all — so this pins that the purge runs at depth 0, and that the
    storage client is constructed before any scope exists. The session-taking
    ``clear_cache_for_media_file`` it used to call has been deleted.
    """
    from app.services import minio_service as minio_mod
    from app.services import video_processing_service as vps

    tracker = _ScopeTracker(db_session)
    recorded: dict = {}
    media_file = _make_file(db_session, normal_user)

    monkeypatch.setattr("app.db.session_utils.session_scope", tracker.scope)

    class _FakeMinIO:
        def __init__(self):
            tracker.observe("storage_client")

    class _FakeVideoService:
        def __init__(self, minio_service):
            recorded["storage"] = minio_service

        def clear_derived_cache(self, file_id, filename):
            tracker.observe("clear_cache")
            recorded["file_id"] = file_id
            recorded["filename"] = filename

    monkeypatch.setattr(minio_mod, "MinIOService", _FakeMinIO)
    monkeypatch.setattr(vps, "VideoProcessingService", _FakeVideoService)

    spk._clear_video_cache_for_speaker(media_file.id)

    assert tracker.seen["storage_client"] == 0, _leak(tracker, "storage_client")
    assert tracker.seen["clear_cache"] == 0, _leak(tracker, "clear_cache")
    assert tracker.opened == 1, f"expected exactly one short session, got {tracker.opened}"
    assert tracker.max_depth == 1
    assert tracker.depth == 0
    assert recorded["file_id"] == media_file.id
    assert isinstance(recorded["storage"], _FakeMinIO)


def test_clear_speaker_video_cache_uses_one_session_per_file(db_session, normal_user, monkeypatch):
    """The merge path purges each file in its OWN transaction, not one across both."""
    from app.services import minio_service as minio_mod
    from app.services import video_processing_service as vps

    tracker = _ScopeTracker(db_session)
    purged: list[int] = []
    first = _make_file(db_session, normal_user, "a")
    second = _make_file(db_session, normal_user, "b")

    monkeypatch.setattr("app.db.session_utils.session_scope", tracker.scope)

    class _FakeVideoService:
        def __init__(self, minio_service):
            pass

        def clear_derived_cache(self, file_id, filename):
            tracker.observe("clear_cache")
            purged.append(file_id)

    monkeypatch.setattr(minio_mod, "MinIOService", lambda: object())
    monkeypatch.setattr(vps, "VideoProcessingService", _FakeVideoService)

    spk._clear_speaker_video_cache({first.id, second.id})

    assert sorted(purged) == sorted([first.id, second.id])
    # Each purge runs at depth 0 — the per-file read scope closes before it. The scope
    # COUNT still has to differ per file, or one transaction is spanning the whole merge.
    assert tracker.seen["clear_cache"] == 0, _leak(tracker, "clear_cache")
    counts = tracker.opened_at("clear_cache")
    assert len(set(counts)) == len(counts), (
        "both files were purged after the SAME session — one read transaction is "
        f"spanning the whole merge: {tracker.timeline}"
    )
    assert tracker.opened == 2, f"expected one short session per file, got {tracker.opened}"
    assert tracker.depth == 0


# --------------------------------------------------------------------------- #
# 4. trigger_retroactive_matching — a CALLER-owned session (commit generations)
# --------------------------------------------------------------------------- #
@pytest.fixture
def retroactive_env(db_session, monkeypatch):
    tracker = _CommitTracker(db_session)
    recorded: dict[str, Any] = {"embeddings": {}, "pushed_names": [], "pushed_profiles": []}

    monkeypatch.setattr(db_session, "commit", tracker.commit)

    def _get_embedding(speaker_uuid):
        # One OpenSearch read per candidate speaker the user owns.
        tracker.observe("voiceprint_read")
        return recorded["embeddings"].get(str(speaker_uuid))

    monkeypatch.setattr(su, "get_speaker_embedding", _get_embedding)
    monkeypatch.setattr(
        su,
        "_update_profile_embedding",
        lambda db, speaker_id, profile_id: tracker.observe("profile_embedding_write"),
    )
    monkeypatch.setattr(su, "_send_bulk_update_notification", lambda *a: None)

    # The snapshot is the LAST thing that legitimately needs the session. Recording
    # its generation is what makes "the push came later" a real claim rather than
    # "the push came after some commit" — a push moved back inside the collecting
    # loop still lands after an earlier commit and would otherwise pass.
    real_document = su._speaker_sync_document

    def _document(db, speaker):
        tracker.observe("document_snapshot")
        return real_document(db, speaker)

    monkeypatch.setattr(su, "_speaker_sync_document", _document)

    def _push_name(speaker_uuid, display_name):
        tracker.observe("index_push")
        recorded["pushed_names"].append((speaker_uuid, display_name))

    def _push_profile(**kwargs):
        recorded["pushed_profiles"].append(kwargs)

    monkeypatch.setattr("app.services.opensearch_service.update_speaker_display_name", _push_name)
    monkeypatch.setattr("app.services.opensearch_service.update_speaker_profile", _push_profile)

    return tracker, recorded


def test_retroactive_matching_reads_and_pushes_with_the_transaction_committed(
    db_session, normal_user, retroactive_env
):
    """Voiceprint reads and index writes must not share a transaction with the writes.

    Before the split this ran read → (per speaker: OpenSearch read, Postgres write,
    OpenSearch write) → commit, so a single transaction stayed open across one
    network round trip per speaker in the user's whole library.
    """
    tracker, recorded = retroactive_env
    profile = _make_profile(db_session, normal_user)
    media_file = _make_file(db_session, normal_user)

    trigger = _make_speaker(
        db_session, normal_user, media_file, "SPEAKER_00", profile_id=profile.id
    )
    trigger.display_name = "Ada"
    match = _make_speaker(db_session, normal_user, media_file, "SPEAKER_01")
    stranger = _make_speaker(db_session, normal_user, media_file, "SPEAKER_02")
    db_session.flush()

    recorded["embeddings"] = {
        str(trigger.uuid): [1.0, 0.0, 0.0],
        str(match.uuid): [1.0, 0.0, 0.0],
        str(stranger.uuid): [0.0, 1.0, 0.0],
    }

    result = su.trigger_retroactive_matching(trigger, db_session)

    assert result == {"auto_applied_count": 1, "suggested_count": 0}, result

    # Every voiceprint read happened after the entry commit — i.e. with the
    # caller's read transaction already released.
    read_generations = tracker.at("voiceprint_read")
    assert read_generations, "no voiceprint was read — the test would prove nothing"
    assert all(g >= 1 for g in read_generations), _held(tracker, "voiceprint_read")

    # ...and every index write happened in a LATER generation than the last
    # statement it depends on. Same generation = one transaction spanning both.
    write_generations = tracker.at("profile_embedding_write")
    snapshot_generations = tracker.at("document_snapshot")
    push_generations = tracker.at("index_push")
    assert write_generations and snapshot_generations and push_generations, tracker.timeline
    assert min(push_generations) > max(snapshot_generations), _held(tracker, "index_push")
    assert min(push_generations) > max(write_generations), _held(tracker, "index_push")
    assert tracker.commits >= 3, f"expected read/write/push boundaries, got {tracker.commits}"

    # Real state changed, so "nothing happened" cannot pass the checks above.
    db_session.expire_all()
    assert match.display_name == "Ada"
    assert match.verified is True
    assert match.profile_id == profile.id
    assert stranger.display_name is None
    assert stranger.suggested_name is None
    assert recorded["pushed_names"] == [(str(match.uuid), "Ada")]
    assert recorded["pushed_profiles"][0]["profile_uuid"] == str(profile.uuid)


def test_retroactive_matching_suggests_medium_confidence_without_labelling(
    db_session, normal_user, retroactive_env
):
    """50-75% is a suggestion: no display name, but the document is still pushed.

    Pins the other branch of ``_persist_match_results`` — the one whose index write
    used to come from ``_sync_suggestion_speakers_to_opensearch``, a second
    ``db``-taking helper that looped index writes on the caller's session.
    """
    tracker, recorded = retroactive_env
    media_file = _make_file(db_session, normal_user)

    trigger = _make_speaker(db_session, normal_user, media_file, "SPEAKER_00")
    trigger.display_name = "Ada"
    candidate = _make_speaker(db_session, normal_user, media_file, "SPEAKER_01")
    db_session.flush()

    # cos = 0.6 — above the 0.5 suggestion floor, below the 0.75 auto-apply bar.
    recorded["embeddings"] = {
        str(trigger.uuid): [1.0, 0.0, 0.0],
        str(candidate.uuid): [0.6, 0.8, 0.0],
    }

    result = su.trigger_retroactive_matching(trigger, db_session)

    assert result == {"auto_applied_count": 0, "suggested_count": 1}, result

    db_session.expire_all()
    assert candidate.suggested_name == "Ada"
    assert candidate.display_name is None
    assert candidate.verified is False

    snapshot_generations = tracker.at("document_snapshot")
    push_generations = tracker.at("index_push")
    assert push_generations, "the suggestion was never synced to the index"
    assert snapshot_generations, "no document was snapshotted — the test proves nothing"
    assert min(push_generations) > max(snapshot_generations), _held(tracker, "index_push")
    assert recorded["pushed_names"] == [(str(candidate.uuid), None)]


def test_retroactive_matching_snapshot_survives_the_entry_commit(
    db_session, normal_user, retroactive_env
):
    """The labeled speaker is read as plain data BEFORE the transaction is released.

    ``commit()`` expires every instance in the session, so anything still reading
    attributes off ``updated_speaker`` afterwards would silently issue a refresh
    SELECT — a new transaction, opened underneath the OpenSearch phase.
    """
    tracker, recorded = retroactive_env
    media_file = _make_file(db_session, normal_user)
    trigger = _make_speaker(db_session, normal_user, media_file, "SPEAKER_00")
    trigger.display_name = "Ada"
    db_session.flush()
    recorded["embeddings"] = {}  # no voiceprint -> the documented early return

    result = su.trigger_retroactive_matching(trigger, db_session)

    assert result == {"auto_applied_count": 0, "suggested_count": 0}
    # The read happened after the snapshot was taken and the transaction released.
    assert tracker.at("voiceprint_read") == [1], tracker.timeline


# --------------------------------------------------------------------------- #
# 5. _sync_profile_rename_to_opensearch — read, commit, THEN fan out
# --------------------------------------------------------------------------- #
def test_profile_rename_fans_out_after_the_read_commits(db_session, normal_user, monkeypatch):
    tracker = _CommitTracker(db_session)
    pushed: list[tuple[str, str]] = []

    profile = _make_profile(db_session, normal_user)
    media_file = _make_file(db_session, normal_user)
    first = _make_speaker(
        db_session, normal_user, media_file, "SPEAKER_00", profile_id=profile.id, display_name="A"
    )
    second = _make_speaker(
        db_session, normal_user, media_file, "SPEAKER_01", profile_id=profile.id, display_name="A"
    )
    # A linked speaker with no display name must not produce a document at all.
    _make_speaker(db_session, normal_user, media_file, "SPEAKER_02", profile_id=profile.id)
    db_session.flush()

    monkeypatch.setattr(db_session, "commit", tracker.commit)

    def _push(speaker_uuid, display_name):
        tracker.observe("index_push")
        pushed.append((speaker_uuid, display_name))

    monkeypatch.setattr(spk, "_update_opensearch_speaker_name", _push)

    spk._sync_profile_rename_to_opensearch(db_session, profile.id)

    assert {uuid_ for uuid_, _ in pushed} == {str(first.uuid), str(second.uuid)}
    push_generations = tracker.at("index_push")
    assert push_generations, "nothing was pushed — the test would prove nothing"
    assert all(g >= 1 for g in push_generations), _held(tracker, "index_push")


def test_profile_rename_read_returns_plain_data(db_session, normal_user):
    """``_load_profile_speaker_names`` hands back tuples, never ORM rows."""
    profile = _make_profile(db_session, normal_user)
    media_file = _make_file(db_session, normal_user)
    speaker = _make_speaker(
        db_session, normal_user, media_file, profile_id=profile.id, display_name="A"
    )
    db_session.flush()

    rows = spk._load_profile_speaker_names(db_session, profile.id)

    assert rows == [(str(speaker.uuid), "A")]
    for row in rows:
        for field in row:
            assert isinstance(field, str)


# --------------------------------------------------------------------------- #
# 6. debug_cross_media_data — a REQUEST session held across two index searches
# --------------------------------------------------------------------------- #
def test_debug_cross_media_reads_the_index_after_committing(db_session, normal_user, monkeypatch):
    """The diagnostic endpoint ends its read transaction before the index hop.

    Its ``db`` comes from ``Depends(get_db)`` and lives for the whole REQUEST, so
    there is no scope to close — the fix is to finish the Postgres reads, commit,
    and only then talk to the cluster. The response shape is unchanged.
    """
    tracker = _CommitTracker(db_session)
    media_file = _make_file(db_session, normal_user)
    _make_speaker(db_session, normal_user, media_file, display_name="Ada")
    db_session.flush()

    monkeypatch.setattr(db_session, "commit", tracker.commit)

    def _collect(user_id):
        tracker.observe("index_read")
        return {
            "opensearch_speakers": [{"opensearch_id": "s1", "user_id": user_id}],
            "opensearch_profiles": [],
        }

    monkeypatch.setattr(spk, "_collect_index_debug_documents", _collect)

    report = spk.debug_cross_media_data(db=db_session, current_user=normal_user)

    assert tracker.at("index_read") == [1], _held(tracker, "index_read")

    # The Postgres half still ran — otherwise "committed first" is trivially true.
    assert report["analysis"]["total_speakers"] >= 1
    assert report["analysis"]["total_media_files"] >= 1
    assert report["opensearch_speakers"] == [{"opensearch_id": "s1", "user_id": normal_user.id}]
    assert report["analysis"]["opensearch_speaker_count"] == 1


# --------------------------------------------------------------------------- #
# 7. The suggestion service takes no session at all
# --------------------------------------------------------------------------- #
def _no_session_annotations(func) -> bool:
    signature = inspect.signature(func)
    return not any(
        parameter.annotation is Session or parameter.name in ("db", "db_session")
        for parameter in signature.parameters.values()
    )


def test_suggestion_helpers_declare_no_session_parameter():
    """A ``Session`` parameter is how the idiom spreads — these two must not have one."""
    batch = smart.SmartSpeakerSuggestionService.consolidate_suggestions_batch
    assert _no_session_annotations(batch), inspect.signature(batch)
    optimized = smart.SmartSpeakerSuggestionService._get_profile_suggestions_optimized
    assert _no_session_annotations(optimized), inspect.signature(optimized)


def test_batch_suggestions_run_with_the_database_unreachable(db_session, normal_user, monkeypatch):
    """The batch kNN needs no DB at all — proven by making one impossible to open.

    The tenant scope it used to query for (each speaker's file org) is now supplied
    by the caller's short read phase. If that regressed to ``_media_file_org_ids(db,
    ...)`` this raises instead of returning suggestions.
    """
    media_file = _make_file(db_session, normal_user)
    speaker = _make_speaker(db_session, normal_user, media_file)
    db_session.flush()

    def _forbidden(*args, **kwargs):
        raise AssertionError("consolidate_suggestions_batch opened a database session")

    # Both routes to a session are poisoned. ``session_utils`` binds ``SessionLocal``
    # at import, so patching ``app.db.base`` alone does NOT reach ``session_scope`` —
    # verified by control: a helper opening its own scope still passed.
    monkeypatch.setattr("app.db.session_utils.session_scope", _forbidden)
    monkeypatch.setattr("app.db.base.SessionLocal", _forbidden)

    # A real stand-in rather than a mock, so the profile-existence probe still runs.
    class _FakeIndices:
        @staticmethod
        def exists(index):
            return True

    class _FakeClient:
        indices = _FakeIndices()

        @staticmethod
        def search(index, body):
            return {"hits": {"total": {"value": 1}}}

    monkeypatch.setattr("app.services.opensearch_service.opensearch_client", _FakeClient())
    monkeypatch.setattr(
        "app.services.opensearch_service.get_speaker_embeddings_batch",
        lambda uuids: {u: [1.0, 0.0, 0.0] for u in uuids},
    )

    seen_orgs: list[Any] = []

    def _msearch(*, speaker_embeddings, user_id, threshold, organization_id=None):
        seen_orgs.append(organization_id)
        return {
            speaker_uuid: [
                {"profile_id": 7, "profile_name": "Ada", "speaker_count": 3, "similarity": 0.9}
            ]
            for speaker_uuid in speaker_embeddings
        }

    monkeypatch.setattr("app.services.opensearch_service.msearch_profile_knn_batch", _msearch)

    result = smart.SmartSpeakerSuggestionService.consolidate_suggestions_batch(
        speakers=[speaker],
        user_id=normal_user.id,
        file_org_map={int(media_file.id): None},
    )

    suggestions = result[int(speaker.id)]
    assert [s.name for s in suggestions] == ["Ada"]
    assert suggestions[0].suggestion_type == "profile"
    # The tenant gate still travels with the query — it just arrives as plain data.
    assert seen_orgs == [None]


# --------------------------------------------------------------------------- #
# 8. The gate itself: these files must stay clean
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def auditor():
    if not _AUDITOR.exists():
        pytest.fail(f"{_AUDITOR} is missing — the session-lifetime gate has no implementation")
    spec = importlib.util.spec_from_file_location("audit_session_lifetime", _AUDITOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit_session_lifetime"] = module
    spec.loader.exec_module(module)
    return module


def test_speaker_plane_has_no_session_lifetime_findings_left(auditor):
    """Structural backstop for the sixteen findings this module's tests cover.

    The behavioural tests above pin the SHAPE of each fix; this pins that no new
    one appears in the same six files. It is not a substitute for them — an AST
    sweep cannot tell a released transaction from a renamed function — which is
    why it comes last and asserts only on scopes.

    The one documented residual is listed explicitly, so closing it fails here and
    forces the matching allowlist line to be deleted in the same change.
    """
    root = Path(__file__).resolve().parents[2] / "app"
    findings = []
    for relative in _SPEAKER_PLANE_FILES:
        path = root / relative
        assert path.exists(), f"{relative} moved — update _SPEAKER_PLANE_FILES"
        findings.extend(auditor.scan_file(path, root))

    unexpected = [f for f in findings if f.scope not in _KNOWN_RESIDUAL_SCOPES]
    assert not unexpected, "new session-lifetime findings in the speaker plane:\n  " + "\n  ".join(
        f"{f.path}:{f.line} [{f.category}] {f.scope} — {f.detail}" for f in unexpected
    )

    residual = {f.scope for f in findings}
    assert residual == _KNOWN_RESIDUAL_SCOPES, (
        "the known residual changed — if it is fixed, delete its allowlist line in "
        f"scripts/session-lifetime-allowlist.txt and this set. Found: {sorted(residual)}"
    )
