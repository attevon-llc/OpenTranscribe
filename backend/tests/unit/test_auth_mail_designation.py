"""A broken auth-mail designation must be impossible to save.

``load_auth_mail_config`` (pinned by ``test_auth_email_delivery.py``) degrades to
env SMTP whenever the designated ``EmailNotificationConfig`` is missing or
disabled, and says so only in an ERROR log. That is the right *read* posture and
the wrong thing to discover later: with ``SMTP_HOST`` unset — the default in all
23 compose files — the degradation means password resets simply stop, while the
admin panel keeps showing a designation that looks fine.

So the write side is where it has to be caught. These tests pin the four rules
that keep a designation trustworthy:

1. a UUID that names no config, or a disabled one, is rejected outright;
2. clearing is legitimate and means "use env SMTP";
3. the response says whether the designation still *resolves*, not just what it
   is, so a row deleted later shows up as dangling; and
4. deleting or disabling the designated config is refused rather than silently
   taking auth mail down.

Every change is audited through ``ADMIN_SETTINGS_CHANGE``, the same
OpenSearch-backed trail the rest of the admin surface writes to.
"""

from __future__ import annotations

from typing import Any
from typing import cast

import pytest
from fastapi import HTTPException

from app.core.constants import AUTH_EMAIL_CONFIG_SETTING_KEY
from app.models.email_notification_config import EmailNotificationConfig
from app.services import auth_mail_config_service as mod

CONFIG_UUID = "019ec90a-1b2c-7def-8000-00000000ee01"
OTHER_UUID = "019ec90a-1b2c-7def-8000-00000000ee02"
GONE_UUID = "019ec90a-1b2c-7def-8000-0000000000ff"


class FakeConfig:
    """Stands in for an ``EmailNotificationConfig`` row."""

    def __init__(self, *, uuid: str = CONFIG_UUID, enabled: bool = True, name: str = "Corp M365"):
        self.uuid = uuid
        self.name = name
        self.provider = "m365"
        self.is_enabled = enabled

    def __getattr__(self, name: str):
        """Every other column exists but is empty on this stand-in.

        The update route serialises the whole row on the way out; spelling out
        twenty unset provider columns would say nothing the test is about.
        """
        return None


class FakeQuery:
    """Resolves ``filter(EmailNotificationConfig.uuid == x).first()`` in memory.

    The criterion is a real SQLAlchemy expression, so the bound value is read off
    it rather than re-implementing the comparison.
    """

    def __init__(self, rows: list[FakeConfig]) -> None:
        self._rows = rows
        self._wanted: str | None = None

    def filter(self, criterion):
        self._wanted = str(criterion.right.value)
        return self

    def first(self):
        return next((row for row in self._rows if str(row.uuid) == self._wanted), None)


class FakeSession:
    """Enough of a ``Session`` for the config lookup, with no database."""

    def __init__(self, rows: list[FakeConfig] | None = None) -> None:
        self.rows = rows if rows is not None else []
        self.deleted: list[FakeConfig] = []
        self.commits = 0

    def query(self, model):
        assert model is EmailNotificationConfig
        return FakeQuery(self.rows)

    def delete(self, row):
        self.deleted.append(row)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def refresh(self, _row):
        pass


@pytest.fixture
def store(monkeypatch):
    """In-memory stand-in for the ``SystemSettings`` key/value table."""
    values: dict[str, str] = {}

    def fake_get(_db, key, default=None):
        return values.get(key, default)

    def fake_set(_db, key, value, description=None):
        values[key] = value
        return None

    monkeypatch.setattr(mod, "get_setting", fake_get)
    monkeypatch.setattr(mod, "set_setting", fake_set)
    return values


@pytest.fixture
def no_env_smtp(monkeypatch):
    """The out-of-the-box state: nothing to fall back to."""
    monkeypatch.setattr(mod.settings, "SMTP_HOST", "")
    return mod.settings


def _db(*configs: FakeConfig) -> Any:
    """A structural stand-in for ``Session``; casting states that honestly."""
    return cast(Any, FakeSession(list(configs)))


