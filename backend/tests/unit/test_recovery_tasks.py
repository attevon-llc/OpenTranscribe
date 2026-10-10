"""Tests for ``app/tasks/recovery_tasks.py`` — the post-data-loss re-ingestion tasks.

These two tasks exist because this deployment lost its Postgres and OpenSearch volumes
while 484 GB of media survived in MinIO. They run against the *irreplaceable* remainder, and
each one carries a stated safety constraint that the module docstring spells out:

* ``youtube_metadata_fetch`` must **never re-download media** and must stay under YouTube's
  bot-detection threshold — "we cannot afford an IP block on the surviving thumbnail ids".
  Its only lever for that is ``_YT_METADATA_DELAY_SECONDS`` and the ``limit`` argument.
* ``youtube_metadata_backfill`` must **never write a wrong title** onto a recovered row —
  "better an empty title than a wrong one". Which rows are eligible to be overwritten is
  decided entirely by ``_looks_unenriched``.

Neither had a test. Both failure modes are silent: an over-eager ``_looks_unenriched``
overwrites a title a user typed by hand with one matched only by duration, and a ``limit``
that does not bound requests gets the host IP blocked, which ends the recovery outright.

Pinned here:

1. ``_looks_unenriched`` — the row-eligibility gate, including the "user already renamed
   it" case it exists to protect.
2. ``youtube_metadata_fetch`` — resumability, ``limit`` semantics, and the sidecar write
   cadence, with the real recovery-service seams stubbed and ``_YT_METADATA_DELAY_SECONDS``
   pinned to 0.
3. ``youtube_metadata_backfill`` — the empty-sidecar short circuit and the candidate filter.
4. One **characterization test for an open defect**:
   ``test_limit_does_not_bound_requests_when_fetches_fail``.

Following the characterization-test convention of ``tests/unit/test_chunking_service.py``.
"""

from __future__ import annotations

from typing import Any
from typing import cast

import pytest

from app.models.media import MediaFile
from app.tasks import recovery_tasks
from app.tasks.recovery_tasks import _looks_unenriched


class _Row:
    """Structural stand-in for the two MediaFile attributes ``_looks_unenriched`` reads."""

    def __init__(self, title: str | None, storage_path: Any) -> None:
        self.title = title
        self.storage_path = storage_path


def _fake_row(title: str | None, storage_path: Any) -> MediaFile:
    """A two-attribute double, typed as the MediaFile the predicate declares.

    `_looks_unenriched` reads exactly `title` and `storage_path`, so a real
    MediaFile would need a DB session for no gain. The cast is at construction —
    one place — rather than at each of the eight call sites, and it is a cast
    rather than a `type: ignore` so a future reader sees a deliberate double
    instead of a silenced check.
    """
    return cast(MediaFile, _Row(title, storage_path))


@pytest.fixture
def recovery_seams(monkeypatch):
    """Stub every ``storage_recovery_service`` seam the fetch task touches.

    Returns a recorder exposing ``.fetch_calls`` and ``.saves`` so tests can assert on
    what the task actually *did* — how many network fetches it issued and what it
    persisted — rather than only on the summary it chose to report.
    """

    class Recorder:
        def __init__(self) -> None:
            self.ids: list[str] = []
            self.initial_cache: dict[str, dict[str, Any]] = {}
            self.responses: dict[str, dict[str, Any] | None] = {}
            self.fetch_calls: list[str] = []
            self.saves: list[dict[str, dict[str, Any]]] = []

    rec = Recorder()

    monkeypatch.setattr(recovery_tasks, "_YT_METADATA_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(
        recovery_tasks.recovery, "discover_youtube_ids", lambda _client, _uid: list(rec.ids)
    )
    monkeypatch.setattr(
        recovery_tasks.recovery, "load_metadata_sidecar", lambda _svc: dict(rec.initial_cache)
    )

    def _save(_svc, data):
        rec.saves.append(dict(data))

    monkeypatch.setattr(recovery_tasks.recovery, "save_metadata_sidecar", _save)

    def _fetch(video_id):
        rec.fetch_calls.append(video_id)
        return rec.responses.get(video_id)

    monkeypatch.setattr(recovery_tasks.recovery, "fetch_youtube_metadata", _fetch)

    # The task constructs these inside its body; neither stub is ever called by the stubs above.
    import app.services.minio_service as minio_service

    monkeypatch.setattr(minio_service, "MinIOService", lambda: object())
    monkeypatch.setattr(minio_service, "minio_client", object(), raising=False)

    return rec


