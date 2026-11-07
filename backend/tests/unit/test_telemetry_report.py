"""The opt-in anonymous usage report (issue #1019, ``app/services/telemetry.py``).

Every network call is intercepted at ``httpx.Client.send``; nothing here reaches a real host.
The "sends nothing by default" proofs live in ``test_telemetry_disabled.py``; this file covers
the payload, the opt-in lifecycle, the schedule and the wiring.
"""

from __future__ import annotations

import json
import re
import socket
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from app.core import constants
from app.services import telemetry

# Opting in writes the unique telemetry `system_settings` rows inside the test's open
# transaction; every module that does so shares one xdist group (see test_admin_telemetry.py).
pytestmark = pytest.mark.xdist_group("telemetry_setting")

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCS_PAGE = REPO_ROOT / "docs-site" / "docs" / "operations" / "telemetry.md"

_OFF_SWITCHES = ("DO_NOT_TRACK", "OPENTRANSCRIBE_TELEMETRY", "OPENTRANSCRIBE_DEMO_MODE")


@pytest.fixture
def allowed(monkeypatch):
    """A deployment where nothing forces the report off and a receiver key is configured."""
    for name in _OFF_SWITCHES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENTRANSCRIBE_TELEMETRY_PROJECT_KEY", "phc_test_key")
    monkeypatch.setenv("OPENTRANSCRIBE_TELEMETRY_ENDPOINT", "https://receiver.invalid/i/v0/e/")


@pytest.fixture
def posts(monkeypatch):
    """Every request httpx would send, answered 200 without touching the network."""
    sent: list[httpx.Request] = []

    def fake_send(self, request, **_kwargs):
        sent.append(request)
        return httpx.Response(200, json={"status": 1}, request=request)

    def no_socket(*_args, **_kwargs):
        raise AssertionError("a real connection was attempted")

    monkeypatch.setattr(httpx.Client, "send", fake_send)
    monkeypatch.setattr(socket, "create_connection", no_socket)
    return sent


def _media_file(db, user, uploaded_at):
    from app.models.media import MediaFile

    row = MediaFile(
        filename="x.wav",
        storage_path=f"t/{uuid.uuid4()}.wav",
        file_size=1,
        content_type="audio/wav",
        user_id=user.id,
        upload_time=uploaded_at,
    )
    db.add(row)
    db.flush()
    return row


# --- the payload ---------------------------------------------------------------------------


def test_the_payload_has_exactly_the_four_documented_fields(db_session):
    payload = telemetry.build_payload(db_session, "11111111-1111-4111-8111-111111111111")

    assert tuple(payload) == telemetry.PAYLOAD_FIELDS
    assert telemetry.PAYLOAD_FIELDS == (
        "schema_version",
        "install_id",
        "app_version",
        "files_last_7d_bucket",
    )
    assert payload["schema_version"] == constants.TELEMETRY_SCHEMA_VERSION == 1


def test_the_request_is_the_payload_plus_the_three_privacy_directives(db_session, allowed):
    install_id = "11111111-1111-4111-8111-111111111111"
    body = telemetry.build_request(db_session, install_id)

    assert set(body) == {"api_key", "event", "distinct_id", "properties"}
    assert body["distinct_id"] == install_id
    assert body["event"] == constants.TELEMETRY_EVENT_NAME
    props = body["properties"]
    assert {k: v for k, v in props.items() if k.startswith("$")} == {
        "$ip": None,
        "$geoip_disable": True,
        "$process_person_profile": False,
    }
    assert tuple(k for k in props if not k.startswith("$")) == telemetry.PAYLOAD_FIELDS


def test_the_request_carries_nothing_about_the_host(db_session, allowed):
    text = json.dumps(telemetry.build_request(db_session, str(uuid.uuid4())))

    facts = {socket.gethostname(), socket.getfqdn()} - {"localhost", ""}
    leaked = [fact for fact in facts if fact in text]

    assert facts, "no host name to look for: this check would pass on anything"
    assert leaked == []


@pytest.mark.parametrize(
    ("count", "bucket"),
    [(0, "0"), (1, "1-10"), (10, "1-10"), (11, "11-100"), (100, "11-100"), (101, "100+")],
)
def test_files_are_bucketed(count, bucket):
    assert telemetry.files_bucket(count) == bucket


