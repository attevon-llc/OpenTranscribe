"""The opt-in anonymous usage report (issue #1019, owner decision D14).

It answers one question: does this install exist, which version is it, and is it in use. It
never says what the install is used for. The report has exactly the four fields
:func:`build_payload` returns, documented field by field in
``docs-site/docs/operations/telemetry.md`` (a test keeps the two equal):

* ``schema_version`` - :data:`~app.core.constants.TELEMETRY_SCHEMA_VERSION`;
* ``install_id`` - a random UUID4 created when an administrator turns the report on and
  deleted when it is turned off. Never derived from the host, the network or an account;
* ``app_version`` - the release this server runs, or ``"other"`` for a custom build string;
* ``files_last_7d_bucket`` - media files added in the last seven days, as ``0``, ``1-10``,
  ``11-100`` or ``100+``.

Nothing is sent unless ALL of these hold: an administrator turned it on, ``DO_NOT_TRACK`` is
not set, ``OPENTRANSCRIBE_TELEMETRY`` is not set to a "no", ``OPENTRANSCRIBE_DEMO_MODE`` is not
set, and a receiver project key is configured. The images and compose ship ``DO_NOT_TRACK=1``
(``app/core/privacy_env.py``), so the report is a DOUBLE opt-in: an operator sets
``DO_NOT_TRACK=0`` in ``.env`` and restarts, then a super admin turns it on. Those conditions are :func:`off_reasons`, and
the same list refuses turning it on (:func:`set_enabled`).

The admin preview and the real send both come from :func:`build_request`, so the preview is
the request, not a description of it. The request follows the receiver's single-event capture
API (https://posthog.com/docs/api/capture): ``api_key``, ``event``, ``distinct_id`` and
``properties``. Three processing directives ride in ``properties`` and are not data:
``$ip: null`` and ``$geoip_disable: true`` stop the receiver keeping an address or deriving a
location from the connection (https://posthog.com/docs/privacy/data-collection), and
``$process_person_profile: false`` marks the event anonymous, with no person profile (capture
docs above). The receiver project must also have client IP capture discarded (owner setup).
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Any

import httpx
from sqlalchemy import func
from sqlalchemy import select

from app.core import constants
from app.core.env import env_bool
from app.core.env import env_is_set
from app.core.env import env_str
from app.core.env import parse_bool
from app.core.version import APP_VERSION
from app.models.media import MediaFile
from app.models.system_settings import SystemSettings
from app.schemas.telemetry import TelemetryOffReason
from app.services.system_settings_service import get_settings_map
from app.services.system_settings_service import insert_setting_if_absent
from app.services.system_settings_service import set_setting

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: Why the report cannot be sent, in the order they are checked. Stable codes the UI translates.
REASON_DO_NOT_TRACK: TelemetryOffReason = "do_not_track"
REASON_ENV_DISABLED: TelemetryOffReason = "env_disabled"
REASON_DEMO: TelemetryOffReason = "demo"
REASON_NO_RECEIVER: TelemetryOffReason = "no_receiver"
#: Offline (air-gapped), PKI and FIPS deployments are the ones that must not call out, so the
#: report is forced off there regardless of the admin switch.
REASON_OFFLINE: TelemetryOffReason = "offline"
REASON_PKI: TelemetryOffReason = "pki"
REASON_FIPS: TelemetryOffReason = "fips"

#: The field list, in order. The docs page and its parity test read this.
PAYLOAD_FIELDS = ("schema_version", "install_id", "app_version", "files_last_7d_bucket")

#: Shown in the preview before an install id exists; a real one is created on opt-in.
PREVIEW_INSTALL_ID = "00000000-0000-0000-0000-000000000000"

#: A release version (``v0.7.0``, ``0.7.0-rc1``). Anything else is reported as ``"other"``,
#: so a custom build string can never carry something identifying out.
_RELEASE_VERSION = re.compile(r"^v?\d{1,4}\.\d{1,4}\.\d{1,4}(?:[-.][0-9A-Za-z]{1,16})?$")

_SETTING_KEYS = [
    constants.TELEMETRY_ENABLED_KEY,
    constants.TELEMETRY_INSTALL_ID_KEY,
    constants.TELEMETRY_LAST_SENT_AT_KEY,
    constants.TELEMETRY_LAST_ATTEMPT_AT_KEY,
]


class TelemetryRefusedError(Exception):
    """Turning the report on was refused; ``reasons`` are :func:`off_reasons` codes."""

    def __init__(self, reasons: list[TelemetryOffReason]) -> None:
        super().__init__(", ".join(reasons))
        self.reasons = reasons


# --- the receiver and the switches -------------------------------------------------------

#: The receiver's documented single-event capture URL in its EU region
#: (https://posthog.com/docs/api/capture).
DEFAULT_ENDPOINT = "https://eu.i.posthog.com/i/v0/e/"


def receiver_endpoint() -> str:
    """Where the report goes: ``OPENTRANSCRIBE_TELEMETRY_ENDPOINT``, else the EU default.

    Deployment configuration, never an admin-UI setting: an administrator decides WHETHER the
    report is sent, not WHERE, so the UI cannot point the payload at an arbitrary host.
    """
    return env_str("OPENTRANSCRIBE_TELEMETRY_ENDPOINT", DEFAULT_ENDPOINT)


def receiver_key() -> str:
    """The receiver's public write-only project key. None ships: without one nothing can send."""
    return env_str("OPENTRANSCRIBE_TELEMETRY_PROJECT_KEY")