# --------------------------------------------------------------------------------------
# 1. _looks_unenriched — which rows may be overwritten
# --------------------------------------------------------------------------------------


def test_a_row_whose_title_is_the_storage_basename_is_eligible():
    """``register_object`` sets title = the object basename, so that means "never restored"."""
    row = _fake_row(
        title="0f8c1a2b-3d4e-5f60-7182-93a4b5c6d7e8.mp4",
        storage_path="user_1/youtube_abc123/0f8c1a2b-3d4e-5f60-7182-93a4b5c6d7e8.mp4",
    )

    assert _looks_unenriched(row) is True


@pytest.mark.parametrize("title", [None, "", "   ", "\t\n"])
def test_a_row_with_no_usable_title_is_eligible(title):
    assert _looks_unenriched(_fake_row(title, "user_1/youtube_abc/file.mp4")) is True


def test_a_row_the_user_has_already_named_is_protected():
    """The guard that stops recovery overwriting a real title with a duration guess.

    This is the direction that loses data: a wrong-but-plausible title written over a
    correct one is not detectable afterwards, because the original is gone.
    """
    row = _fake_row(title="Q3 board meeting", storage_path="user_1/youtube_abc/uuid.mp4")

    assert _looks_unenriched(row) is False


def test_the_title_is_compared_against_the_basename_not_the_whole_key():
    """A title equal to the *full* storage path is not the placeholder shape."""
    row = _fake_row(title="user_1/youtube_abc/uuid.mp4", storage_path="user_1/youtube_abc/uuid.mp4")

    assert _looks_unenriched(row) is False


def test_surrounding_whitespace_on_a_placeholder_title_still_matches():
    """``.strip()`` is applied before comparison, so a padded placeholder is still one."""
    row = _fake_row(title="  uuid.mp4  ", storage_path="user_1/youtube_abc/uuid.mp4")

    assert _looks_unenriched(row) is True


def test_a_storage_path_with_no_slash_is_compared_whole():
    row = _fake_row(title="loose.mp4", storage_path="loose.mp4")

    assert _looks_unenriched(row) is True


def test_a_row_with_no_storage_path_is_not_treated_as_a_placeholder():
    """``str(None)`` is ``"None"``, so only a row literally titled ``None`` would match.

    Pinned because the stringification is implicit: a row with a real title and a missing
    storage path must stay protected rather than becoming eligible by accident.
    """
    assert _looks_unenriched(_fake_row("A real title", None)) is False
    # The one degenerate case the implicit str() creates, stated explicitly.
    assert _looks_unenriched(_fake_row("None", None)) is True


# --------------------------------------------------------------------------------------
# 2. youtube_metadata_fetch
# --------------------------------------------------------------------------------------


def test_every_discovered_id_is_fetched_and_persisted_when_the_cache_is_empty(recovery_seams):
    recovery_seams.ids = ["aaa", "bbb", "ccc"]
    recovery_seams.responses = {
        "aaa": {"title": "A", "duration": 10.0},
        "bbb": {"title": "B", "duration": 20.0},
        "ccc": {"title": "C", "duration": 30.0},
    }

    summary = recovery_tasks.youtube_metadata_fetch(user_id=1)

    assert recovery_seams.fetch_calls == ["aaa", "bbb", "ccc"]
    assert summary == {
        "ids_discovered": 3,
        "fetched": 3,
        "already_present": 0,
        "failed": 0,
        "total_cached": 3,
    }
    assert recovery_seams.saves[-1] == recovery_seams.responses


def test_the_sidecar_is_written_after_every_success_so_the_run_is_resumable(recovery_seams):
    """The stated resumability guarantee: a crash mid-run must not lose earlier fetches.

    Asserted on the *contents* of each intermediate save, not just the number of saves —
    a save that always wrote ``{}`` would satisfy a count-only assertion.
    """
    recovery_seams.ids = ["aaa", "bbb"]
    recovery_seams.responses = {"aaa": {"title": "A"}, "bbb": {"title": "B"}}

    recovery_tasks.youtube_metadata_fetch(user_id=1)

    assert len(recovery_seams.saves) >= 2, "one save per successful fetch, plus the final one"
    assert recovery_seams.saves[0] == {"aaa": {"title": "A"}}
    assert recovery_seams.saves[1] == {"aaa": {"title": "A"}, "bbb": {"title": "B"}}


