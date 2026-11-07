# mypy: disable-error-code="arg-type"
# This suite passes structural stand-ins (a fake session, a recording Path) to
# signatures that declare Session/Path. Declared once here rather than as a cast at
# every call site: a cast buries the thing being asserted, and widening a production
# signature to suit a test is worse. Same convention as
# tests/unit/test_proxy_identity_consistency.py.
"""Watch-source tasks (stitch, notification mail, scan) hold no DB transaction across remote I/O.

See ``tests/unit/_session_lifetime_trackers.py`` for the depth-tracking scope stand-in
and the measured incident behind this family of tests.
"""

import contextlib
import uuid as uuid_mod

import pytest

from app.models.email_notification_config import EmailNotificationConfig
from app.models.email_notification_config import WatchSourceEmail
from app.models.watch_source import WatchSource
from app.tasks import watch_source_tasks as wst
from tests.unit._session_lifetime_trackers import ScopeTracker
from tests.unit._session_lifetime_trackers import leak
from tests.unit._session_lifetime_trackers import make_transcribed_file


# --------------------------------------------------------------------------- #
# 3. watch_source.stitch_and_import / send_notification
# --------------------------------------------------------------------------- #
def _make_watch_source(db_session, user, *, source_type="s3"):
    source = WatchSource(
        uuid=uuid_mod.uuid4(),
        name=f"src-{uuid_mod.uuid4().hex[:8]}",
        source_type=source_type,
        user_id=user.id,
        created_by=user.id,
        auto_transcribe=False,
    )
    db_session.add(source)
    db_session.flush()
    return source


@pytest.fixture
def stitch_env(db_session, normal_user, monkeypatch, tmp_path):
    """Patch the watch-source transfer/ffmpeg/ingest seams; keep the DB real."""
    from app.core.config import settings as app_settings

    tracker = ScopeTracker(db_session)
    media_file, _ = make_transcribed_file(db_session, normal_user, speakers=1)

    monkeypatch.setattr(wst, "session_scope", tracker.scope)
    monkeypatch.setattr(
        type(app_settings), "watch_temp_dir", property(lambda self: tmp_path), raising=False
    )

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def download_file(self, remote_path, local_path):
            # A multi-part group is gigabytes over SMB/S3.
            tracker.observe("part_download")
            with open(local_path, "wb") as fh:
                fh.write(b"part")
            return 4

        def upload_file(self, local_path, remote_path):
            tracker.observe("upload_back")
            return True

    monkeypatch.setattr(wst, "create_client", lambda src: _FakeClient())

    def _stitch(parts, output_path):
        tracker.observe("ffmpeg_stitch")
        with open(output_path, "wb") as fh:
            fh.write(b"stitched")
        return True

    monkeypatch.setattr("app.services.watch_sources.multipart.stitch_files", _stitch)
    monkeypatch.setattr(
        "app.services.watch_sources.multipart.generate_stitched_filename",
        lambda base, ext: f"{base}_stitched{ext}",
    )

    def _ingest(db, src, local_path, *, filename, row, size=None):
        # Legitimately DB work — it creates the MediaFile row.
        tracker.observe("ingest_prepared_file")
        row.status = "imported"
        row.media_file_id = media_file.id
        return row

    monkeypatch.setattr(wst, "ingest_prepared_file", _ingest)
    return tracker


def test_stitch_and_import_downloads_and_concats_outside_the_session(
    db_session, normal_user, stitch_env
):
    tracker = stitch_env
    source = _make_watch_source(db_session, normal_user)

    parts = [
        {"path": "remote/clip.part1.mp4", "name": "clip.part1.mp4", "size": 4, "modified": None},
        {"path": "remote/clip.part2.mp4", "name": "clip.part2.mp4", "size": 4, "modified": None},
    ]
    result = wst.stitch_and_import.apply(args=[source.id, "clip", ".mp4", parts]).get()

    assert result["status"] == "imported", result

    download_depths = [d for label, d in tracker.observations if label == "part_download"]
    assert download_depths == [0, 0], leak(tracker, "part_download")
    assert tracker.seen["ffmpeg_stitch"] == 0, leak(tracker, "ffmpeg_stitch")
    # The ingest itself is DB work and belongs inside a (short) scope.
    assert tracker.seen["ingest_prepared_file"] == 1, tracker.observations
    assert tracker.opened >= 2, f"expected a read scope and a write scope, got {tracker.opened}"
    assert tracker.max_depth == 1, "session scopes must not nest"