def test_only_files_from_the_last_seven_days_count(db_session, normal_user):
    now = datetime.now(UTC)
    _media_file(db_session, normal_user, now - timedelta(days=8))
    before = telemetry.build_payload(db_session, "x", now)["files_last_7d_bucket"]
    _media_file(db_session, normal_user, now - timedelta(hours=1))
    after = telemetry.build_payload(db_session, "x", now)["files_last_7d_bucket"]

    assert before == "0"
    assert after == "1-10"


@pytest.mark.parametrize(
    ("version", "reported"),
    [
        ("v0.7.0", "v0.7.0"),
        ("0.7.0-rc1", "0.7.0-rc1"),
        ("unknown", "other"),
        ("v0.7.0-acme-corp-internal-build", "other"),
        ("build for host-db01.example.com", "other"),
    ],
)
def test_a_custom_version_string_is_reported_as_other(version, reported):
    assert telemetry.reported_version(version) == reported


# --- opting in and out ---------------------------------------------------------------------


def test_it_is_off_on_a_fresh_install(db_session):
    state = telemetry.read_state(db_session)

    assert state["enabled"] is False
    assert state["install_id"] is None


def test_turning_it_on_creates_a_random_id_and_off_deletes_it(db_session, allowed):
    telemetry.set_enabled(db_session, True)
    first = telemetry.read_state(db_session)
    telemetry.set_enabled(db_session, False)
    cleared = telemetry.read_state(db_session)
    telemetry.set_enabled(db_session, True)
    second = telemetry.read_state(db_session)

    assert first["enabled"] is True
    assert uuid.UUID(first["install_id"]).version == 4
    assert cleared["enabled"] is False
    assert cleared["install_id"] is None
    assert second["install_id"] != first["install_id"]


@pytest.mark.parametrize(
    ("env", "reason"),
    [
        ({"DO_NOT_TRACK": "1"}, "do_not_track"),
        ({"DO_NOT_TRACK": "please"}, "do_not_track"),
        ({"OPENTRANSCRIBE_TELEMETRY": "false"}, "env_disabled"),
        ({"OPENTRANSCRIBE_TELEMETRY": "nope"}, "env_disabled"),
        ({"OPENTRANSCRIBE_DEMO_MODE": "true"}, "demo"),
    ],
)
def test_turning_it_on_is_refused_while_a_switch_forces_it_off(
    db_session, allowed, monkeypatch, env, reason
):
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(telemetry.TelemetryRefusedError) as refused:
        telemetry.set_enabled(db_session, True)

    assert refused.value.reasons == [reason]
    assert telemetry.read_state(db_session)["enabled"] is False


def test_turning_it_on_is_refused_without_a_receiver_key(db_session, allowed, monkeypatch):
    monkeypatch.setenv("OPENTRANSCRIBE_TELEMETRY_PROJECT_KEY", "  ")

    with pytest.raises(telemetry.TelemetryRefusedError) as refused:
        telemetry.set_enabled(db_session, True)

    assert refused.value.reasons == ["no_receiver"]


@pytest.mark.parametrize(
    ("value", "blocks"),
    [
        (None, False),  # unset
        ("", False),  # empty reads as unset
        ("0", False),  # the documented way to allow the report
        ("false", False),
        ("1", True),  # the shipped default
        ("true", True),
        ("please", True),  # unrecognised: fails closed
        ("O", True),  # a typo of 0 is not a 0
    ],
)
def test_do_not_track_values(allowed, monkeypatch, value, blocks):
    if value is None:
        monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    else:
        monkeypatch.setenv("DO_NOT_TRACK", value)

    assert ("do_not_track" in telemetry.off_reasons()) is blocks


def test_the_shipped_default_keeps_it_off(allowed, monkeypatch):
    """privacy_env's own default value, not a value this test picked."""
    from app.core.privacy_env import TELEMETRY_OFF_DEFAULTS

    monkeypatch.setenv("DO_NOT_TRACK", TELEMETRY_OFF_DEFAULTS["DO_NOT_TRACK"])

    assert telemetry.off_reasons() == ["do_not_track"]


@pytest.mark.parametrize(
    ("name", "value"),
    [("DO_NOT_TRACK", "0"), ("DO_NOT_TRACK", "false"), ("OPENTRANSCRIBE_TELEMETRY", "true")],
)
def test_an_explicit_yes_or_no_in_the_permissive_direction_does_not_block(
    allowed, monkeypatch, name, value
):
    monkeypatch.setenv(name, value)

    assert telemetry.off_reasons() == []


# --- sending -------------------------------------------------------------------------------