def test_ids_already_carrying_a_title_are_skipped_without_a_network_call(recovery_seams):
    """Resuming a completed run must issue no requests at all."""
    recovery_seams.ids = ["aaa", "bbb"]
    recovery_seams.initial_cache = {"aaa": {"title": "A"}, "bbb": {"title": "B"}}

    summary = recovery_tasks.youtube_metadata_fetch(user_id=1)

    assert recovery_seams.fetch_calls == []
    assert summary["already_present"] == 2
    assert summary["fetched"] == 0
    assert summary["total_cached"] == 2


def test_a_cached_entry_with_an_empty_title_is_retried(recovery_seams):
    """The skip test is ``cache[id].get("title")`` — truthiness, not key presence.

    A record that came back title-less is worthless for backfill, so it must not count as
    done. Pinned because changing this to ``id in cache`` would permanently strand those ids.
    """
    recovery_seams.ids = ["aaa", "bbb"]
    recovery_seams.initial_cache = {"aaa": {"title": "", "duration": 5.0}}
    recovery_seams.responses = {"aaa": {"title": "A", "duration": 5.0}, "bbb": {"title": "B"}}

    summary = recovery_tasks.youtube_metadata_fetch(user_id=1)

    assert recovery_seams.fetch_calls == ["aaa", "bbb"]
    assert summary["already_present"] == 0
    assert summary["fetched"] == 2
    assert recovery_seams.saves[-1]["aaa"]["title"] == "A"


def test_limit_caps_the_number_of_new_ids_fetched_in_one_run(recovery_seams):
    recovery_seams.ids = ["aaa", "bbb", "ccc", "ddd", "eee"]
    recovery_seams.responses = {i: {"title": i.upper()} for i in recovery_seams.ids}

    summary = recovery_tasks.youtube_metadata_fetch(user_id=1, limit=2)

    assert recovery_seams.fetch_calls == ["aaa", "bbb"]
    assert summary["fetched"] == 2
    assert summary["total_cached"] == 2
    assert sorted(recovery_seams.saves[-1]) == ["aaa", "bbb"]


def test_already_cached_ids_do_not_consume_the_limit(recovery_seams):
    """``limit`` budgets *new* work, so a resumed run still makes progress."""
    recovery_seams.ids = ["aaa", "bbb", "ccc", "ddd"]
    recovery_seams.initial_cache = {"aaa": {"title": "A"}, "bbb": {"title": "B"}}
    recovery_seams.responses = {"ccc": {"title": "C"}, "ddd": {"title": "D"}}

    summary = recovery_tasks.youtube_metadata_fetch(user_id=1, limit=2)

    assert recovery_seams.fetch_calls == ["ccc", "ddd"]
    assert summary["already_present"] == 2
    assert summary["fetched"] == 2


def test_no_discovered_ids_still_writes_the_sidecar_and_reports_zeroes(recovery_seams):
    recovery_seams.ids = []

    summary = recovery_tasks.youtube_metadata_fetch(user_id=1)

    assert recovery_seams.fetch_calls == []
    assert summary == {
        "ids_discovered": 0,
        "fetched": 0,
        "already_present": 0,
        "failed": 0,
        "total_cached": 0,
    }


def test_a_failed_fetch_is_counted_and_does_not_poison_the_cache(recovery_seams):
    recovery_seams.ids = ["aaa", "bbb", "ccc"]
    recovery_seams.responses = {"aaa": {"title": "A"}, "bbb": None, "ccc": {"title": "C"}}

    summary = recovery_tasks.youtube_metadata_fetch(user_id=1)

    assert summary["failed"] == 1
    assert summary["fetched"] == 2
    assert "bbb" not in recovery_seams.saves[-1]
    assert sorted(recovery_seams.saves[-1]) == ["aaa", "ccc"]


