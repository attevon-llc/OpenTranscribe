"""Branches of the download SSE relay: keepalive, Redis failure, bad payload, terminal frame."""

import asyncio
import json

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.api.endpoints.files.media_delivery import _relay_download_events


class _FakePubSub:
    """Replays a script: dict -> message, None -> no message, Exception -> raised."""

    def __init__(self, script):
        self._script = list(script)

    async def get_message(self, **_kwargs):
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _msg(payload):
    return {"data": json.dumps(payload) if not isinstance(payload, str) else payload}


def _collect(script, mode="audio_mp3", variant=""):
    async def run():
        return [f async for f in _relay_download_events(_FakePubSub(script), "f-1", mode, variant)]

    return asyncio.run(run())


def test_timeout_and_empty_read_yield_keepalives_then_ready_ends_stream():
    ready = {"mode": "audio_mp3", "status": "completed", "url": "http://u", "filename": "a.mp3"}
    frames = _collect([RedisTimeoutError(), None, _msg(ready), _msg(ready)])
    assert frames[0] == ": keepalive\n\n"
    assert frames[1] == ": keepalive\n\n"
    assert len(frames) == 3  # the second ready message is never consumed
    assert frames[2].startswith("event: ready\n")
    assert json.loads(frames[2].split("data: ")[1]) == {"url": "http://u", "filename": "a.mp3"}


def test_redis_error_emits_connection_interrupted_and_stops():
    frames = _collect([RedisConnectionError("boom"), _msg({"mode": "audio_mp3"})])
    assert len(frames) == 1
    assert frames[0].startswith("event: error\n")
    assert json.loads(frames[0].split("data: ")[1])["code"] == "connection_interrupted"


def test_malformed_and_foreign_events_are_skipped_progress_is_relayed():
    progress = {"mode": "audio_mp3", "status": "processing", "message": "x", "progress": 40}
    other_mode = {"mode": "audio_wav", "status": "error"}
    err = {"mode": "audio_mp3", "status": "error", "message": "bad"}
    frames = _collect(
        [_msg("not json"), {"no_data": 1}, _msg(other_mode), _msg(progress), _msg(err)]
    )
    assert [f.split("\n")[0] for f in frames] == ["event: progress", "event: error"]
    assert json.loads(frames[1].split("data: ")[1]) == {"message": "bad"}