def test_send_notification_sends_mail_outside_the_session(db_session, normal_user, monkeypatch):
    tracker = ScopeTracker(db_session)
    source = _make_watch_source(db_session, normal_user, source_type="local")

    config = EmailNotificationConfig(
        uuid=uuid_mod.uuid4(),
        name=f"mailer-{uuid_mod.uuid4().hex[:8]}",
        provider="smtp",
        smtp_host="smtp.invalid.example.com",
        smtp_port=587,
        from_address="noreply@example.com",
        default_recipients="ops@example.com",
        is_enabled=True,
    )
    db_session.add(config)
    db_session.flush()
    db_session.add(
        WatchSourceEmail(
            watch_source_id=source.id,
            email_config_id=config.id,
            notify_on_success=True,
            notify_on_error=True,
        )
    )
    db_session.flush()

    sends: list[tuple] = []
    lazy_loads: list[str] = []

    def _send_email(cfg, recipients, subject, html_body, timeout=30, text_body=None):
        # SMTP/Graph round trip: 30 s timeout per config.
        tracker.observe("send_email")
        # Reading a column off the detached config must still work...
        sends.append((cfg.smtp_host, tuple(recipients), subject))
        # ...and a lazy relationship load must fail LOUDLY rather than quietly
        # opening a second transaction while the socket is open.
        try:
            _ = cfg.links[0]
            lazy_loads.append("succeeded")
        except Exception as exc:
            lazy_loads.append(type(exc).__name__)
        return True, "ok"

    monkeypatch.setattr(wst, "session_scope", tracker.scope)
    monkeypatch.setattr("app.services.watch_email_service.send_email", _send_email)

    result = wst.send_notification.apply(
        args=[source.id, {"found": 1, "imported": 1, "skipped": 0, "errors": 0}]
    ).get()

    assert result == {"sent": 1}
    assert tracker.seen["send_email"] == 0, leak(tracker, "send_email")
    assert tracker.opened >= 1
    assert sends[0][0] == "smtp.invalid.example.com"
    assert sends[0][1] == ("ops@example.com",)
    # The config reaches send_email DETACHED, so a stray lazy load raises
    # instead of silently opening a transaction mid-SMTP.
    assert lazy_loads == ["DetachedInstanceError"], lazy_loads


# --------------------------------------------------------------------------- #
# 8. watch_source.scan_single — the INTERPROCEDURAL one
#
#    The session wrapped ``_perform_scan``, one frame down from the slow calls:
#    ``client.list_files()`` against a remote share, then a download AND a MinIO
#    upload for every file up to ``watch.max_imports_per_scan``.
# --------------------------------------------------------------------------- #
class _FakeLock:
    @contextlib.contextmanager
    def acquire_lock(self, lock_key, timeout=300, blocking_timeout=0):
        yield True


@pytest.fixture
def scan_env(db_session, normal_user, monkeypatch, tmp_path):
    from app.core.config import settings as app_settings
    from app.services.watch_sources import processing as proc
    from app.services.watch_sources.base import RemoteFileInfo

    tracker = ScopeTracker(db_session)
    recorded: dict = {}
    media_file, _ = make_transcribed_file(db_session, normal_user, speakers=1)

    # Both modules' scopes are tracked: the leak spans the task AND the service.
    monkeypatch.setattr(wst, "session_scope", tracker.scope)
    monkeypatch.setattr(proc, "session_scope", tracker.scope)
    monkeypatch.setattr(wst, "task_lock_manager", _FakeLock())
    monkeypatch.setattr(wst, "_notify_scan_complete", lambda *a, **kw: None)
    monkeypatch.setattr(
        type(app_settings), "watch_temp_dir", property(lambda self: tmp_path), raising=False
    )

    discovered = [
        RemoteFileInfo(path="remote/talk.mp4", name="talk.mp4", size=4, modified_time=None)
    ]

    class _FakeClient:
        def __init__(self, source):
            # ``LocalWatchClient`` really does keep this reference, which is why
            # the plan must hand over a DETACHED row. Recorded so a test can
            # check the state it was handed over in.
            self.source = source
            recorded["client_source"] = source

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def list_files(self, extensions=None, recursive=True, min_modified=None):
            # An S3 LIST / SMB directory walk over a remote share.
            tracker.observe("list_files")
            return list(discovered)

        def download_file(self, remote_path, local_path):
            # Gigabytes over SMB/S3, per file.
            tracker.observe("part_download")
            with open(local_path, "wb") as fh:
                fh.write(b"data")
            return 4

    monkeypatch.setattr(wst, "create_client", _FakeClient)

    class _Row:
        status = "imported"

    def _ingest(db, source, local_path, *, filename, row, size=None):
        # The documented RESIDUAL: this still runs a MinIO upload inside a
        # session, because the object key derives from the MediaFile PK. What
        # changed is that the scope is now one FILE wide, not one SCAN wide.
        tracker.observe("ingest_prepared_file")
        recorded["ingested"] = filename
        row.status = "imported"
        row.media_file_id = media_file.id
        return _Row()

    monkeypatch.setattr(proc, "ingest_prepared_file", _ingest)

    tracker.recorded = recorded
    return tracker


