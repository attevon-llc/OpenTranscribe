"""Issue #791: the Alertmanager webhook is authenticated and unreachable through nginx."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.endpoints import alertmanager_webhook as mod
from app.db.base import get_db

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
PATH = "/internal/alertmanager/webhook"
TOKEN = "s3cret-token-value"
PAYLOAD = {
    "status": "firing",
    "alerts": [
        {
            "status": "firing",
            "labels": {"alertname": "AuditOutboxStuck"},
            "annotations": {"summary": "stuck"},
        },
        {"status": "resolved", "labels": {"alertname": "Old"}, "annotations": {"summary": "gone"}},
    ],
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ALERTMANAGER_WEBHOOK_TOKEN", TOKEN)
    app = FastAPI()
    app.include_router(mod.router)
    app.dependency_overrides[get_db] = lambda: MagicMock()
    return TestClient(app)


def test_missing_or_wrong_token_is_refused_and_notifies_nobody(client):
    with patch("app.services.backup_alerts._notify_admins") as notify:
        assert client.post(PATH, json=PAYLOAD).status_code == 401
        assert (
            client.post(PATH, json=PAYLOAD, headers={"Authorization": "Bearer nope"}).status_code
            == 401
        )
        assert (
            client.post(PATH, json=PAYLOAD, headers={"Authorization": f"Basic {TOKEN}"}).status_code
            == 401
        )
    notify.assert_not_called()


def test_unset_token_fails_closed_even_for_an_empty_bearer(client, monkeypatch):
    monkeypatch.setenv("ALERTMANAGER_WEBHOOK_TOKEN", "")
    with patch("app.services.backup_alerts._notify_admins") as notify:
        assert (
            client.post(PATH, json=PAYLOAD, headers={"Authorization": "Bearer "}).status_code == 401
        )
    notify.assert_not_called()


def test_valid_token_notifies_admins_of_firing_alerts_only(client):
    with patch("app.services.backup_alerts._notify_admins") as notify:
        resp = client.post(PATH, json=PAYLOAD, headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200
    assert resp.json() == {"firing": 1}
    notify.assert_called_once()
    assert notify.call_args.kwargs["message"] == "AuditOutboxStuck: stuck"
    assert notify.call_args.kwargs["event_type"] == mod.EVENT_TYPE


def _render_https_server() -> str:
    """Render nginx/site.conf.template the way the container's envsubst does."""
    text = (REPO_ROOT / "nginx" / "site.conf.template").read_text(encoding="utf-8")
    values = {
        "NGINX_SERVER_NAME": "t.example",
        "NGINX_HTTPS_REDIRECT_SUFFIX": "",
        "FLOWER_AUTH_B64": "x",
    }
    return re.sub(r"\$\{(\w+)\}", lambda m: values[m.group(1)], text)


def _locations(rendered: str) -> list[tuple[str, str, str]]:
    """(modifier, path, body) for every ``location`` block."""
    out = []
    for m in re.finditer(r"location\s+(\^~|=|~\*|~)?\s*(\S+)\s*\{", rendered):
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(rendered[i], 0)
            i += 1
        out.append((m.group(1) or "", m.group(2), rendered[m.end() : i - 1]))
    return out


def test_rendered_nginx_denies_the_webhook_path_ahead_of_the_catch_all():
    locs = _locations(_render_https_server())
    # nginx picks the longest matching prefix; a ^~ prefix then skips regex locations.
    prefixes = [(m, p, b) for m, p, b in locs if m in ("", "^~") and PATH.startswith(p)]
    mod_, path, body = max(prefixes, key=lambda t: len(t[1]))
    assert (mod_, path) == ("^~", "/internal/")
    assert "deny all;" in body and "return 404;" in body and "proxy_pass" not in body
    assert not any(m == "=" and p == PATH for m, p, _ in locs)