def off_reasons() -> list[TelemetryOffReason]:
    """Every deployment-level reason the report cannot be sent, whatever the admin chose.

    Each switch fails closed on an unrecognised spelling: ``DO_NOT_TRACK`` and the demo flag
    count as set unless they read as an explicit no, ``OPENTRANSCRIBE_TELEMETRY`` counts as off
    unless it reads as an explicit yes. Read at call time, so no restart is needed.
    """
    reasons: list[TelemetryOffReason] = []
    # https://consoledonottrack.com/: any set value but an explicit no means "do not track".
    if env_is_set("DO_NOT_TRACK") and env_bool("DO_NOT_TRACK", True):
        reasons.append(REASON_DO_NOT_TRACK)
    if env_is_set("OPENTRANSCRIBE_TELEMETRY") and not env_bool("OPENTRANSCRIBE_TELEMETRY", False):
        reasons.append(REASON_ENV_DISABLED)
    if env_is_set("OPENTRANSCRIBE_DEMO_MODE") and env_bool("OPENTRANSCRIBE_DEMO_MODE", True):
        reasons.append(REASON_DEMO)
    # Set by docker-compose.offline.yml on every service; an explicit "no" or typo stays off.
    if env_is_set("HF_HUB_OFFLINE") and env_bool("HF_HUB_OFFLINE", True):
        reasons.append(REASON_OFFLINE)
    if env_bool("PKI_ENABLED", False):
        reasons.append(REASON_PKI)
    if env_bool("FIPS_MODE", False):
        reasons.append(REASON_FIPS)
    if not receiver_key():
        reasons.append(REASON_NO_RECEIVER)
    return reasons


def debug_enabled() -> bool:
    """``OPENTRANSCRIBE_TELEMETRY_DEBUG``: log the request that would be sent, send nothing."""
    return env_bool("OPENTRANSCRIBE_TELEMETRY_DEBUG", False)


# --- the payload -------------------------------------------------------------------------


def reported_version(version: str = APP_VERSION) -> str:
    """The version as reported: a release version, else ``"other"``."""
    value = (version or "").strip()
    return value if _RELEASE_VERSION.match(value) else "other"


def files_bucket(count: int) -> str:
    """A count of files as one of four coarse buckets."""
    if count <= 0:
        return "0"
    if count <= 10:
        return "1-10"
    if count <= 100:
        return "11-100"
    return "100+"


def _files_last_7d(db: Session, now: datetime) -> int:
    since = now - timedelta(days=7)
    count = db.execute(
        select(func.count(MediaFile.id)).where(MediaFile.upload_time >= since)
    ).scalar_one()
    return int(count or 0)


def build_payload(db: Session, install_id: str, now: datetime | None = None) -> dict[str, Any]:
    """The four reported fields, exactly. See the module docstring."""
    moment = now or datetime.now(UTC)
    return {
        "schema_version": constants.TELEMETRY_SCHEMA_VERSION,
        "install_id": install_id,
        "app_version": reported_version(),
        "files_last_7d_bucket": files_bucket(_files_last_7d(db, moment)),
    }


def build_request(db: Session, install_id: str, now: datetime | None = None) -> dict[str, Any]:
    """The request body that is sent, and that the admin preview shows."""
    return {
        "api_key": receiver_key(),
        "event": constants.TELEMETRY_EVENT_NAME,
        "distinct_id": install_id,
        "properties": {
            **build_payload(db, install_id, now),
            "$ip": None,
            "$geoip_disable": True,
            "$process_person_profile": False,
        },
    }


# --- stored state ------------------------------------------------------------------------


