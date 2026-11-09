"""Concurrent first writes of the same unique value answer as a duplicate, never a 500.

Three paths read "does it exist?" and then inserted: self-registration, ``set_setting``
and ``add_tags_to_file``. Two concurrent callers both saw "no", both inserted, and the
second failed on the unique index with an unhandled ``IntegrityError``. Registration
now answers the second exactly like a sequential duplicate; the other two use
``INSERT ... ON CONFLICT`` and simply succeed.

Real PostgreSQL connections, one per thread. A rendezvous sits between each caller's check
and its write, so both check before either writes; where the fix makes the second
caller wait on the first one's row, the rendezvous times out and the first proceeds alone.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable

import pytest
from starlette.testclient import TestClient

import app.api.endpoints.auth.registration as registration_module
import app.services.upload_prepare_queries as prepare_upload_module
from app.db.base import SessionLocal
from app.main import app
from app.models.media import MediaFile
from app.models.system_settings import SystemSettings
from app.models.tag import FileTag
from app.models.tag import Tag
from app.models.user import User
from app.services import system_settings_service

PASSWORD = "Race-condition-pass-1!"


class _Rendezvous:
    """Two callers wait (up to *timeout*) for each other; ``meet`` says whether both came.

    After the fix the second caller is often waiting on the first one's row, so not
    meeting is expected; the first then proceeds alone once the wait times out.
    """

    def __init__(self, timeout: float = 5.0) -> None:
        self._arrived = 0
        self._timeout = timeout
        self._cond = threading.Condition()

    def meet(self) -> bool:
        with self._cond:
            self._arrived += 1
            self._cond.notify_all()
            return self._cond.wait_for(lambda: self._arrived >= 2, timeout=self._timeout)


def _run_pair(work: Callable[[], object]) -> list[str]:
    """Run *work* in two threads; return each outcome (``"ok"`` or the error's type)."""
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            result = work()
            outcome = "ok" if result is None else str(result)
        except Exception as exc:  # recorded, not asserted: an assert in a thread is lost
            outcome = type(exc).__name__
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return sorted(outcomes)


def test_concurrent_registrations_of_one_email(monkeypatch):
    email = f"register-race-{uuid.uuid4().hex[:12]}@example.com"
    rendezvous = _Rendezvous()
    real_status = registration_module.initial_approval_status

    def _after_the_existence_check(db):
        rendezvous.meet()
        return real_status(db)

    monkeypatch.setattr(registration_module, "initial_approval_status", _after_the_existence_check)

    def register() -> int:
        client = TestClient(app, raise_server_exceptions=False)
        return client.post(
            "/api/auth/register",
            json={"email": email, "password": PASSWORD, "full_name": "Concurrent Signup"},
        ).status_code

    try:
        outcomes = _run_pair(register)
    finally:
        with SessionLocal() as db:
            db.query(User).filter(User.email == email).delete()
            db.commit()

    assert outcomes == ["200", "400"]


def test_concurrent_first_writes_of_one_setting(monkeypatch):
    key = f"race.test.{uuid.uuid4().hex[:12]}"
    rendezvous = _Rendezvous()

    def write() -> None:
        with SessionLocal() as db:
            real_commit = db.commit

            def _commit_after_both_checked() -> None:
                rendezvous.meet()
                real_commit()

            monkeypatch.setattr(db, "commit", _commit_after_both_checked)
            system_settings_service.set_setting(db, key, "v")

    try:
        outcomes = _run_pair(write)
        with SessionLocal() as db:
            rows = db.query(SystemSettings).filter(SystemSettings.key == key).count()
    finally:
        with SessionLocal() as db:
            db.query(SystemSettings).filter(SystemSettings.key == key).delete()
            db.commit()

    assert outcomes == ["ok", "ok"]
    assert rows == 1


@pytest.fixture
def owner_with_file_and_tag():
    """A committed user, a media file, and a tag the user already owns."""
    with SessionLocal() as db:
        user = User(
            email=f"tag-race-{uuid.uuid4().hex[:12]}@example.com",
            full_name="Tag Race",
            hashed_password="not-used",
            is_active=True,
            is_superuser=False,
            role="user",
        )
        db.add(user)
        db.flush()
        files = [
            MediaFile(
                uuid=str(uuid.uuid4()),
                user_id=user.id,
                filename=f"race-{n}.mp4",
                storage_path=f"test/race-{uuid.uuid4().hex}.mp4",
                content_type="video/mp4",
                file_size=1000,
            )
            for n in range(2)
        ]
        db.add_all(files)
        db.flush()
        # Creates the tag on another file, so the race below is only on the attach.
        prepare_upload_module.add_tags_to_file(db, files[0].id, ["race-tag"], user.id)
        db.commit()
        ids = (int(user.id), int(files[1].id))
    yield ids
    with SessionLocal() as db:
        db.query(MediaFile).filter(MediaFile.user_id == ids[0]).delete()
        db.query(Tag).filter(Tag.user_id == ids[0]).delete()
        db.query(User).filter(User.id == ids[0]).delete()
        db.commit()


def test_concurrent_attaches_of_one_tag_to_one_file(owner_with_file_and_tag, monkeypatch):
    user_id, file_id = owner_with_file_and_tag
    rendezvous = _Rendezvous()
    real_resolve = prepare_upload_module.resolve_or_create_tags

    def _resolve_then_meet(*args, **kwargs):
        resolved = real_resolve(*args, **kwargs)
        rendezvous.meet()
        return resolved

    monkeypatch.setattr(prepare_upload_module, "resolve_or_create_tags", _resolve_then_meet)

    def attach() -> None:
        with SessionLocal() as db:
            prepare_upload_module.add_tags_to_file(db, file_id, ["race-tag"], user_id)
            db.commit()

    outcomes = _run_pair(attach)

    with SessionLocal() as db:
        attached = db.query(FileTag).filter(FileTag.media_file_id == file_id).count()
    assert outcomes == ["ok", "ok"]
    assert attached == 1
