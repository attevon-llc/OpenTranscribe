"""``/api/admin/hf-token``: super_admin only, write-only, audited without the value."""

from __future__ import annotations

import logging

import pytest

from app.core.config import settings
from app.models.system_settings import SystemSettings
from app.services import huggingface_token as svc

URL = "/api/admin/hf-token"
TOKEN = "hf_" + "Q" * 34


@pytest.fixture(autouse=True)
def _isolated(db_session, monkeypatch):
    import app.db.base as db_base

    class KeepOpen:
        def __getattr__(self, name):
            return getattr(db_session, name)

        def close(self):
            return None

    monkeypatch.setattr(db_base, "SessionLocal", lambda: KeepOpen())
    monkeypatch.setattr(settings, "HUGGINGFACE_TOKEN", None)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.setattr(svc, "_published_by_us", False)
    svc.invalidate_cache()
    yield
    svc.invalidate_cache()


@pytest.fixture
def audit_events(monkeypatch):
    from app.api.endpoints import admin_hf_token

    captured: list[dict] = []
    monkeypatch.setattr(admin_hf_token.audit_logger, "log", lambda **kw: captured.append(kw))
    return captured


class TestWhoMayCall:
    @pytest.mark.parametrize(
        ("method", "kwargs"),
        [
            ("get", {}),
            ("put", {"json": {"token": TOKEN}}),
            ("delete", {}),
            ("post", {"json": {}}),
        ],
    )
    def test_plain_admin_is_denied(self, client, admin_token_headers, method, kwargs):
        suffix = "/test" if method == "post" else ""
        r = getattr(client, method)(URL + suffix, headers=admin_token_headers, **kwargs)
        assert r.status_code == 403

    @pytest.mark.parametrize("method", ["get", "put", "delete", "post"])
    def test_user_is_denied(self, client, user_token_headers, method):
        suffix = "/test" if method == "post" else ""
        kwargs = {"json": {"token": TOKEN}} if method in ("put", "post") else {}
        r = getattr(client, method)(URL + suffix, headers=user_token_headers, **kwargs)
        assert r.status_code == 403

    def test_anonymous_is_denied(self, client):
        assert client.get(URL).status_code == 401

    def test_a_denied_put_stores_nothing(self, client, admin_token_headers, db_session):
        client.put(URL, headers=admin_token_headers, json={"token": TOKEN})
        assert db_session.query(SystemSettings).filter_by(key=svc.SETTING_KEY).count() == 0


class TestSetClearStatus:
    def test_status_none_then_app_then_none(self, client, super_admin_token_headers):
        r = client.get(URL, headers=super_admin_token_headers)
        assert r.status_code == 200
        assert r.json() == {"is_set": False, "source": "none"}

        r = client.put(URL, headers=super_admin_token_headers, json={"token": TOKEN})
        assert r.status_code == 200
        assert r.json() == {"is_set": True, "source": "app"}
        assert client.get(URL, headers=super_admin_token_headers).json()["source"] == "app"

        r = client.delete(URL, headers=super_admin_token_headers)
        assert r.json() == {"is_set": False, "source": "none"}

    def test_environment_source_is_reported_and_app_overrides(
        self, client, super_admin_token_headers, monkeypatch
    ):
        monkeypatch.setattr(settings, "HUGGINGFACE_TOKEN", "hf_" + "E" * 34)
        assert client.get(URL, headers=super_admin_token_headers).json() == {
            "is_set": True,
            "source": "environment",
        }
        r = client.put(URL, headers=super_admin_token_headers, json={"token": TOKEN})
        assert r.json()["source"] == "app"
        r = client.delete(URL, headers=super_admin_token_headers)
        assert r.json()["source"] == "environment"

    def test_saved_token_reaches_the_resolver(self, client, super_admin_token_headers):
        client.put(URL, headers=super_admin_token_headers, json={"token": f"  {TOKEN}  "})
        assert svc.get_hf_token() == TOKEN

    def test_stored_encrypted(self, client, super_admin_token_headers, db_session):
        client.put(URL, headers=super_admin_token_headers, json={"token": TOKEN})
        row = db_session.query(SystemSettings).filter_by(key=svc.SETTING_KEY).one()
        assert TOKEN not in row.value


