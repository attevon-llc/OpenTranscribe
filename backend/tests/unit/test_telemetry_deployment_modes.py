"""The usage report is forced off in offline, PKI and FIPS deployments, and has a debug switch.

Issue #1019. Nothing here reaches a real host: ``httpx.Client.send`` is intercepted.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import yaml
from sqlalchemy.orm import Session

from app.core.config import settings
from app.services import telemetry

_DB = MagicMock(spec=Session)
REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def allowed(monkeypatch):
    for name in (
        "DO_NOT_TRACK",
        "OPENTRANSCRIBE_TELEMETRY",
        "OPENTRANSCRIBE_DEMO_MODE",
        "HF_HUB_OFFLINE",
        "OPENTRANSCRIBE_TELEMETRY_DEBUG",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(settings, "PKI_ENABLED", False)
    monkeypatch.setattr(settings, "FIPS_MODE", False)
    monkeypatch.setenv("OPENTRANSCRIBE_TELEMETRY_PROJECT_KEY", "phc_test_key")


def _restrict(monkeypatch, name):
    """PKI_ENABLED and FIPS_MODE are Settings fields; HF_HUB_OFFLINE is read from the env."""
    if name == "HF_HUB_OFFLINE":
        monkeypatch.setenv(name, "1")
    else:
        monkeypatch.setattr(settings, name, True)


@pytest.fixture
def posts(monkeypatch):
    sent: list[httpx.Request] = []

    def fake_send(self, request, **_kwargs):
        sent.append(request)
        return httpx.Response(200, json={"status": 1}, request=request)

    monkeypatch.setattr(httpx.Client, "send", fake_send)
    return sent


def test_nothing_forces_it_off_in_a_plain_deployment(allowed):
    assert telemetry.off_reasons() == []


@pytest.mark.parametrize(
    ("name", "reason"),
    [("HF_HUB_OFFLINE", "offline"), ("PKI_ENABLED", "pki"), ("FIPS_MODE", "fips")],
)
def test_each_restricted_deployment_forces_it_off(allowed, monkeypatch, name, reason):
    _restrict(monkeypatch, name)

    assert telemetry.off_reasons() == [reason]


def test_an_explicit_no_does_not_force_it_off(allowed, monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")

    assert telemetry.off_reasons() == []


@pytest.fixture
def stored_on(monkeypatch):
    """The admin switch is stored on; the database is replaced by in-memory state."""
    written: dict[str, str] = {}
    monkeypatch.setattr(
        telemetry,
        "read_state",
        lambda _db: {
            "enabled": True,
            "install_id": "3f2b8c1e-7d4a-4f0e-9b61-2c5a8e9d1f07",
            "last_sent_at": None,
            "last_attempt_at": None,
        },
    )
    monkeypatch.setattr(
        telemetry, "build_request", lambda *_a, **_k: {"event": "opentranscribe_heartbeat"}
    )
    monkeypatch.setattr(
        telemetry, "set_setting", lambda _db, key, value, *_a: written.update({key: value})
    )
    return written


@pytest.mark.parametrize("name", ["HF_HUB_OFFLINE", "PKI_ENABLED", "FIPS_MODE"])
def test_a_stored_on_switch_sends_nothing_in_a_restricted_deployment(
    allowed, posts, stored_on, monkeypatch, name
):
    _restrict(monkeypatch, name)

    assert telemetry.send_if_due(_DB) == "blocked"
    assert posts == []


def test_turning_it_on_is_refused_in_a_restricted_deployment(allowed, monkeypatch):
    monkeypatch.setattr(settings, "FIPS_MODE", True)

    with pytest.raises(telemetry.TelemetryRefusedError) as refused:
        telemetry.set_enabled(_DB, True)
    assert refused.value.reasons == ["fips"]


def test_debug_logs_the_request_and_sends_nothing(allowed, posts, stored_on, monkeypatch, caplog):
    monkeypatch.setenv("OPENTRANSCRIBE_TELEMETRY_DEBUG", "true")

    with caplog.at_level("INFO", logger=telemetry.logger.name):
        result = telemetry.send_if_due(_DB)

    assert result == "debug"
    assert posts == []
    assert "opentranscribe_heartbeat" in caplog.text
    assert telemetry.constants.TELEMETRY_LAST_SENT_AT_KEY not in stored_on


def test_without_debug_the_same_state_sends(allowed, posts, stored_on):
    assert telemetry.send_if_due(_DB) == "sent"
    assert len(posts) == 1


@pytest.mark.parametrize("overlay", ["docker-compose.pki.yml"])
def test_the_overlay_forces_the_report_off_for_the_services_that_send(overlay):
    services = yaml.safe_load((REPO_ROOT / overlay).read_text())["services"]

    base = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())["services"]
    # telemetry.usage_report routes to `utility`; find the worker that consumes it.
    senders = [n for n, svc in base.items() if "utility" in str(svc.get("command", ""))]
    assert senders, "no worker consumes the utility queue"

    for name in ("backend", *senders):
        assert services[name]["environment"]["OPENTRANSCRIBE_TELEMETRY"] == "false"


def test_the_offline_overlay_sets_the_offline_switch_on_the_sending_worker():
    services = yaml.safe_load((REPO_ROOT / "docker-compose.offline.yml").read_text())["services"]

    assert "HF_HUB_OFFLINE=1" in services["celery-worker"]["environment"]


def test_the_root_telemetry_page_names_the_switches_and_every_field():
    page = (REPO_ROOT / "TELEMETRY.md").read_text()

    needles = (*telemetry.PAYLOAD_FIELDS, "DO_NOT_TRACK", "OPENTRANSCRIBE_TELEMETRY_DEBUG")

    assert [needle for needle in needles if needle not in page] == []