def test_limit_bounds_requests_regardless_of_whether_they_succeed(recovery_seams):
    """CHARACTERIZATION — pins current WRONG behaviour. DEFECT: recovery_tasks.py L81-L91.

    ``new_this_run`` is incremented only in the *success* branch (L91), while the ``limit``
    check at L81 tests ``new_this_run >= limit``. A failing fetch therefore costs a network
    request and a ``_YT_METADATA_DELAY_SECONDS`` sleep but does not advance the budget, so
    ``limit`` stops bounding requests exactly when fetches are failing.

    That inverts the argument's purpose. The module docstring's stated reason for the delay
    and the cap is that "we cannot afford an IP block on the surviving thumbnail ids" — and
    a run whose fetches are failing is the most likely one to be *already* rate-limited or
    soft-blocked. An operator asking for ``limit=2`` as a cautious probe against a few
    thousand ids gets the full sweep instead.

    Compounding it, failures are never negatively cached (asserted by
    ``test_a_failed_fetch_is_counted_and_does_not_poison_the_cache``), so every subsequent
    run repeats the whole sweep from the start.

    Fixed in issue #457: the budget advances on every ATTEMPT. All-failing is the
    scenario that matters — it is the state most likely to mean the remote has already
    soft-blocked this host, which is the reason the module rate-limits itself at all.
    """
    recovery_seams.ids = [f"id{n:02d}" for n in range(10)]
    recovery_seams.responses = {}  # every fetch fails

    summary = recovery_tasks.youtube_metadata_fetch(user_id=1, limit=2)

    assert len(recovery_seams.fetch_calls) == 2, (
        "limit=2 must cap REQUESTS; counting only successes meant an all-failing run "
        "swept every id, which is exactly when a soft block is most likely"
    )
    assert summary["failed"] == 2
    assert summary["fetched"] == 0


# --------------------------------------------------------------------------------------
# 3. youtube_metadata_backfill
# --------------------------------------------------------------------------------------


def test_backfill_short_circuits_on_an_empty_sidecar_without_opening_a_session(monkeypatch):
    """No sidecar means no possible match — it must not query, let alone write.

    ``session_scope`` is replaced with something that raises, so a regression that opens a
    session anyway fails loudly instead of silently scanning every row of the user's
    library on the CPU worker.
    """
    import app.services.minio_service as minio_service

    monkeypatch.setattr(minio_service, "MinIOService", lambda: object())
    monkeypatch.setattr(recovery_tasks.recovery, "load_metadata_sidecar", lambda _svc: {})

    def _explode():
        raise AssertionError("backfill must not open a DB session with an empty sidecar")

    monkeypatch.setattr(recovery_tasks, "session_scope", _explode)

    summary = recovery_tasks.youtube_metadata_backfill(user_id=1)

    assert summary == {"candidate_rows": 0, "matched": 0, "updated": 0}


def test_backfill_only_offers_unenriched_rows_to_the_matcher(monkeypatch, db_session, normal_user):
    """The candidate filter is the last guard before a title is overwritten.

    A row the user has already named must never reach ``match_metadata_by_duration``, even
    when its duration matches a sidecar record exactly. Asserted on the rows the matcher was
    actually handed, and on the summary, so a filter that silently passed everything through
    would fail here rather than in production.
    """
    import uuid as uuid_module

    from app.core.enums import FileStatus
    from app.models.media import MediaFile

    named = MediaFile(
        uuid=uuid_module.uuid4(),
        user_id=normal_user.id,
        filename="named.mp4",
        title="A title the user typed",
        storage_path=f"user_{normal_user.id}/youtube_aaa/named.mp4",
        file_size=1024,
        content_type="video/mp4",
        status=FileStatus.COMPLETED,
        duration=123.0,
    )
    placeholder = MediaFile(
        uuid=uuid_module.uuid4(),
        user_id=normal_user.id,
        filename="uuid-name.mp4",
        title="uuid-name.mp4",
        storage_path=f"user_{normal_user.id}/youtube_bbb/uuid-name.mp4",
        file_size=1024,
        content_type="video/mp4",
        status=FileStatus.COMPLETED,
        duration=123.0,
    )
    no_duration = MediaFile(
        uuid=uuid_module.uuid4(),
        user_id=normal_user.id,
        filename="pending.mp4",
        title="pending.mp4",
        storage_path=f"user_{normal_user.id}/youtube_ccc/pending.mp4",
        file_size=1024,
        content_type="video/mp4",
        status=FileStatus.PROCESSING,
        duration=None,
    )
    db_session.add_all([named, placeholder, no_duration])
    db_session.commit()

    import app.services.minio_service as minio_service

    monkeypatch.setattr(minio_service, "MinIOService", lambda: object())
    monkeypatch.setattr(
        recovery_tasks.recovery,
        "load_metadata_sidecar",
        lambda _svc: {"vid": {"title": "Matched", "duration": 123.0}},
    )

    import contextlib

    @contextlib.contextmanager
    def _scope():
        yield db_session

    monkeypatch.setattr(recovery_tasks, "session_scope", _scope)

    seen: dict[str, list[str]] = {}

    def _match(rows, _metadata, **_kw):
        seen["titles"] = [r.title for r in rows]
        return {}

    monkeypatch.setattr(recovery_tasks.recovery, "match_metadata_by_duration", _match)
    monkeypatch.setattr(recovery_tasks.recovery, "apply_metadata_matches", lambda _db, _m: 0)

    summary = recovery_tasks.youtube_metadata_backfill(user_id=normal_user.id)

    assert seen["titles"] == ["uuid-name.mp4"], "only the placeholder row is a candidate"
    assert summary["candidate_rows"] == 1
    assert summary["matched"] == 0
    assert summary["updated"] == 0


