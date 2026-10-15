"""The Hugging Face token resolver: precedence, encryption at rest, TTL cache, fallbacks."""

from __future__ import annotations

import logging
import os
from typing import Any

import pytest

from app.core.config import settings
from app.models.system_settings import SystemSettings
from app.services import huggingface_token as svc

_APP = "hf_" + "A" * 34
_ENV = "hf_" + "E" * 34


class _KeepOpen:
    """Stand-in for ``SessionLocal()``: the test's savepointed session, close() a no-op."""

    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self):
        return None


@pytest.fixture
def resolver(db_session, monkeypatch):
    import app.db.base as db_base

    monkeypatch.setattr(db_base, "SessionLocal", lambda: _KeepOpen(db_session))
    monkeypatch.setattr(settings, "HUGGINGFACE_TOKEN", None)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    monkeypatch.setattr(svc, "_published_by_us", False)
    svc.invalidate_cache()
    yield svc
    svc.invalidate_cache()
    os.environ.pop("HF_TOKEN", None)


class TestPrecedence:
    def test_nothing_set_is_none(self, resolver):
        assert resolver.get_hf_token() is None

    def test_environment_used_when_no_app_value(self, resolver, monkeypatch):
        monkeypatch.setattr(settings, "HUGGINGFACE_TOKEN", _ENV)
        assert resolver.get_hf_token() == _ENV

    def test_app_value_beats_environment(self, resolver, db_session, monkeypatch):
        monkeypatch.setattr(settings, "HUGGINGFACE_TOKEN", _ENV)
        resolver.save_token(db_session, _APP)
        assert resolver.get_hf_token() == _APP

    def test_clearing_falls_back_to_environment(self, resolver, db_session, monkeypatch):
        monkeypatch.setattr(settings, "HUGGINGFACE_TOKEN", _ENV)
        resolver.save_token(db_session, _APP)
        assert resolver.clear_token(db_session) is True
        assert resolver.get_hf_token() == _ENV
        assert resolver.clear_token(db_session) is False

    def test_status_reports_source(self, resolver, db_session, monkeypatch):
        assert resolver.get_status(db_session) == (False, "none")
        monkeypatch.setattr(settings, "HUGGINGFACE_TOKEN", _ENV)
        assert resolver.get_status(db_session) == (True, "environment")
        resolver.save_token(db_session, _APP)
        assert resolver.get_status(db_session) == (True, "app")


class TestEncryptedAtRest:
    def test_stored_value_is_not_the_token(self, resolver, db_session):
        resolver.save_token(db_session, _APP)
        row = db_session.query(SystemSettings).filter_by(key=resolver.SETTING_KEY).one()
        assert row.value and _APP not in row.value and "AAAA" not in row.value
        assert row.description and _APP not in row.description

    def test_replace_keeps_one_row(self, resolver, db_session):
        resolver.save_token(db_session, _APP)
        resolver.save_token(db_session, "hf_" + "B" * 34)
        rows = db_session.query(SystemSettings).filter_by(key=resolver.SETTING_KEY).all()
        assert len(rows) == 1
        assert resolver.get_hf_token() == "hf_" + "B" * 34


