"""Credential handling for protected media imports: no secret travels as a Celery argument.

The import password used to be a task argument, i.e. written to the broker, kept across
retries and visible to monitoring. It is now held encrypted in Redis under an opaque
reference with a TTL; the task fetches it and removes it when finished.
"""

from __future__ import annotations

import ast
import contextlib
import logging
import re
import uuid as uuid_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from celery.app.task import Task

import app.utils.task_secret_handoff as handoff
from app.api.endpoints.files.url_processing import _dispatch_video_task
from app.core.constants import TASK_SECRET_HANDOFF_TTL_SECONDS
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.tasks import youtube_processing
from app.tasks.youtube_processing import process_youtube_url_task

CTX = "media-cred:1:file-a"
SECRET = "hunter2-very-secret"
USER = "alice-media-login"


class FakeRedis:
    """Just enough of redis-py for the hand-off, with an explicit clock for expiry."""

    def __init__(self) -> None:
        self.store: dict[str, tuple[Any, int]] = {}
        self.now = 0

    def setex(self, key, ttl, value):
        self.store[key] = (value, self.now + ttl)

    def get(self, key):
        item = self.store.get(key)
        if item is None or item[1] <= self.now:
            return None
        return item[0].encode() if isinstance(item[0], str) else item[0]

    def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture
