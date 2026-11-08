"""The SSE progress streams share one async Redis pool per event loop (audit L9).

``download_stream`` and ``bulk_export_stream`` each built a fresh ``redis.asyncio`` client,
and with it a fresh connection pool, for every stream a browser opened, and tore it down
when the stream ended. They now borrow from ``get_async_redis()``: one client per event
loop, whose pub/sub connections go back to the pool and which no stream closes.
"""

from __future__ import annotations

import asyncio
import json
from typing import cast

from sqlalchemy.orm import Session

from app.api.endpoints.files import subtitles
from app.core import redis as redis_core
from app.models.user import User


class _Client:
    def __init__(self, result: dict) -> None:
        self._result = result
        self.closed = False

    async def get(self, _key):
        return json.dumps(self._result)

    async def close(self) -> None:
        self.closed = True

    async def aclose(self) -> None:
        self.closed = True


def _count_clients(monkeypatch) -> list[_Client]:
    built: list[_Client] = []

    def _from_url(*_args, **_kwargs):
        client = _Client({"url": "https://storage.test/x.zip", "filename": "x.zip", "file_ids": []})
        built.append(client)
        return client

    monkeypatch.setattr("redis.asyncio.from_url", _from_url)
    monkeypatch.setattr(
        "app.services.media_revocation_cache.hidden_from_requester", lambda *_a: False
    )
    return built


async def _first_frame(response) -> str:
    async for frame in response.body_iterator:
        return str(frame)
    raise AssertionError("the stream closed without emitting a frame")


def test_two_streams_on_one_loop_share_one_client_and_leave_it_open(monkeypatch):
    built = _count_clients(monkeypatch)
    owner = User(id=9001)

    async def _two_streams() -> list[str]:
        frames = []
        for _ in range(2):
            job = subtitles.new_bulk_job_id(owner.id)
            response = subtitles.bulk_export_stream(
                job=job, db=cast("Session", _Session()), current_user=owner
            )
            frames.append(await _first_frame(response))
        return frames

    frames = asyncio.run(_two_streams())

    assert all(frame.startswith("event: ready") for frame in frames), frames
    assert len(built) == 1, f"{len(built)} Redis clients were built for two streams"
    assert built[0].closed is False, "a stream closed the shared client"


def test_a_new_event_loop_gets_its_own_client(monkeypatch):
    """A pool is bound to the loop it was made on, so it must never cross loops."""
    built = _count_clients(monkeypatch)

    async def _get() -> object:
        return redis_core.get_async_redis()

    first, second = asyncio.run(_get()), asyncio.run(_get())

    assert first is not second
    assert len(built) == 2


class _Session:
    def close(self) -> None:
        return None
