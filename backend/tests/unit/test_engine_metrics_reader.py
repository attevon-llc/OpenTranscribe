"""``GET /engine-settings/metrics`` decodes Redis hashes whatever mix of bytes/str they carry.

The reader used to assume every key was ``bytes`` (``k.startswith(b"last_")``). With redis-py's
own inline types, ``hgetall`` is ``dict[bytes | str, ...]``; a ``str``-keyed hash (a client built
with ``decode_responses=True``) raised inside the per-host ``try`` and the host silently vanished
from the response.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.api.endpoints import engine_settings

pytestmark = pytest.mark.unit


class _FakeRedis:
    def __init__(self, hashes: dict[bytes | str, dict[bytes | str, bytes | str]]):
        self._hashes = hashes

    def keys(self, pattern: str) -> list[bytes | str]:
        return list(self._hashes)

    def hgetall(self, key: bytes | str) -> dict[bytes | str, bytes | str]:
        return self._hashes[key]


def _read(monkeypatch: pytest.MonkeyPatch, hashes: dict) -> dict:
    monkeypatch.setattr("app.core.redis.get_redis", lambda: _FakeRedis(hashes))
    result = engine_settings.get_engine_metrics(current_user=MagicMock())
    assert isinstance(result, dict)
    return result


def test_bytes_hash_is_decoded(monkeypatch):
    result = _read(
        monkeypatch,
        {
            b"engine:metrics:gpu-a": {
                b"gpu_ready_queue_depth": b"3",
                b"gpu_in_flight": b"1",
                b"gpu_idle_seconds": b"2.5",
                b"last_transcribe_s": b"4.25",
                b"unrelated": b"ignored",
            }
        },
    )

    assert result == {
        "gpu-a": {
            "gpu_ready_queue_depth": 3,
            "gpu_in_flight": 1,
            "gpu_idle_seconds": 2.5,
            "last_stage_durations": {"transcribe": 4.25},
        }
    }


def test_str_hash_is_decoded_too_not_dropped(monkeypatch):
    result = _read(
        monkeypatch,
        {
            "engine:metrics:gpu-b": {
                "gpu_ready_queue_depth": "7",
                "last_diarize_s": "1.5",
            }
        },
    )

    assert result["gpu-b"]["gpu_ready_queue_depth"] == 7
    assert result["gpu-b"]["last_stage_durations"] == {"diarize": 1.5}


def test_a_host_with_no_counters_reports_zeroes(monkeypatch):
    result = _read(monkeypatch, {b"engine:metrics:idle": {}})

    assert result["idle"] == {
        "gpu_ready_queue_depth": 0,
        "gpu_in_flight": 0,
        "gpu_idle_seconds": 0.0,
        "last_stage_durations": {},
    }