def test_backfill_reports_the_row_count_the_apply_step_actually_wrote(monkeypatch, db_session):
    """``updated`` is the applier's return value, not the match count.

    They differ whenever a match is dropped at write time, and conflating them would report
    a successful recovery that never touched the database.
    """
    import contextlib

    import app.services.minio_service as minio_service

    monkeypatch.setattr(minio_service, "MinIOService", lambda: object())
    monkeypatch.setattr(
        recovery_tasks.recovery,
        "load_metadata_sidecar",
        lambda _svc: {"vid": {"title": "T", "duration": 1.0}},
    )

    @contextlib.contextmanager
    def _scope():
        yield db_session

    monkeypatch.setattr(recovery_tasks, "session_scope", _scope)
    monkeypatch.setattr(
        recovery_tasks.recovery, "match_metadata_by_duration", lambda _r, _m, **_k: {1: {}, 2: {}}
    )
    monkeypatch.setattr(recovery_tasks.recovery, "apply_metadata_matches", lambda _db, _m: 1)

    summary = recovery_tasks.youtube_metadata_backfill(user_id=-1)

    assert summary["matched"] == 2
    assert summary["updated"] == 1


# --------------------------------------------------------------------------------------
# media_duration_backfill (issue #969)
# --------------------------------------------------------------------------------------

import contextlib
import uuid as uuid_module

from app.core.enums import FileStatus
from app.models.media import TranscriptSegment


def _patch_session_scope_to_test_db(monkeypatch, db_session):
    """Route every ``session_scope()`` call in the task at the test's savepoint session.

    The task opens several short-lived sessions on purpose (the read/slow-work/write
    phase split the session-lifetime rule requires) — all of them must land on the
    SAME connection the test's fixtures and assertions use, or writes are invisible
    to the test and rollback can't undo them.
    """

    @contextlib.contextmanager
    def _scope():
        yield db_session

    monkeypatch.setattr(recovery_tasks, "session_scope", _scope)


def _corrupted_file(
    db_session,
    user_id: int,
    *,
    stored_duration: float = 30.0,
    segment_end: float | None = None,
    metadata_important: dict | None = None,
    is_quarantined: bool = False,
    legal_hold: bool = False,
    status: FileStatus = FileStatus.COMPLETED,
) -> MediaFile:
    """A completed file whose stored duration matches its speech extent (the #969 signature).

    ``segment_end`` defaults to ``stored_duration`` -- the overwrite's exact-equality
    fingerprint the backfill selects on. Pass it explicitly to build a row that does
    NOT carry the signature (``test_a_non_matching_row_is_not_selected``).
    """
    if segment_end is None:
        segment_end = stored_duration
    mf = MediaFile(
        uuid=uuid_module.uuid4(),
        user_id=user_id,
        filename="corrupted.mp4",
        storage_path=f"user_{user_id}/corrupted-{uuid_module.uuid4().hex[:8]}.mp4",
        file_size=4096,
        content_type="video/mp4",
        status=status,
        duration=stored_duration,
        duration_source=None,
        metadata_important=metadata_important or {},
        is_quarantined=is_quarantined,
        legal_hold=legal_hold,
    )
    db_session.add(mf)
    db_session.commit()
    db_session.refresh(mf)

    db_session.add(
        TranscriptSegment(
            media_file_id=mf.id,
            start_time=0.0,
            end_time=segment_end,
            text="x",
        )
    )
    db_session.commit()
    return mf