class TestValidation:
    @pytest.mark.parametrize(
        "bad",
        ["", "nope", "hf_short", "hf_" + "a" * 19, "hf_has space" + "a" * 30, "hf_" + "a" * 300],
    )
    def test_bad_shape_is_422_with_a_fixed_message(self, client, super_admin_token_headers, bad):
        r = client.put(URL, headers=super_admin_token_headers, json={"token": bad})
        assert r.status_code == 422
        assert (
            r.json()["detail"]
            == "The token is not in the expected form. A Hugging Face access token starts with hf_ and has no spaces."
        )
        if bad:
            assert bad not in r.text

    def test_missing_body_field_does_not_echo_anything(self, client, super_admin_token_headers):
        r = client.put(URL, headers=super_admin_token_headers, json={})
        assert r.status_code == 422


class TestNothingLeaks:
    def test_no_response_audit_or_log_carries_the_token(
        self, client, super_admin_token_headers, audit_events, caplog
    ):
        responses = []
        with caplog.at_level(logging.DEBUG):
            responses.append(
                client.put(URL, headers=super_admin_token_headers, json={"token": TOKEN})
            )
            responses.append(client.get(URL, headers=super_admin_token_headers))
            responses.append(client.delete(URL, headers=super_admin_token_headers))
            responses.append(
                client.put(URL, headers=super_admin_token_headers, json={"token": "bad " + TOKEN})
            )
        for r in responses:
            assert TOKEN not in r.text
        assert TOKEN not in caplog.text
        assert [e["details"]["action"] for e in audit_events] == ["set", "clear"]
        assert TOKEN not in repr(audit_events)
        assert all(e["details"]["setting"] == "huggingface_access_token" for e in audit_events)

    def test_status_response_has_no_token_derived_field(self, client, super_admin_token_headers):
        client.put(URL, headers=super_admin_token_headers, json={"token": TOKEN})
        body = client.get(URL, headers=super_admin_token_headers).json()
        assert set(body) == {"is_set", "source"}

    def test_clearing_nothing_writes_no_audit_entry(
        self, client, super_admin_token_headers, audit_events
    ):
        client.delete(URL, headers=super_admin_token_headers)
        assert audit_events == []


class TestTestAction:
    def test_valid(self, client, super_admin_token_headers, monkeypatch):
        monkeypatch.setattr(svc, "check_token", lambda token: svc.TEST_VALID)
        r = client.post(URL + "/test", headers=super_admin_token_headers, json={"token": TOKEN})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "code": "valid", "message": svc.TEST_VALID}

    def test_saved_token_is_tested_when_no_body_and_nothing_is_stored_for_an_unsaved_one(
        self, client, super_admin_token_headers, monkeypatch, db_session
    ):
        seen: list[str] = []

        def record(token: str) -> str:
            seen.append(token)
            return svc.TEST_VALID

        monkeypatch.setattr(svc, "check_token", record)
        client.post(URL + "/test", headers=super_admin_token_headers, json={"token": TOKEN})
        assert db_session.query(SystemSettings).filter_by(key=svc.SETTING_KEY).count() == 0
        r = client.post(URL + "/test", headers=super_admin_token_headers)
        assert r.status_code == 404  # nothing saved, nothing in the environment
        client.put(URL, headers=super_admin_token_headers, json={"token": "hf_" + "S" * 34})
        client.post(URL + "/test", headers=super_admin_token_headers)
        assert seen == [TOKEN, "hf_" + "S" * 34]

    def test_a_rejected_token_reports_invalid(self, client, super_admin_token_headers, monkeypatch):
        monkeypatch.setattr(svc, "check_token", lambda token: svc.TEST_INVALID)
        r = client.post(URL + "/test", headers=super_admin_token_headers, json={"token": TOKEN})
        assert r.json() == {"ok": False, "code": "invalid", "message": svc.TEST_INVALID}

    def test_failures_are_fixed_sentences_never_the_exception(
        self, client, super_admin_token_headers, monkeypatch
    ):
        import requests

        def boom(*a, **kw):
            raise requests.ConnectionError(f"secret detail for {kw['headers']} user=alice")

        monkeypatch.setattr(requests, "get", boom)
        r = client.post(URL + "/test", headers=super_admin_token_headers, json={"token": TOKEN})
        assert r.status_code == 200
        assert r.json() == {"ok": False, "code": "unreachable", "message": svc.TEST_UNREACHABLE}
        assert "alice" not in r.text and TOKEN not in r.text
