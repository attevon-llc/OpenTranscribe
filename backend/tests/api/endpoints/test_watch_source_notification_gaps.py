"""Three ways a watch-source email link can look configured and deliver nothing.

Each of these is silent today, which is what makes them expensive: an admin who
subscribes a source and never receives mail has no signal anywhere — not in the UI,
not in the logs, not in the API.

* ``additional_recipients`` was free-text CSV with no validation. ``_merge_recipients``
  splits on commas and strips; a typo'd address is dropped without a word, so the link
  reads as configured and simply never delivers to that person.
* ``send_notification`` skips a disabled config, and skips a link whose merged
  recipient list is empty, with **no log line** in either case.
* Deleting an email config cascades (``EmailNotificationConfig.links`` is
  ``all, delete-orphan``), silently unlinking every source that used it — and the API
  gave the caller no way to know how many that was before deciding.

**No mail is sent and no network is touched**: ``send_email`` is replaced at the
service boundary, and every config row here is created with no secret at all.
"""

from __future__ import annotations

import logging
import uuid as uuid_pkg
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from fastapi import status
from pydantic import ValidationError

from app.models.email_notification_config import EmailNotificationConfig
from app.models.email_notification_config import WatchSourceEmail
from app.models.watch_source import WatchSource
from app.schemas.watch_source import EmailLinkCreate
from app.tasks import watch_source_tasks

BASE = "/api/watch-sources"


def _make_source(db_session, owner) -> WatchSource:
    source = WatchSource(
        uuid=uuid_pkg.uuid4(),
        user_id=owner.id,
        created_by=owner.id,
        name=f"watch-{uuid_pkg.uuid4().hex[:8]}",
        source_type="local",
        local_path=f"pytest/{uuid_pkg.uuid4().hex[:8]}",
    )
    db_session.add(source)
    db_session.commit()
    db_session.refresh(source)
    return source


def _make_email_config(db_session, **overrides) -> EmailNotificationConfig:
    """A credential-free mailer row — no password is set, so none can leak."""
    defaults = {
        "uuid": uuid_pkg.uuid4(),
        "name": f"mailer-{uuid_pkg.uuid4().hex[:8]}",
        "provider": "smtp",
        "smtp_host": "smtp.invalid.example.com",
        "smtp_port": 587,
        "from_address": "noreply@example.com",
    }
    defaults.update(overrides)
    config = EmailNotificationConfig(**defaults)
    db_session.add(config)
    db_session.commit()
    db_session.refresh(config)
    return config


def _link(db_session, source, config, **overrides) -> WatchSourceEmail:
    defaults = {
        "watch_source_id": source.id,
        "email_config_id": config.id,
        "notify_on_success": True,
        "notify_on_error": True,
    }
    defaults.update(overrides)
    link = WatchSourceEmail(**defaults)
    db_session.add(link)
    db_session.commit()
    return link


# ---------------------------------------------------------------------------
# W4 — additional_recipients validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "not-an-address",
        "ops@example.com, alsobad",
        "@example.com",
        "ops@",
    ],
)
def test_additional_recipients_rejects_an_unusable_address(value):
    """A malformed entry must be refused at the boundary, not dropped at send time.

    ``_merge_recipients`` would strip these into the recipient list and hand them to
    the mailer, where they fail per-address inside a send this code never inspects —
    so the admin sees a link that claims to notify someone it never reaches.
    """
    with pytest.raises(ValidationError):
        EmailLinkCreate(email_config_uuid=str(uuid_pkg.uuid4()), additional_recipients=value)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "ops@example.com",
        "ops@example.com,oncall@example.com",
        " ops@example.com , oncall@example.com ",
    ],
)
def test_additional_recipients_accepts_valid_and_empty_forms(value):
    """The negative control: validation must not break the shapes already in use.

    Blank and ``None`` both mean "no extras" and are the common case; the CSV is
    stored verbatim and split on send, so surrounding whitespace has to stay legal.
    """
    payload = EmailLinkCreate(email_config_uuid=str(uuid_pkg.uuid4()), additional_recipients=value)
    assert payload.additional_recipients == value


# ---------------------------------------------------------------------------
# W5 — a dropped notification leaves a trace
# ---------------------------------------------------------------------------