def _parse_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def read_state(db: Session) -> dict[str, Any]:
    """The stored switch, install id and send times."""
    stored = get_settings_map(db, _SETTING_KEYS)
    return {
        "enabled": parse_bool(
            stored.get(constants.TELEMETRY_ENABLED_KEY), constants.DEFAULT_TELEMETRY_ENABLED
        ),
        "install_id": stored.get(constants.TELEMETRY_INSTALL_ID_KEY) or None,
        "last_sent_at": _parse_time(stored.get(constants.TELEMETRY_LAST_SENT_AT_KEY)),
        "last_attempt_at": _parse_time(stored.get(constants.TELEMETRY_LAST_ATTEMPT_AT_KEY)),
    }


def _delete_keys(db: Session, keys: list[str]) -> None:
    from app.core import settings_cache

    db.query(SystemSettings).filter(SystemSettings.key.in_(keys)).delete(synchronize_session=False)
    db.commit()
    for key in keys:
        settings_cache.invalidate(key)


def _write_enabled(db: Session, enabled: bool) -> None:
    """Store the switch; the row is created race-free first, so ``set_setting`` only updates."""
    description = "Send the anonymous usage report"
    insert_setting_if_absent(db, constants.TELEMETRY_ENABLED_KEY, enabled, description)
    set_setting(db, constants.TELEMETRY_ENABLED_KEY, enabled, description)


def set_enabled(db: Session, enabled: bool) -> None:
    """Turn the report on (refused while :func:`off_reasons` has any) or off.

    On creates a fresh random install id when none exists. Off deletes the install id and the
    send times, so turning it back on later starts a new, unlinked id.
    """
    if enabled:
        reasons = off_reasons()
        if reasons:
            raise TelemetryRefusedError(reasons)
        # Create-if-absent rather than read-then-insert: two admins turning it on at once
        # both succeed and keep whichever id was written first.
        insert_setting_if_absent(db, constants.TELEMETRY_INSTALL_ID_KEY, str(uuid.uuid4()))
        _write_enabled(db, True)
        return
    _write_enabled(db, False)
    _delete_keys(
        db,
        [
            constants.TELEMETRY_INSTALL_ID_KEY,
            constants.TELEMETRY_LAST_SENT_AT_KEY,
            constants.TELEMETRY_LAST_ATTEMPT_AT_KEY,
        ],
    )


# --- sending -----------------------------------------------------------------------------


def jitter_seconds(install_id: str) -> int:
    """This install's fixed offset from the daily interval, within +/- the jitter window."""
    try:
        value = uuid.UUID(install_id).int
    except ValueError:
        value = 0
    span = 2 * constants.TELEMETRY_JITTER_SECONDS
    return int(value % (span + 1)) - constants.TELEMETRY_JITTER_SECONDS


def is_due(install_id: str, last_attempt_at: datetime | None, now: datetime) -> bool:
    """True when no attempt was made yet, or the last one is a day (plus jitter) old."""
    if last_attempt_at is None:
        return True
    interval = constants.TELEMETRY_INTERVAL_SECONDS + jitter_seconds(install_id)
    return (now - last_attempt_at).total_seconds() >= interval


def _post(body: dict[str, Any]) -> bool:
    """One POST, short timeout, no retry. True when the receiver accepted it."""
    try:
        with httpx.Client(timeout=constants.TELEMETRY_TIMEOUT_SECONDS) as client:
            response = client.post(receiver_endpoint(), json=body)
    except httpx.HTTPError as exc:
        logger.debug("Usage report not delivered: %s", type(exc).__name__)
        return False
    if response.is_success:
        return True
    logger.debug("Usage report not accepted: HTTP %s", response.status_code)
    return False


def send_if_due(db: Session, now: datetime | None = None) -> str:
    """Send the report when it is on, allowed and due. Returns what happened, as a word."""
    state = read_state(db)
    if not state["enabled"]:
        return "disabled"
    if off_reasons():
        return "blocked"
    install_id = state["install_id"]
    if not install_id:
        return "no_install_id"
    moment = now or datetime.now(UTC)
    if not is_due(install_id, state["last_attempt_at"], moment):
        return "not_due"
    body = build_request(db, install_id, moment)
    set_setting(db, constants.TELEMETRY_LAST_ATTEMPT_AT_KEY, moment.isoformat())
    if debug_enabled():
        logger.info("Usage report debug mode, NOT sent. Request body: %s", body)
        return "debug"
    if not _post(body):
        return "failed"
    set_setting(db, constants.TELEMETRY_LAST_SENT_AT_KEY, moment.isoformat())
    logger.info("Anonymous usage report sent")
    return "sent"