class TestWriteTimeValidation:
    """A designation that cannot deliver is refused before it is stored."""

    def test_unknown_uuid_is_rejected(self, store, no_env_smtp):
        with pytest.raises(ValueError) as exc:
            mod.set_designation(_db(FakeConfig()), GONE_UUID)

        assert GONE_UUID in str(exc.value)
        assert AUTH_EMAIL_CONFIG_SETTING_KEY not in store

    def test_disabled_config_is_rejected(self, store, no_env_smtp):
        disabled = FakeConfig(enabled=False, name="Retired relay")

        with pytest.raises(ValueError) as exc:
            mod.set_designation(_db(disabled), CONFIG_UUID)

        assert "Retired relay" in str(exc.value)
        assert AUTH_EMAIL_CONFIG_SETTING_KEY not in store

    def test_malformed_uuid_is_rejected_without_a_query(self, store, no_env_smtp):
        with pytest.raises(ValueError) as exc:
            mod.set_designation(_db(FakeConfig()), "not-a-uuid")

        assert "not a valid" in str(exc.value)
        assert AUTH_EMAIL_CONFIG_SETTING_KEY not in store

    def test_an_enabled_config_is_stored_normalised(self, store, no_env_smtp):
        result = mod.set_designation(_db(FakeConfig()), f"  {CONFIG_UUID.upper()}  ")

        assert store[AUTH_EMAIL_CONFIG_SETTING_KEY] == CONFIG_UUID
        assert result.status == mod.STATUS_ACTIVE
        assert result.resolves is True

    def test_a_rejected_write_leaves_the_previous_designation_intact(self, store, no_env_smtp):
        db = _db(FakeConfig(), FakeConfig(uuid=OTHER_UUID, name="Other"))
        mod.set_designation(db, CONFIG_UUID)

        with pytest.raises(ValueError):
            mod.set_designation(db, GONE_UUID)

        assert store[AUTH_EMAIL_CONFIG_SETTING_KEY] == CONFIG_UUID


class TestClearing:
    """ "Use env SMTP" is a choice, not a failure state."""

    @pytest.mark.parametrize("cleared", ["", "   ", None])
    def test_clearing_is_accepted(self, store, no_env_smtp, cleared):
        db = _db(FakeConfig())
        mod.set_designation(db, CONFIG_UUID)

        result = mod.set_designation(db, cleared)

        assert store[AUTH_EMAIL_CONFIG_SETTING_KEY] == ""
        assert result.config_uuid is None
        assert result.status == mod.STATUS_NOT_DESIGNATED

    def test_clearing_reports_whether_env_smtp_can_take_over(self, store, monkeypatch):
        monkeypatch.setattr(mod.settings, "SMTP_HOST", "mail.example.org")

        assert mod.set_designation(_db(), "").env_smtp_configured is True


class TestDescribeDesignation:
    """The UI must be able to tell 'designated' from 'designated and working'."""

    def test_nothing_designated(self, store, no_env_smtp):
        status = mod.describe_designation(_db(FakeConfig()))

        assert status.status == mod.STATUS_NOT_DESIGNATED
        assert status.config_uuid is None
        assert status.resolves is False
        assert status.env_smtp_configured is False

    def test_active_designation_carries_the_provider_details(self, store, no_env_smtp):
        db = _db(FakeConfig())
        mod.set_designation(db, CONFIG_UUID)

        status = mod.describe_designation(db)

        assert (status.config_name, status.provider) == ("Corp M365", "m365")
        assert status.resolves is True

    def test_a_deleted_config_leaves_a_dangling_designation(self, store, no_env_smtp):
        db = _db(FakeConfig())
        mod.set_designation(db, CONFIG_UUID)
        db.rows.clear()

        status = mod.describe_designation(db)

        assert status.status == mod.STATUS_MISSING
        assert status.config_uuid == CONFIG_UUID
        assert status.resolves is False

    def test_a_disabled_config_is_reported_as_not_resolving(self, store, no_env_smtp):
        config = FakeConfig()
        db = _db(config)
        mod.set_designation(db, CONFIG_UUID)
        config.is_enabled = False

        status = mod.describe_designation(db)

        assert status.status == mod.STATUS_DISABLED
        assert status.is_enabled is False
        assert status.resolves is False


class TestIsDesignated:
    """The guard the CRUD routes consult before deleting or disabling a row."""

    def test_matches_the_designated_row(self, store, no_env_smtp):
        db = _db(FakeConfig())
        mod.set_designation(db, CONFIG_UUID)

        assert mod.is_designated(db, CONFIG_UUID) is True
        assert mod.is_designated(db, OTHER_UUID) is False

    def test_comparison_is_by_uuid_not_by_string(self, store, no_env_smtp):
        store[AUTH_EMAIL_CONFIG_SETTING_KEY] = CONFIG_UUID.upper()

        assert mod.is_designated(_db(), CONFIG_UUID) is True

    def test_nothing_is_designated_when_the_key_is_cleared(self, store, no_env_smtp):
        store[AUTH_EMAIL_CONFIG_SETTING_KEY] = ""

        assert mod.is_designated(_db(), CONFIG_UUID) is False

    def test_a_corrupt_stored_value_matches_nothing(self, store, no_env_smtp):
        store[AUTH_EMAIL_CONFIG_SETTING_KEY] = "garbage"

        assert mod.is_designated(_db(), CONFIG_UUID) is False


class FakeRequest:
    """The two attributes ``_get_client_info`` reads."""

    def __init__(self) -> None:
        self.client = type("Client", (), {"host": "203.0.113.5"})()
        self.headers: dict[str, str] = {"User-Agent": "pytest"}


class FakeUser:
    id = 7
    email = "root@example.com"


