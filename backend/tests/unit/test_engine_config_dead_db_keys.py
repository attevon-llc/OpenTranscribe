"""EngineConfig must not look up SystemSettings keys that nothing can write (issue #566).

``engine.gpu_split``, ``engine.precompute_vad`` and ``engine.shared_volume_path`` were read
from the database on every job although the engine-settings endpoint has no field for them,
so a stored value could only come from hand-written SQL. Their one source is the environment.
"""

from unittest.mock import MagicMock

from sqlalchemy.orm import Session

from app.api.endpoints.engine_settings import _KEYS
from app.transcription.engine.config import EngineConfig

DEAD_KEYS = {"engine.gpu_split", "engine.precompute_vad", "engine.shared_volume_path"}


def _requested_keys(monkeypatch) -> set[str]:
    requested: set[str] = set()

    def fake_map(_db, keys):
        requested.update(keys)
        return {key: "true" for key in keys}

    monkeypatch.setattr("app.services.system_settings_service.get_settings_map", fake_map)
    EngineConfig.from_db_with_env_fallback(MagicMock(spec=Session))
    return requested


def test_no_unwritable_key_is_looked_up(monkeypatch):
    assert not _requested_keys(monkeypatch) & DEAD_KEYS


def test_a_stored_value_cannot_flip_the_gpu_split(monkeypatch):
    monkeypatch.setenv("ENGINE_GPU_SPLIT", "false")
    _requested_keys(monkeypatch)
    assert EngineConfig.from_db_with_env_fallback(MagicMock(spec=Session)).gpu_split is False


def test_every_key_still_looked_up_has_a_writer(monkeypatch):
    """The other direction: the lookups that remain are the ones the endpoint writes."""
    looked_up = _requested_keys(monkeypatch)
    writable = set(_KEYS.values())
    assert looked_up
    assert looked_up <= writable, sorted(looked_up - writable)