class TestCache:
    def test_second_call_does_not_read_the_database(self, resolver, db_session, monkeypatch):
        resolver.save_token(db_session, _APP)
        resolver.invalidate_cache()
        assert resolver.get_hf_token() == _APP
        # a write that bypasses save_token (so the cache is not busted) is not seen yet
        db_session.query(SystemSettings).filter_by(key=resolver.SETTING_KEY).delete()
        db_session.commit()
        assert resolver.get_hf_token() == _APP

    def test_expires_after_the_ttl(self, resolver, db_session, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(svc, "_clock", lambda: now[0])
        resolver.save_token(db_session, _APP)
        resolver.invalidate_cache()
        assert resolver.get_hf_token() == _APP
        db_session.query(SystemSettings).filter_by(key=resolver.SETTING_KEY).delete()
        db_session.commit()
        now[0] += resolver.CACHE_TTL_SECONDS - 1
        assert resolver.get_hf_token() == _APP
        now[0] += 2
        assert resolver.get_hf_token() is None  # picked up without a restart

    def test_save_busts_the_cache_in_this_process(self, resolver, db_session):
        assert resolver.get_hf_token() is None
        resolver.save_token(db_session, _APP)
        assert resolver.get_hf_token() == _APP

    def test_a_database_failure_falls_back_to_env_and_is_not_cached(
        self, resolver, db_session, monkeypatch
    ):
        monkeypatch.setattr(settings, "HUGGINGFACE_TOKEN", _ENV)
        resolver.save_token(db_session, _APP)
        resolver.invalidate_cache()

        def boom(_db):
            raise RuntimeError("db down")

        monkeypatch.setattr(svc, "_read_stored", boom)
        assert resolver.get_hf_token() == _ENV
        monkeypatch.undo()  # restore real reader (and settings)
        # not cached: the next call asks the database again
        assert svc._cache is None


class TestSessionDiscipline:
    def test_the_resolver_closes_the_session_it_opens(self, db_session, monkeypatch):
        import app.db.base as db_base

        closed = []

        class Tracking(_KeepOpen):
            def close(self):
                closed.append(True)

        monkeypatch.setattr(db_base, "SessionLocal", lambda: Tracking(db_session))
        monkeypatch.setattr(settings, "HUGGINGFACE_TOKEN", None)
        svc.invalidate_cache()
        svc.get_hf_token()
        assert closed == [True]
        svc.invalidate_cache()


class TestPublishToHubEnvironment:
    def test_resolving_exports_hf_token(self, resolver, monkeypatch):
        monkeypatch.setattr(settings, "HUGGINGFACE_TOKEN", _ENV)
        resolver.get_hf_token()
        assert os.environ["HF_TOKEN"] == _ENV

    def test_a_changed_app_token_replaces_what_we_published(self, resolver, db_session):
        resolver.save_token(db_session, _APP)
        resolver.get_hf_token()
        resolver.save_token(db_session, "hf_" + "C" * 34)
        resolver.get_hf_token()
        assert os.environ["HF_TOKEN"] == "hf_" + "C" * 34

    def test_an_operator_supplied_hf_token_wins(self, resolver, db_session, monkeypatch):
        monkeypatch.setenv("HF_TOKEN", "operator-token")
        resolver.save_token(db_session, _APP)
        assert resolver.get_hf_token() == _APP  # explicit loaders still get the saved one
        assert os.environ["HF_TOKEN"] == "operator-token"

    def test_the_fork_hook_publishes_only_the_environment_token(
        self, resolver, db_session, monkeypatch
    ):
        from app.core.celery import publish_hf_token_to_environment

        resolver.save_token(db_session, _APP)
        # saved app token is NOT consulted (no DB in a fork)
        assert publish_hf_token_to_environment() is False
        monkeypatch.setattr(settings, "HUGGINGFACE_TOKEN", _ENV)
        assert publish_hf_token_to_environment() is True
        assert os.environ["HF_TOKEN"] == _ENV
        assert publish_hf_token_to_environment() is False  # already published


class TestNeverLogged:
    def test_no_log_line_contains_the_token(self, resolver, db_session, caplog):
        with caplog.at_level(logging.DEBUG):
            resolver.save_token(db_session, _APP)
            resolver.get_hf_token()
            resolver.clear_token(db_session)
        assert _APP not in caplog.text


class TestValidation:
    @pytest.mark.parametrize(
        "value",
        ["hf_" + "a" * 20, "  hf_" + "Z9" * 20 + "  ", "hf_" + "x" * 200],
    )
    def test_accepts_the_usual_shape(self, value):
        assert svc.validate_token(value) == value.strip()

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "hf_short",
            "abc" + "d" * 40,
            "hf_" + "a" * 19,
            "hf_ " + "a" * 30,
            "hf_a b" + "c" * 30,
            "hf_" + "a" * 300,
            "hf_" + "a" * 20 + "\n" + "b" * 5,
            "hf_" + "é" * 30,
        ],
    )
    def test_rejects_everything_else(self, value):
        with pytest.raises(ValueError) as raised:
            svc.validate_token(value)
        assert value.strip() not in str(raised.value) or not value.strip()


class TestCheckToken:
    @staticmethod
    def _respond(monkeypatch, status):
        import requests

        seen: dict[str, Any] = {}

        def fake_get(url, headers=None, timeout=None):
            seen.update(url=url, headers=headers, timeout=timeout)
            resp = requests.Response()
            resp.status_code = status
            return resp

        monkeypatch.setattr(requests, "get", fake_get)
        return seen

    def test_valid_sends_the_token_with_a_bounded_timeout(self, monkeypatch):
        seen = self._respond(monkeypatch, 200)
        assert svc.check_token(_APP) == svc.TEST_VALID
        assert seen["url"].endswith("/api/whoami-v2")
        assert seen["headers"] == {"Authorization": f"Bearer {_APP}"}
        assert 0 < seen["timeout"] <= 10

    @pytest.mark.parametrize("status", [401, 403])
    def test_rejected_token_is_the_fixed_invalid_sentence(self, monkeypatch, status):
        self._respond(monkeypatch, status)
        assert svc.check_token(_APP) == svc.TEST_INVALID

    @pytest.mark.parametrize("status", [429, 500, 503])
    def test_a_hub_outage_is_not_blamed_on_the_token(self, monkeypatch, status):
        self._respond(monkeypatch, status)
        assert svc.check_token(_APP) == svc.TEST_UNREACHABLE

    def test_network_failure_is_the_fixed_unreachable_sentence(self, monkeypatch):
        import requests

        def down(*a, **kw):
            raise requests.ConnectionError(f"cannot reach with {kw['headers']}")

        monkeypatch.setattr(requests, "get", down)
        assert svc.check_token(_APP) == svc.TEST_UNREACHABLE