@pytest.fixture
def _scoped_session(db_session, monkeypatch):
    """Point the task's own ``session_scope`` at the test session.

    Patched on ``watch_source_tasks``, not ``app.db.session_utils``: the module did
    ``from … import session_scope`` at import time, so patching the source module would
    rebind a name nothing reads. Without this the task opens a second connection that
    cannot see the fixture's uncommitted rows and blocks on their locks.
    """

    @contextmanager
    def _scope():
        yield db_session
        db_session.commit()

    monkeypatch.setattr(watch_source_tasks, "session_scope", _scope)


@pytest.fixture
def _no_mail():
    """Replace the mailer at the service boundary — no SMTP/Graph session is opened.

    ``send_notification`` imports ``send_email`` inside the function body, so the name
    is resolved on the module at call time and patching it there is what takes effect.
    """
    with patch("app.services.watch_email_service.send_email") as send:
        send.return_value = (True, "ok")
        yield send


def test_a_disabled_config_logs_why_it_was_skipped(
    db_session, normal_user, caplog, _scoped_session, _no_mail
):
    """A disabled mailer silently swallowed the notification.

    The link is present and both flags are on, so every surface says "subscribed" —
    the only thing standing between the source and the mail is a boolean on a
    deployment-wide config the source owner may not even be able to see.
    """
    source = _make_source(db_session, normal_user)
    config = _make_email_config(db_session, is_enabled=False, default_recipients="ops@example.com")
    _link(db_session, source, config)

    with caplog.at_level(logging.WARNING):
        watch_source_tasks.send_notification(source.id, {"errors": 0, "imported": 1})

    assert any(
        record.levelno >= logging.WARNING and config.name in record.getMessage()
        for record in caplog.records
    ), "a skipped delivery must name the config that was skipped"
    _no_mail.assert_not_called()


def test_a_link_with_no_recipients_anywhere_logs_why_it_was_skipped(
    db_session, normal_user, caplog, _scoped_session, _no_mail
):
    """Config has no default recipients and the link adds none — nothing is sent.

    This is the failure mode the UI cannot infer on its own: both the config and the
    link look complete in isolation, and only their *combination* is empty.
    """
    source = _make_source(db_session, normal_user)
    config = _make_email_config(db_session, default_recipients=None)
    _link(db_session, source, config, additional_recipients=None)

    with caplog.at_level(logging.WARNING):
        watch_source_tasks.send_notification(source.id, {"errors": 1, "imported": 0})

    assert any(
        record.levelno >= logging.WARNING and config.name in record.getMessage()
        for record in caplog.records
    ), "an empty recipient list must be reported, not silently skipped"
    _no_mail.assert_not_called()


def test_a_deliverable_link_still_sends_and_logs_no_warning(
    db_session, normal_user, caplog, _scoped_session, _no_mail
):
    """The negative control: a correctly configured link must stay quiet and deliver.

    Without this, a change that logged a warning unconditionally would satisfy both
    tests above while making the new log line worthless.
    """
    source = _make_source(db_session, normal_user)
    config = _make_email_config(db_session, default_recipients="ops@example.com")
    _link(db_session, source, config)

    with caplog.at_level(logging.WARNING):
        result = watch_source_tasks.send_notification(source.id, {"errors": 0, "imported": 2})

    assert result["sent"] == 1
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
        "a healthy delivery must not warn"
    )


# ---------------------------------------------------------------------------
# W6 — the blast radius of deleting a config is visible before you delete it
# ---------------------------------------------------------------------------


def test_email_config_list_reports_how_many_sources_use_each_config(
    client, db_session, normal_user, super_admin_token_headers
):
    """Deleting a config cascades its links away — the caller must see the count first.

    ``EmailNotificationConfig.links`` is ``cascade="all, delete-orphan"``, so removing
    a config stops notifications for every source linked to it, with no warning and no
    way to put them back. The count is the only thing that makes that consequence
    visible at the moment of the decision.
    """
    config = _make_email_config(db_session)
    unused = _make_email_config(db_session)
    for _ in range(2):
        _link(db_session, _make_source(db_session, normal_user), config)

    response = client.get(f"{BASE}/email-configs", headers=super_admin_token_headers)

    assert response.status_code == status.HTTP_200_OK
    by_uuid = {c["uuid"]: c for c in response.json()["configs"]}
    assert by_uuid[str(config.uuid)]["linked_source_count"] == 2
    assert by_uuid[str(unused.uuid)]["linked_source_count"] == 0