def test_dry_run_reports_counts_and_writes_nothing(monkeypatch, db_session, normal_user):
    """``dry_run=True`` (the default) must not touch the database."""
    mf = _corrupted_file(
        db_session, normal_user.id, metadata_important={"Duration": 358.19}, stored_duration=30.0
    )
    _patch_session_scope_to_test_db(monkeypatch, db_session)

    summary = recovery_tasks.media_duration_backfill(user_id=normal_user.id, dry_run=True)

    assert summary["examined"] == 1
    assert summary["tier_a"] == 1
    assert summary["updated"] == 1

    db_session.refresh(mf)
    assert mf.duration == pytest.approx(30.0), "dry run must not write anything"
    assert mf.duration_source is None


def test_tier_a_corrects_from_metadata_with_no_minio_call(monkeypatch, db_session, normal_user):
    """A row with a usable ``metadata_important.Duration`` is corrected by a pure dict read."""
    import app.services.minio_service as minio_service

    def _boom(*_a, **_k):
        raise AssertionError("Tier A must not touch MinIO")

    monkeypatch.setattr(minio_service, "get_internal_presigned_url", _boom)
    _patch_session_scope_to_test_db(monkeypatch, db_session)

    mf = _corrupted_file(
        db_session, normal_user.id, metadata_important={"Duration": 358.19}, stored_duration=347.24
    )

    summary = recovery_tasks.media_duration_backfill(user_id=normal_user.id, dry_run=False)

    assert summary["tier_a"] == 1
    assert summary["updated"] == 1
    db_session.refresh(mf)
    assert mf.duration == pytest.approx(358.19)
    assert mf.duration_source == "container"


def test_tier_b_probes_minio_when_metadata_has_no_duration(monkeypatch, db_session, normal_user):
    """No usable metadata Duration -> the MinIO object is re-probed directly with ffprobe."""
    import app.services.minio_service as minio_service
    import app.tasks.transcription.metadata_extractor as metadata_extractor

    monkeypatch.setattr(
        minio_service, "get_internal_presigned_url", lambda path, expires=3600: f"https://x/{path}"
    )
    monkeypatch.setattr(metadata_extractor, "probe_media_duration", lambda url, timeout=15: 358.19)
    _patch_session_scope_to_test_db(monkeypatch, db_session)

    mf = _corrupted_file(db_session, normal_user.id, metadata_important={}, stored_duration=347.24)

    summary = recovery_tasks.media_duration_backfill(user_id=normal_user.id, dry_run=False)

    assert summary["tier_b"] == 1
    assert summary["updated"] == 1
    db_session.refresh(mf)
    assert mf.duration == pytest.approx(358.19)
    assert mf.duration_source == "container"


def test_unrecoverable_leaves_duration_untouched_and_labels_it_transcript_extent(
    monkeypatch, db_session, normal_user
):
    """When neither tier produces a duration, the row is labelled honestly, not silently skipped."""
    import app.services.minio_service as minio_service
    import app.tasks.transcription.metadata_extractor as metadata_extractor

    monkeypatch.setattr(
        minio_service, "get_internal_presigned_url", lambda path, expires=3600: f"https://x/{path}"
    )
    monkeypatch.setattr(metadata_extractor, "probe_media_duration", lambda url, timeout=15: None)
    _patch_session_scope_to_test_db(monkeypatch, db_session)

    mf = _corrupted_file(db_session, normal_user.id, metadata_important={}, stored_duration=30.0)

    summary = recovery_tasks.media_duration_backfill(user_id=normal_user.id, dry_run=False)

    assert summary["unrecoverable"] == 1
    assert summary["updated"] == 1
    db_session.refresh(mf)
    assert mf.duration == pytest.approx(30.0), "the speech-extent value is left in place"
    assert mf.duration_source == "transcript_extent"