def fake_redis(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(handoff, "get_redis", lambda: r)
    return r


# ---------------------------------------------------------------- the helper itself


def test_stash_stores_ciphertext_with_the_ttl_and_round_trips(fake_redis):
    ref = handoff.stash_task_secret({"media_password": SECRET, "media_username": USER}, context=CTX)
    assert ref is not None and SECRET not in ref
    raw = next(iter(fake_redis.store.values()))[0]
    assert SECRET not in str(raw) and USER not in str(raw), "stored value must be encrypted"
    assert next(iter(fake_redis.store.values()))[1] == TASK_SECRET_HANDOFF_TTL_SECONDS
    assert handoff.fetch_task_secret(ref, context=CTX) == {
        "media_password": SECRET,
        "media_username": USER,
    }


def test_nothing_to_stash_gives_no_reference(fake_redis):
    assert (
        handoff.stash_task_secret({"media_password": None, "media_username": ""}, context=CTX)
        is None
    )
    assert fake_redis.store == {}


def test_reference_expires_with_a_clear_non_secret_error(fake_redis):
    ref = handoff.stash_task_secret({"media_password": SECRET}, context=CTX)
    assert ref is not None
    fake_redis.now = TASK_SECRET_HANDOFF_TTL_SECONDS + 1
    with pytest.raises(handoff.TaskSecretUnavailableError) as exc:
        handoff.fetch_task_secret(ref, context=CTX)
    assert "expired" in str(exc.value) and SECRET not in str(exc.value)


def test_a_value_cannot_be_replayed_under_another_reference(fake_redis):
    ref = handoff.stash_task_secret({"media_password": SECRET}, context=CTX)
    assert ref is not None
    other = "f" * 32
    fake_redis.store[f"task_secret:{other}"] = fake_redis.store[f"task_secret:{ref}"]
    with pytest.raises(handoff.TaskSecretUnavailableError):
        handoff.fetch_task_secret(other, context=CTX)


def test_a_reference_is_bound_to_its_user_and_file(fake_redis):
    ref = handoff.stash_task_secret({"media_password": SECRET}, context=CTX)
    assert ref is not None
    for other in ("media-cred:2:file-a", "media-cred:1:file-b"):
        with pytest.raises(handoff.TaskSecretUnavailableError):
            handoff.fetch_task_secret(ref, context=other)
    assert handoff.fetch_task_secret(ref, context=CTX)["media_password"] == SECRET


def test_discard_removes_the_entry_and_tolerates_none(fake_redis):
    ref = handoff.stash_task_secret({"media_password": SECRET}, context=CTX)
    assert ref is not None
    handoff.discard_task_secret(ref)
    handoff.discard_task_secret(None)
    with pytest.raises(handoff.TaskSecretUnavailableError):
        handoff.fetch_task_secret(ref, context=CTX)


# ---------------------------------------------------------------- the dispatch site


def test_dispatch_passes_only_a_reference_to_the_broker(
    monkeypatch, fake_redis, db_session, normal_user
):
    mf = _make_media_file(db_session, normal_user)
    sent: list[dict[str, Any]] = []

    def _capture(*a, **kw):
        sent.append({"args": a, "kwargs": kw})
        return SimpleNamespace(id="t1")

    monkeypatch.setattr(process_youtube_url_task, "delay", _capture)
    _dispatch_video_task(
        db_session,
        mf,
        "https://m.example.com/view?m=abc",
        normal_user.id,
        media_username=USER,
        media_password=SECRET,
    )
    assert len(sent) == 1
    assert SECRET not in repr(sent) and USER not in repr(sent)
    ref = sent[0]["kwargs"]["media_credentials_ref"]
    ctx = handoff.media_credentials_context(normal_user.id, str(mf.uuid))
    assert handoff.fetch_task_secret(ref, context=ctx)["media_password"] == SECRET


def test_failed_dispatch_does_not_leave_the_secret_behind(
    monkeypatch, fake_redis, db_session, normal_user
):
    from fastapi import HTTPException

    mf = _make_media_file(db_session, normal_user)

    def _boom(*_a, **_kw):
        raise RuntimeError("broker down")

    monkeypatch.setattr(process_youtube_url_task, "delay", _boom)
    with pytest.raises(HTTPException):
        _dispatch_video_task(
            db_session, mf, "https://m.example.com/x", normal_user.id, media_password=SECRET
        )
    assert fake_redis.store == {}


# ---------------------------------------------------------------- the task side


def _make_media_file(db_session, user) -> MediaFile:
    mf = MediaFile(
        uuid=uuid_module.uuid4(),
        user_id=user.id,
        filename="video.mp4",
        title="Video",
        storage_path=f"user_{user.id}/{uuid_module.uuid4()}/video.mp4",
        file_size=1024,
        content_type="video/mp4",
        status=FileStatus.QUEUED,
        source_url="https://m.example.com/view?m=abc",
    )
    db_session.add(mf)
    db_session.commit()
    db_session.refresh(mf)
    return mf


class _Service:
    def __init__(self, exc: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._exc = exc

    def process_media_url_sync(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        return kwargs["media_file"]


@pytest.fixture
def seams(monkeypatch, db_session):
    monkeypatch.setattr(youtube_processing.settings, "YOUTUBE_PRE_DOWNLOAD_JITTER_ENABLED", False)
    monkeypatch.setattr(youtube_processing.settings, "YOUTUBE_AUTO_RETRY_ENABLED", True)

    @contextlib.contextmanager
    def _scope():
        yield db_session

    monkeypatch.setattr(youtube_processing, "session_scope", _scope)
    monkeypatch.setattr(
        youtube_processing, "send_youtube_notification_via_redis", lambda **_k: True
    )
    monkeypatch.setattr(youtube_processing, "dispatch_transcription_pipeline", lambda **_k: None)
    monkeypatch.setattr(youtube_processing, "send_ws_event_for_file", lambda *_a, **_k: None)


def _run(mf, user, ref, **kw):
    return process_youtube_url_task.apply(
        args=("https://m.example.com/view?m=abc", user.id, str(mf.uuid)),
        kwargs={"media_credentials_ref": ref},
        **kw,
    ).get()


def test_task_uses_the_stored_credentials_and_deletes_them_on_success(
    monkeypatch, fake_redis, seams, db_session, normal_user
):
    mf = _make_media_file(db_session, normal_user)
    svc = _Service()
    monkeypatch.setattr(youtube_processing, "MediaDownloadService", lambda: svc)
    ref = handoff.stash_task_secret(
        {"media_password": SECRET, "media_username": USER},
        context=handoff.media_credentials_context(normal_user.id, str(mf.uuid)),
    )
    assert ref is not None
    _run(mf, normal_user, ref)
    assert svc.calls[0]["media_password"] == SECRET
    assert svc.calls[0]["media_username"] == USER
    assert fake_redis.store == {}


def test_retry_within_ttl_still_has_the_credentials_and_terminal_failure_deletes_them(
    monkeypatch, fake_redis, seams, db_session, normal_user
):
    mf = _make_media_file(db_session, normal_user)
    svc = _Service(exc=RuntimeError("Connection timeout"))
    monkeypatch.setattr(youtube_processing, "MediaDownloadService", lambda: svc)
    ref = handoff.stash_task_secret(
        {"media_password": SECRET},
        context=handoff.media_credentials_context(normal_user.id, str(mf.uuid)),
    )
    assert ref is not None
    seen_after_first: list[bool] = []

    real_retry = Task.retry

    def _spy(self, *a, **kw):
        seen_after_first.append(f"task_secret:{ref}" in fake_redis.store)
        return real_retry(self, *a, **kw)

    monkeypatch.setattr(Task, "retry", _spy)
    result = _run(mf, normal_user, ref)
    assert result["status"] == "error"
    assert len(svc.calls) == 4 and all(c["media_password"] == SECRET for c in svc.calls)
    assert seen_after_first and all(seen_after_first), "kept while a retry is pending"
    assert fake_redis.store == {}, "removed once the failure is terminal"


def test_expired_credentials_fail_the_task_with_a_non_secret_error(
    monkeypatch, fake_redis, seams, db_session, normal_user
):
    mf = _make_media_file(db_session, normal_user)
    svc = _Service()
    monkeypatch.setattr(youtube_processing, "MediaDownloadService", lambda: svc)
    ref = handoff.stash_task_secret(
        {"media_password": SECRET},
        context=handoff.media_credentials_context(normal_user.id, str(mf.uuid)),
    )
    assert ref is not None
    fake_redis.now = TASK_SECRET_HANDOFF_TTL_SECONDS + 1
    result = _run(mf, normal_user, ref)
    assert result["status"] == "error" and "expired" in result["message"]
    assert SECRET not in result["message"]
    assert svc.calls == []
    db_session.refresh(mf)
    assert mf.status == FileStatus.ERROR


def test_secret_never_reaches_the_logs(
    monkeypatch, fake_redis, seams, db_session, normal_user, caplog
):
    mf = _make_media_file(db_session, normal_user)
    svc = _Service(exc=RuntimeError("Connection timeout"))
    monkeypatch.setattr(youtube_processing, "MediaDownloadService", lambda: svc)
    ref = handoff.stash_task_secret(
        {"media_password": SECRET, "media_username": USER},
        context=handoff.media_credentials_context(normal_user.id, str(mf.uuid)),
    )
    with caplog.at_level(logging.DEBUG):
        _run(mf, normal_user, ref)
    assert SECRET not in caplog.text and USER not in caplog.text


# ---------------------------------------------------------------- structural guard

_SECRETISH = re.compile(r"(password|passwd|secret|token|api_?key|cookie|credential)", re.I)
_REFERENCE_SUFFIXES = ("_ref", "_id", "_ids", "_uuid", "_count", "_limit", "_ttl")
_APP = Path(__file__).resolve().parents[2] / "app"


def _is_secret_name(name: str) -> bool:
    low = name.lower()
    return (
        bool(_SECRETISH.search(low))
        and not low.endswith(_REFERENCE_SUFFIXES)
        and "tokens" not in low
    )


def _app_trees():
    for path in _APP.rglob("*.py"):
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def test_no_celery_task_takes_or_receives_a_secret_by_name():
    offenders: list[str] = []
    for path, tree in _app_trees():
        rel = path.relative_to(_APP)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and any(
                "task" in ast.unparse(d) for d in node.decorator_list
            ):
                for a in node.args.args + node.args.kwonlyargs:
                    if _is_secret_name(a.arg):
                        offenders.append(f"{rel}:{node.lineno} task parameter {a.arg}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"delay", "apply_async", "s", "si", "send_task"}
            ):
                for kw in node.keywords:
                    if kw.arg and _is_secret_name(kw.arg):
                        offenders.append(f"{rel}:{node.lineno} dispatch argument {kw.arg}")
                    if kw.arg == "kwargs" and isinstance(kw.value, ast.Dict):
                        for key in kw.value.keys:
                            if (
                                isinstance(key, ast.Constant)
                                and isinstance(key.value, str)
                                and _is_secret_name(key.value)
                            ):
                                offenders.append(f"{rel}:{node.lineno} kwargs key {key.value}")
    assert offenders == [], (
        "pass an opaque reference (utils/task_secret_handoff.py), never the secret: "
        + "; ".join(offenders)
    )


def test_guard_detects_a_planted_offender():
    src = "@celery_app.task\ndef t(self, media_password=None):\n    pass\nt.delay(api_key=k)\n"
    tree = ast.parse(src)
    names = [a.arg for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) for a in n.args.args]
    assert any(_is_secret_name(n) for n in names)
    assert _is_secret_name("api_key") and not _is_secret_name("media_credentials_ref")