@pytest.fixture
def audited(monkeypatch):
    """Capture what reaches the audit trail instead of indexing it."""
    from app.api.endpoints import auth_email_delivery

    calls: list[dict] = []
    monkeypatch.setattr(
        auth_email_delivery.audit_logger,
        "log_admin_action",
        lambda **kwargs: calls.append(kwargs),
    )
    return calls


class TestEndpointContract:
    """400 on a designation that cannot deliver; an audit record when it can."""

    @staticmethod
    def _put(db, value):
        from app.api.endpoints.auth_email_delivery import update_auth_mail_designation
        from app.schemas.email_notification import AuthMailDesignationUpdate

        return update_auth_mail_designation(
            cast(Any, FakeRequest()),
            AuthMailDesignationUpdate(config_uuid=value),
            db,
            cast(Any, FakeUser()),
        )

    def test_a_valid_designation_is_audited(self, store, no_env_smtp, audited):
        response = self._put(_db(FakeConfig()), CONFIG_UUID)

        assert response.status == mod.STATUS_ACTIVE
        assert len(audited) == 1
        details = audited[0]["details"]
        assert details["setting"] == AUTH_EMAIL_CONFIG_SETTING_KEY
        assert (details["old_value"], details["new_value"]) == (None, CONFIG_UUID)
        assert audited[0]["admin_username"] == "root@example.com"

    def test_the_audit_event_is_the_settings_change_type(self, store, no_env_smtp, audited):
        from app.auth.audit import AuditEventType

        self._put(_db(FakeConfig()), CONFIG_UUID)

        assert audited[0]["event_type"] == AuditEventType.ADMIN_SETTINGS_CHANGE

    def test_clearing_records_what_it_replaced(self, store, no_env_smtp, audited):
        db = _db(FakeConfig())
        self._put(db, CONFIG_UUID)

        self._put(db, "")

        assert audited[-1]["details"]["old_value"] == CONFIG_UUID
        assert audited[-1]["details"]["new_value"] is None

    def test_an_unknown_uuid_becomes_a_400_and_is_not_audited(self, store, no_env_smtp, audited):
        with pytest.raises(HTTPException) as exc:
            self._put(_db(FakeConfig()), GONE_UUID)

        assert exc.value.status_code == 400
        assert GONE_UUID in str(exc.value.detail)
        assert audited == []

    def test_a_disabled_config_becomes_a_400(self, store, no_env_smtp, audited):
        with pytest.raises(HTTPException) as exc:
            self._put(_db(FakeConfig(enabled=False)), CONFIG_UUID)

        assert exc.value.status_code == 400
        assert audited == []

    def test_the_read_route_reports_a_dangling_designation(self, store, no_env_smtp):
        from app.api.endpoints.auth_email_delivery import get_auth_mail_designation

        db = _db(FakeConfig())
        mod.set_designation(db, CONFIG_UUID)
        db.rows.clear()

        response = get_auth_mail_designation(db, cast(Any, FakeUser()))

        assert response.status == mod.STATUS_MISSING
        assert response.resolves is False


class TestDesignatedConfigIsProtected:
    """Deleting or disabling the auth mailer stops password resets outright."""

    @staticmethod
    def _delete(db, uuid):
        from app.api.endpoints.watch_sources import delete_email_config

        return delete_email_config(uuid, db, cast(Any, FakeUser()))

    @staticmethod
    def _disable(db, uuid, enabled=False):
        from app.api.endpoints.watch_sources import update_email_config
        from app.schemas.email_notification import EmailConfigUpdate

        return update_email_config(
            uuid, EmailConfigUpdate(is_enabled=enabled), db, cast(Any, FakeUser())
        )

    def test_deleting_the_designated_config_is_refused(self, store, no_env_smtp):
        db = _db(FakeConfig())
        mod.set_designation(db, CONFIG_UUID)

        with pytest.raises(HTTPException) as exc:
            self._delete(db, CONFIG_UUID)

        assert exc.value.status_code == 409
        assert db.deleted == []

    def test_an_undesignated_config_still_deletes(self, store, no_env_smtp):
        db = _db(FakeConfig(), FakeConfig(uuid=OTHER_UUID, name="Other"))
        mod.set_designation(db, CONFIG_UUID)

        self._delete(db, OTHER_UUID)

        assert [c.uuid for c in db.deleted] == [OTHER_UUID]

    def test_disabling_the_designated_config_is_refused(self, store, no_env_smtp):
        db = _db(FakeConfig())
        mod.set_designation(db, CONFIG_UUID)

        with pytest.raises(HTTPException) as exc:
            self._disable(db, CONFIG_UUID)

        assert exc.value.status_code == 409
        assert db.rows[0].is_enabled is True

    def test_other_edits_to_the_designated_config_are_allowed(self, store, no_env_smtp):
        """Only the enable flag is guarded — renaming or re-keying it is normal."""
        db = _db(FakeConfig())
        mod.set_designation(db, CONFIG_UUID)

        self._disable(db, CONFIG_UUID, enabled=True)

        assert db.commits >= 1