def test_scan_single_lists_and_downloads_outside_the_session(db_session, normal_user, scan_env):
    from app.models.watch_source import WatchSourceFile

    tracker = scan_env
    source = _make_watch_source(db_session, normal_user, source_type="s3")

    summary = wst.scan_single.apply(args=[source.id]).get()

    assert summary["found"] == 1, summary
    assert summary["imported"] == 1, summary

    observed = tracker.seen
    assert observed["list_files"] == 0, leak(tracker, "list_files")
    assert observed["part_download"] == 0, leak(tracker, "part_download")

    # The per-file ingest legitimately needs a session — but its OWN, opened
    # after the download closed. Pinning this stops the fix regressing into
    # "the task stopped touching the DB", which would satisfy the two asserts
    # above for the wrong reason.
    assert observed["ingest_prepared_file"] == 1, tracker.observations
    assert tracker.opened >= 4, (
        "expected separate scopes for the plan, the terminal-path read, the claim, "
        f"the ingest and the result write; got {tracker.opened}"
    )
    assert tracker.max_depth == 1, "session scopes must not nest"
    assert tracker.depth == 0

    # The download happened in a LATER scope-generation than the plan read, and
    # the ingest in a later one still: proof they are not one shared session.
    assert tracker.opened_at("list_files")[0] >= 1
    assert tracker.opened_at("part_download")[0] > tracker.opened_at("list_files")[0]
    assert tracker.opened_at("ingest_prepared_file")[0] > tracker.opened_at("part_download")[0]

    # Real rows: the tracking row was claimed and finalized.
    db_session.expire_all()
    row = (
        db_session.query(WatchSourceFile)
        .filter(WatchSourceFile.watch_source_id == source.id)
        .first()
    )
    assert row is not None and row.remote_path == "remote/talk.mp4"

    refreshed = db_session.query(WatchSource).filter(WatchSource.id == source.id).first()
    assert refreshed.last_scan_status == "success"
    assert refreshed.last_scan_files_found == 1


def test_scan_plan_detaches_the_source_row(db_session, normal_user, scan_env):
    """The plan must not hand a live ORM row to the client.

    ``LocalWatchClient`` keeps a reference to the ``WatchSource``. Expunging it
    means a stray RELATIONSHIP load raises ``DetachedInstanceError`` instead of
    quietly opening a second transaction while a remote listing is in flight.
    """
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.orm.exc import DetachedInstanceError

    tracker = scan_env
    source = _make_watch_source(db_session, normal_user, source_type="s3")

    plan = wst._load_scan_plan(source.id)

    assert tracker.depth == 0
    assert plan is not None
    assert plan["recursive"] is True
    for key, value in plan.items():
        if key == "client":
            continue
        assert not isinstance(value, WatchSource), key

    # The row the CLIENT is holding — the only one that outlives the scope.
    held = plan["client"].source
    assert held is not None, "the client never received the source row"
    assert sa_inspect(held).detached is True, (
        "the client is holding a session-attached WatchSource: a lazy load during "
        "the remote listing would silently open a second transaction"
    )
    # ...and the detachment is loud, not merely nominal.
    with pytest.raises(DetachedInstanceError):
        _ = held.email_links[0]

    # The claim it made still landed, despite the expunge.
    reloaded = db_session.query(WatchSource).filter(WatchSource.id == source.id).first()
    assert reloaded.last_scan_status == "running"


def test_scan_single_records_a_client_failure_as_a_scan_error(
    db_session, normal_user, scan_env, monkeypatch
):
    """A refused client must still leave the source with an ``error`` status.

    ``create_client`` moved into the read phase with the split, and it can refuse —
    unknown source type, a blocked private endpoint (SSRF guard), a missing optional
    dependency. Before the split that refusal happened inside ``_perform_scan``'s try
    block; if the split let it escape, the source would read ``running`` forever and
    ``scan_all`` would keep re-dispatching a scan that always dies the same way.
    """
    tracker = scan_env
    source = _make_watch_source(db_session, normal_user, source_type="s3")
    source_id = int(source.id)

    def _refuse(src):
        raise ValueError("The configured server address could not be used.")

    monkeypatch.setattr(wst, "create_client", _refuse)

    # ``_record_scan_result`` is observed, not replaced — it still runs and still
    # writes. The spy exists because the read phase's rollback unwinds the
    # savepoint this harness creates the fixture rows in, so the persisted row
    # cannot be read back here; what matters is that the error path REACHES the
    # writer with status "error" rather than escaping the task.
    recorded_calls: list[tuple] = []
    real_record = wst._record_scan_result

    def _spy(source_id_arg, summary_arg, started, duration, status, message):
        recorded_calls.append((status, message))
        return real_record(source_id_arg, summary_arg, started, duration, status, message)

    monkeypatch.setattr(wst, "_record_scan_result", _spy)

    summary = wst.scan_single.apply(args=[source_id]).get()

    assert summary["errors"] == 1, summary
    assert tracker.depth == 0, "a session scope survived the refused client"
    assert len(recorded_calls) == 1, recorded_calls
    status, message = recorded_calls[0]
    assert status == "error"
    assert "could not be used" in message
