"""Tests for the admin engine-settings endpoint.

Focuses on the ``boundary_smoothing_enabled`` toggle (issue #193): setting it via the
update endpoint must persist to ``SystemSettings`` under the ``engine.boundary_smoothing_enabled``
key and be reflected on the subsequent GET with ``source == "db"``. Uses the real PostgreSQL
``db_session`` fixture with savepoint rollback — no mocks for the data layer.
"""

import pytest

from app.services.system_settings_service import get_setting

_BASE = "/api/admin/engine-settings"
_DB_KEY = "engine.boundary_smoothing_enabled"

# This file and test_engine_settings_endpoints.py both write engine.boundary_* keys
# through the same POST /update endpoint with no coordination between them — under
# `-n auto` two workers inserting overlapping keys in different orders can deadlock on
# the system_settings_key_key unique index (issue #389, same mechanism as
# test_backup_metrics.py's "backup_system_settings" group).
pytestmark = pytest.mark.xdist_group("engine_system_settings")


class TestEngineSettingsBoundarySmoothing:
    """GET/POST/DELETE coverage for the boundary smoothing toggle."""

    def test_get_includes_boundary_smoothing_default(self, client, super_admin_token_headers):
        """GET exposes boundary_smoothing_enabled as a boolean with an annotated source.

        Resets any DB override first so the result is deterministic regardless of
        pre-existing branch state, then asserts the key falls back to env/default.
        """
        client.delete(f"{_BASE}/boundary_smoothing_enabled", headers=super_admin_token_headers)
        resp = client.get(_BASE, headers=super_admin_token_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "boundary_smoothing_enabled" in data
        entry = data["boundary_smoothing_enabled"]
        assert isinstance(entry["value"], bool)
        assert entry["source"] in ("env", "default")

    def test_set_boundary_smoothing_persists(self, client, super_admin_token_headers, db_session):
        """POST update writes the engine.boundary_smoothing_enabled key to SystemSettings."""
        resp = client.post(
            f"{_BASE}/update",
            json={"boundary_smoothing_enabled": True},
            headers=super_admin_token_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["boundary_smoothing_enabled"]["value"] is True
        assert body["boundary_smoothing_enabled"]["source"] == "db"

        # Directly assert the persisted DB value (string "true" per set_setting bool handling).
        assert get_setting(db_session, _DB_KEY) == "true"

    def test_set_then_get_reflects_db_value(self, client, super_admin_token_headers):
        """A value set via POST is reflected on a fresh GET as a db override."""
        client.post(
            f"{_BASE}/update",
            json={"boundary_smoothing_enabled": True},
            headers=super_admin_token_headers,
        )
        resp = client.get(_BASE, headers=super_admin_token_headers)
        assert resp.status_code == 200
        entry = resp.json()["boundary_smoothing_enabled"]
        assert entry["value"] is True
        assert entry["source"] == "db"

    def test_reset_boundary_smoothing_removes_db_override(
        self, client, super_admin_token_headers, db_session
    ):
        """DELETE reverts the key to env/default by removing the DB row."""
        client.post(
            f"{_BASE}/update",
            json={"boundary_smoothing_enabled": True},
            headers=super_admin_token_headers,
        )
        assert get_setting(db_session, _DB_KEY) == "true"

        resp = client.delete(
            f"{_BASE}/boundary_smoothing_enabled", headers=super_admin_token_headers
        )
        assert resp.status_code == 204
        assert get_setting(db_session, _DB_KEY) is None

        get_resp = client.get(_BASE, headers=super_admin_token_headers)
        assert get_resp.json()["boundary_smoothing_enabled"]["source"] in ("env", "default")


class TestEngineSettingsDiarizerBackend:
    """The diarizer_backend dropdown (issue #58) must actually select something.

    Before this it had exactly one option ('pyannote') and the resolved value was never
    read by anything — an inert control. It is now the DB-backed half of
    ``TranscriptionConfig.diarizer_backend``, validated against
    ``engine.backends.VALID_DIARIZER_BACKENDS``.
    """

    _DIARIZER_DB_KEY = "engine.diarizer_backend"

    def test_default_is_native(self, client, super_admin_token_headers):
        """With no DB override and no env var, native (the primary engine) is the default."""
        client.delete(f"{_BASE}/diarizer_backend", headers=super_admin_token_headers)
        resp = client.get(_BASE, headers=super_admin_token_headers)
        assert resp.status_code == 200
        entry = resp.json()["diarizer_backend"]
        assert entry["source"] in ("env", "default")
        if entry["source"] == "default":
            assert entry["value"] == "native"

    def test_set_pyannote_persists_and_reflects(
        self, client, super_admin_token_headers, db_session
    ):
        """The failover can still be pinned directly, and it round-trips through the DB."""
        resp = client.post(
            f"{_BASE}/update",
            json={"diarizer_backend": "pyannote"},
            headers=super_admin_token_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["diarizer_backend"] == {"value": "pyannote", "source": "db"}
        assert get_setting(db_session, self._DIARIZER_DB_KEY) == "pyannote"

        get_resp = client.get(_BASE, headers=super_admin_token_headers)
        assert get_resp.json()["diarizer_backend"] == {"value": "pyannote", "source": "db"}

    def test_set_native_persists_and_reflects(self, client, super_admin_token_headers, db_session):
        resp = client.post(
            f"{_BASE}/update",
            json={"diarizer_backend": "native"},
            headers=super_admin_token_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["diarizer_backend"] == {"value": "native", "source": "db"}
        assert get_setting(db_session, self._DIARIZER_DB_KEY) == "native"

    def test_unknown_backend_is_rejected(self, client, super_admin_token_headers, db_session):
        """The registry gates writes: a name nothing implements must not persist."""
        resp = client.post(
            f"{_BASE}/update",
            json={"diarizer_backend": "nemo-not-registered"},
            headers=super_admin_token_headers,
        )
        assert resp.status_code == 400
        assert get_setting(db_session, self._DIARIZER_DB_KEY) is None

    def test_reset_diarizer_backend_removes_db_override(
        self, client, super_admin_token_headers, db_session
    ):
        client.post(
            f"{_BASE}/update",
            json={"diarizer_backend": "pyannote"},
            headers=super_admin_token_headers,
        )
        assert get_setting(db_session, self._DIARIZER_DB_KEY) == "pyannote"

        resp = client.delete(f"{_BASE}/diarizer_backend", headers=super_admin_token_headers)
        assert resp.status_code == 204
        assert get_setting(db_session, self._DIARIZER_DB_KEY) is None