def test_a_second_run_is_a_no_op(monkeypatch, db_session, normal_user):
    """Idempotency: ``duration_source`` cleared on the first run means nothing matches the second."""
    import app.services.minio_service as minio_service

    monkeypatch.setattr(
        minio_service, "get_internal_presigned_url", lambda path, expires=3600: f"https://x/{path}"
    )
    _patch_session_scope_to_test_db(monkeypatch, db_session)

    _corrupted_file(
        db_session, normal_user.id, metadata_important={"Duration": 358.19}, stored_duration=30.0
    )

    first = recovery_tasks.media_duration_backfill(user_id=normal_user.id, dry_run=False)
    assert first["updated"] == 1

    second = recovery_tasks.media_duration_backfill(user_id=normal_user.id, dry_run=False)
    assert second["examined"] == 0
    assert second["updated"] == 0


def test_a_shorter_probed_value_is_skipped_not_applied(monkeypatch, db_session, normal_user):
    """A Tier A/B candidate SHORTER than the stored value is an anomaly, never a correction."""
    _patch_session_scope_to_test_db(monkeypatch, db_session)

    mf = _corrupted_file(
        db_session, normal_user.id, metadata_important={"Duration": 350.0}, stored_duration=400.0
    )

    summary = recovery_tasks.media_duration_backfill(user_id=normal_user.id, dry_run=False)

    assert summary["skipped_lower"] == 1
    assert summary["updated"] == 0
    db_session.refresh(mf)
    assert mf.duration == pytest.approx(400.0), "the (wrong but larger) stored value must survive"
    assert mf.duration_source is None, "an anomaly must not be silently marked resolved"


@pytest.mark.parametrize("is_quarantined,legal_hold", [(True, False), (False, True), (True, True)])
def test_quarantined_or_legal_hold_rows_are_never_touched(
    monkeypatch, db_session, normal_user, is_quarantined, legal_hold
):
    """Issue #824/#664: a held file's duration must not move while it is held."""
    _patch_session_scope_to_test_db(monkeypatch, db_session)

    mf = _corrupted_file(
        db_session,
        normal_user.id,
        metadata_important={"Duration": 358.19},
        stored_duration=30.0,
        is_quarantined=is_quarantined,
        legal_hold=legal_hold,
    )

    summary = recovery_tasks.media_duration_backfill(user_id=normal_user.id, dry_run=False)

    assert summary["examined"] == 0
    db_session.refresh(mf)
    assert mf.duration == pytest.approx(30.0)
    assert mf.duration_source is None


def test_updated_files_are_dispatched_for_reindexing(monkeypatch, db_session, normal_user):
    """Issue #969: duration is indexed and copied into every chunk's metadata."""
    import app.services.minio_service as minio_service
    import app.tasks.search_indexing_task as search_indexing_task

    monkeypatch.setattr(
        minio_service, "get_internal_presigned_url", lambda path, expires=3600: f"https://x/{path}"
    )
    dispatched: list[dict] = []
    monkeypatch.setattr(
        search_indexing_task.index_transcript_search_task,
        "delay",
        lambda **kwargs: dispatched.append(kwargs),
    )
    _patch_session_scope_to_test_db(monkeypatch, db_session)

    mf = _corrupted_file(
        db_session, normal_user.id, metadata_important={"Duration": 358.19}, stored_duration=30.0
    )

    summary = recovery_tasks.media_duration_backfill(user_id=normal_user.id, dry_run=False)

    assert summary["reindexed"] == 1
    assert len(dispatched) == 1
    assert dispatched[0]["file_id"] == mf.id
    assert dispatched[0]["file_uuid"] == str(mf.uuid)
    assert dispatched[0]["user_id"] == normal_user.id


def test_a_non_matching_row_is_not_selected(monkeypatch, db_session, normal_user):
    """A row whose duration is NOT the speech-extent signature was never touched by #969."""
    _patch_session_scope_to_test_db(monkeypatch, db_session)

    # stored_duration (400.0) does not equal segment_end (30.0) -- not the overwrite signature.
    mf = _corrupted_file(
        db_session,
        normal_user.id,
        metadata_important={"Duration": 358.19},
        stored_duration=400.0,
        segment_end=30.0,
    )

    summary = recovery_tasks.media_duration_backfill(user_id=normal_user.id, dry_run=False)

    assert summary["examined"] == 0
    db_session.refresh(mf)
    assert mf.duration == pytest.approx(400.0)
    assert mf.duration_source is None