def test_it_sends_once_then_waits_a_day(db_session, allowed, posts):
    telemetry.set_enabled(db_session, True)
    now = datetime.now(UTC)

    first = telemetry.send_if_due(db_session, now)
    again = telemetry.send_if_due(db_session, now + timedelta(hours=1))
    next_day = telemetry.send_if_due(db_session, now + timedelta(hours=25))

    assert (first, again, next_day) == ("sent", "not_due", "sent")
    assert len(posts) == 2
    assert str(posts[0].url) == "https://receiver.invalid/i/v0/e/"
    sent_body = json.loads(posts[0].content)
    assert sent_body == telemetry.build_request(
        db_session, telemetry.read_state(db_session)["install_id"], now
    )
    assert telemetry.read_state(db_session)["last_sent_at"] == now + timedelta(hours=25)


def test_an_unreachable_receiver_is_not_retried_until_the_next_day(
    db_session, allowed, monkeypatch
):
    calls = []

    def unreachable(self, request, **_kwargs):
        calls.append(request)
        raise httpx.ConnectError("unreachable", request=request)

    monkeypatch.setattr(httpx.Client, "send", unreachable)
    telemetry.set_enabled(db_session, True)
    now = datetime.now(UTC)

    outcomes = [telemetry.send_if_due(db_session, now + timedelta(hours=h)) for h in (0, 1, 2)]

    assert outcomes == ["failed", "not_due", "not_due"]
    assert len(calls) == 1
    assert telemetry.read_state(db_session)["last_sent_at"] is None


def test_a_rejected_report_is_not_recorded_as_sent(db_session, allowed, monkeypatch):
    monkeypatch.setattr(
        httpx.Client,
        "send",
        lambda self, request, **_kw: httpx.Response(401, request=request),
    )
    telemetry.set_enabled(db_session, True)

    assert telemetry.send_if_due(db_session) == "failed"
    assert telemetry.read_state(db_session)["last_sent_at"] is None


def test_the_daily_offset_stays_within_an_hour_either_side():
    offsets = {telemetry.jitter_seconds(str(uuid.uuid4())) for _ in range(500)}

    assert min(offsets) >= -constants.TELEMETRY_JITTER_SECONDS
    assert max(offsets) <= constants.TELEMETRY_JITTER_SECONDS
    assert len(offsets) > 400, "the offset must spread installs, not pin them"


def test_the_request_timeout_is_short():
    assert constants.TELEMETRY_TIMEOUT_SECONDS <= 5


def test_the_task_reports_instead_of_raising_on_a_database_error(monkeypatch):
    from sqlalchemy.exc import OperationalError

    from app.tasks import telemetry as task_module

    def broken(_db):
        raise OperationalError("SELECT 1", {}, Exception("db down"))

    monkeypatch.setattr(telemetry, "send_if_due", broken)

    assert task_module.usage_report_task.run() == {"status": "error"}


# --- wiring --------------------------------------------------------------------------------


def test_the_report_is_scheduled_routed_and_classified():
    from app.core.celery import celery_app
    from app.core.celery_beat import BEAT_ENTRY_EFFECT
    from app.core.celery_beat import BEAT_SCHEDULE
    from app.core.celery_beat import BeatEffect
    from app.core.task_replay import is_replayable

    celery_app.loader.import_default_modules()
    assert "app.tasks.telemetry" in celery_app.conf.include
    assert "telemetry.usage_report" in celery_app.tasks
    assert BEAT_SCHEDULE["usage-report"]["task"] == "telemetry.usage_report"
    assert BEAT_SCHEDULE["usage-report"]["options"]["queue"] == "utility"
    assert celery_app.conf.task_routes["telemetry.usage_report"] == {"queue": "utility"}
    assert BEAT_ENTRY_EFFECT["usage-report"] is BeatEffect.MUTATING
    assert not is_replayable("telemetry.usage_report")


# --- the docs page is the contract ---------------------------------------------------------


def test_the_docs_page_lists_exactly_the_fields_that_are_sent():
    """Adding or removing a field without updating the public page fails here (plan section 4.4)."""
    text = DOCS_PAGE.read_text(encoding="utf-8")
    section = text.split("## Exactly what is sent", 1)[1].split("\n## ", 1)[0]
    documented = tuple(re.findall(r"^\| `([a-z0-9_]+)` \|", section, flags=re.MULTILINE))

    assert documented == telemetry.PAYLOAD_FIELDS
    assert f"schema_version` | `{constants.TELEMETRY_SCHEMA_VERSION}`" in section
