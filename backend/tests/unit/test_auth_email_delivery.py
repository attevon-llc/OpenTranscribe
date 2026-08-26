"""Auth mail goes out over the deployment's real transport, or fails audibly.

Every credential-bearing message in the product (password reset, invitation,
address verification, security notice) used to go through a ~90-line env-only
``smtplib`` helper: ``SMTP_HOST`` defaults to empty and is set in none of the 23
compose files, so out of the box nothing was delivered — and ``_send_email``
returned normally anyway, so the reset endpoint kept telling users "check your
email" forever. Implicit SSL on port 465 was unreachable (STARTTLS or plaintext,
nothing else), and ``FRONTEND_URL`` defaults to ``http://localhost:5173`` with no
compose entry either, so a deployment that configured SMTP but forgot it mailed
every user a link to their own machine.

Meanwhile the product already shipped a DB-backed, admin-managed, AES-256-GCM
provider stack (``watch_email_service``) used only for watch-source notices.

These tests pin the delegation and the four properties that make it safe:
the designated config wins, env SMTP is the fallback, a credential body is never
logged on ANY path, and a failed send is visible to the caller without naming the
recipient.
"""

from __future__ import annotations

import logging
import smtplib
from typing import Any
from typing import cast

import pytest

from app.core.constants import AUTH_EMAIL_CONFIG_SETTING_KEY
from app.core.constants import DEFAULT_FRONTEND_URL
from app.services import email_service as mod
from app.services.email_service import EmailDeliveryError
from app.services.email_service import EmailService
from tests.helpers import does_not_raise

RESET_URL = "https://transcribe.example.org/reset-password?token=SUPERSECRETTOKEN"
LOCALHOST_RESET_URL = f"{DEFAULT_FRONTEND_URL}/reset-password?token=SUPERSECRETTOKEN"
RECIPIENT = "victim@example.com"
CONFIG_UUID = "019ec90a-1b2c-7def-8000-00000000ee01"


class FakeConfig:
    """Stands in for an ``EmailNotificationConfig`` row."""

    def __init__(self, *, enabled: bool = True, uuid: str = CONFIG_UUID) -> None:
        self.uuid = uuid
        self.name = "Corp M365"
        self.provider = "m365"
        self.is_enabled = enabled


class FakeQuery:
    def __init__(self, row):
        self._row = row

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._row


class FakeSession:
    """Answers the two reads ``load_auth_mail_config`` performs.

    ``get_setting`` reaches the DB through ``SystemSettings``; the config lookup
    queries ``EmailNotificationConfig``. Dispatch on the queried model so one fake
    covers both without a database.
    """

    def __init__(self, *, setting_value: str | None, config=None) -> None:
        self._setting_value = setting_value
        self._config = config
        self.closed = False

    def query(self, model):
        if model.__name__ == "SystemSettings":
            row = None
            if self._setting_value is not None:
                row = type(
                    "Row", (), {"key": AUTH_EMAIL_CONFIG_SETTING_KEY, "value": self._setting_value}
                )()
            return FakeQuery(row)
        return FakeQuery(self._config)

    def close(self):
        self.closed = True


def _service(session: FakeSession | None) -> EmailService:
    """An EmailService whose config lookup uses ``session`` (or has no DB).

    The fakes stand in for a ``Session`` structurally — casting is the honest
    statement of that, and keeps the production signature un-widened.
    """
    if session is None:

        def factory() -> Any:
            raise RuntimeError("no database in this test")

        return EmailService(session_factory=factory)
    return EmailService(session_factory=lambda: cast(Any, session))


@pytest.fixture
def no_env_smtp(monkeypatch):
    """The out-of-the-box state: SMTP_HOST empty, as in all 23 compose files."""
    monkeypatch.setattr(mod.settings, "SMTP_HOST", "")
    return mod.settings


@pytest.fixture
def env_smtp(monkeypatch):
    monkeypatch.setattr(mod.settings, "SMTP_HOST", "mail.example.org")
    monkeypatch.setattr(type(mod.settings), "SMTP_PORT", 587)
    monkeypatch.setattr(mod.settings, "SMTP_USER", "")
    monkeypatch.setattr(mod.settings, "SMTP_PASSWORD", "")
    monkeypatch.setattr(mod.settings, "SMTP_FROM", "noreply@example.org")
    monkeypatch.setattr(mod.settings, "SMTP_USE_TLS", True)
    return mod.settings


class RecordingWatchMailer:
    """Captures what was handed to the DB-backed provider stack."""

    def __init__(self, result=(True, "Sent to 1 recipient(s) via Graph")):
        self.calls = []
        self.result = result

    def send_email(self, config, recipients, subject, html_body, timeout=30):
        self.calls.append(
            {
                "config": config,
                "recipients": recipients,
                "subject": subject,
                "html": html_body,
                "timeout": timeout,
            }
        )
        return self.result


@pytest.fixture
def watch_mailer(monkeypatch):
    from app.services import watch_email_service

    recorder = RecordingWatchMailer()
    monkeypatch.setattr(watch_email_service, "send_email", recorder.send_email)
    return recorder


class TestTransportSelection:
    """DB config when designated, env SMTP otherwise, never a config nobody chose."""

    def test_designated_config_is_preferred_over_env_smtp(self, env_smtp, watch_mailer):
        config = FakeConfig()
        session = FakeSession(setting_value=CONFIG_UUID, config=config)

        _service(session).send_password_reset(RECIPIENT, RESET_URL)

        assert len(watch_mailer.calls) == 1
        assert watch_mailer.calls[0]["config"] is config
        assert watch_mailer.calls[0]["recipients"] == [RECIPIENT]

    def test_auth_mail_gets_the_short_socket_timeout(self, env_smtp, watch_mailer):
        """A login/reset request thread must not block on the 30 s scan default."""
        session = FakeSession(setting_value=CONFIG_UUID, config=FakeConfig())

        _service(session).send_password_reset(RECIPIENT, RESET_URL)

        assert watch_mailer.calls[0]["timeout"] == mod.SMTP_TIMEOUT

    def test_no_designation_falls_back_to_env_smtp(self, env_smtp, watch_mailer, monkeypatch):
        sent: dict[str, str] = {}
        monkeypatch.setattr(
            EmailService,
            "_send_via_env_smtp",
            staticmethod(lambda to, subject, html, text: sent.update(to=to)),
        )
        session = FakeSession(setting_value="", config=FakeConfig())

        _service(session).send_password_reset(RECIPIENT, RESET_URL)

        assert sent == {"to": RECIPIENT}
        assert watch_mailer.calls == []

    def test_an_undesignated_config_is_never_borrowed(self, env_smtp, watch_mailer, monkeypatch):
        """A config exists but nobody designated it — using it would mail resets
        out of an unrelated mailbox."""
        sent: dict[str, str] = {}
        monkeypatch.setattr(
            EmailService,
            "_send_via_env_smtp",
            staticmethod(lambda to, subject, html, text: sent.update(to=to)),
        )
        session = FakeSession(setting_value=None, config=FakeConfig())

        _service(session).send_password_reset(RECIPIENT, RESET_URL)

        assert watch_mailer.calls == []
        assert sent == {"to": RECIPIENT}

    def test_disabled_designated_config_degrades_to_env_not_to_another_config(
        self, env_smtp, watch_mailer, monkeypatch
    ):
        sent: dict[str, str] = {}
        monkeypatch.setattr(
            EmailService,
            "_send_via_env_smtp",
            staticmethod(lambda to, subject, html, text: sent.update(to=to)),
        )
        session = FakeSession(setting_value=CONFIG_UUID, config=FakeConfig(enabled=False))

        _service(session).send_password_reset(RECIPIENT, RESET_URL)

        assert watch_mailer.calls == []
        assert sent == {"to": RECIPIENT}

    def test_database_failure_degrades_to_env_smtp(self, env_smtp, watch_mailer, monkeypatch):
        """A DB hiccup must not take down a login-adjacent request."""
        sent: dict[str, str] = {}
        monkeypatch.setattr(
            EmailService,
            "_send_via_env_smtp",
            staticmethod(lambda to, subject, html, text: sent.update(to=to)),
        )

        _service(None).send_password_reset(RECIPIENT, RESET_URL)

        assert sent == {"to": RECIPIENT}

    def test_session_is_closed_after_resolution(self, env_smtp, watch_mailer):
        session = FakeSession(setting_value=CONFIG_UUID, config=FakeConfig())

        _service(session).send_password_reset(RECIPIENT, RESET_URL)

        assert session.closed is True


class TestTransportLayerSelection:
    """Port 465 is implicit SSL; everything else is STARTTLS or plaintext."""

    def test_port_465_uses_implicit_ssl(self, env_smtp, monkeypatch):
        monkeypatch.setattr(type(mod.settings), "SMTP_PORT", 465)
        calls: dict[str, object] = {}
        monkeypatch.setattr(
            smtplib,
            "SMTP_SSL",
            lambda host, port, timeout, context: calls.update(
                kind="ssl", host=host, port=port, timeout=timeout
            ),
        )
        monkeypatch.setattr(
            smtplib, "SMTP", lambda *a, **k: pytest.fail("465 must not use plain SMTP")
        )

        mod._env_smtp_connect()

        assert calls["kind"] == "ssl"
        assert calls["port"] == 465
        assert calls["timeout"] == mod.SMTP_TIMEOUT

    def test_port_587_uses_starttls(self, env_smtp, monkeypatch):
        class FakeSMTP:
            def __init__(self, host, port, timeout):
                self.timeout = timeout
                self.started = False

            def ehlo(self):
                return None

            def starttls(self, context=None):
                self.started = context is not None

        monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
        monkeypatch.setattr(
            smtplib, "SMTP_SSL", lambda *a, **k: pytest.fail("587 must not use implicit SSL")
        )

        server: Any = mod._env_smtp_connect()

        assert server.started is True
        assert server.timeout == mod.SMTP_TIMEOUT

    def test_starttls_is_skipped_when_disabled(self, env_smtp, monkeypatch):
        monkeypatch.setattr(mod.settings, "SMTP_USE_TLS", False)

        class FakeSMTP:
            def __init__(self, host, port, timeout):
                self.started = False

            def ehlo(self):
                return None

            def starttls(self, context=None):
                self.started = True

        monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)

        connected: Any = mod._env_smtp_connect()
        assert connected.started is False


class TestSensitiveBodiesAreNeverLogged:
    """A reset URL is a single-use credential. It must not reach stdout."""

    @staticmethod
    def _assert_clean(caplog):
        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert "SUPERSECRETTOKEN" not in blob
        assert RESET_URL not in blob
        assert RECIPIENT not in blob

    def test_no_transport_configured(self, no_env_smtp, caplog):
        caplog.set_level(logging.DEBUG)
        with pytest.raises(EmailDeliveryError):
            _service(FakeSession(setting_value=None)).send_password_reset(RECIPIENT, RESET_URL)
        self._assert_clean(caplog)

    def test_env_smtp_total_failure(self, env_smtp, caplog, monkeypatch):
        caplog.set_level(logging.DEBUG)

        def explode():
            raise OSError(f"connection refused while relaying for {RECIPIENT}")

        monkeypatch.setattr(mod, "_env_smtp_connect", explode)
        with pytest.raises(EmailDeliveryError):
            _service(FakeSession(setting_value=None)).send_password_reset(RECIPIENT, RESET_URL)
        self._assert_clean(caplog)

    def test_db_config_failure(self, env_smtp, caplog, monkeypatch):
        caplog.set_level(logging.DEBUG)
        from app.services import watch_email_service

        monkeypatch.setattr(
            watch_email_service,
            "send_email",
            lambda *a, **k: (False, f"550 5.1.1 <{RECIPIENT}> recipient rejected"),
        )
        session = FakeSession(setting_value=CONFIG_UUID, config=FakeConfig())
        with pytest.raises(EmailDeliveryError):
            _service(session).send_password_reset(RECIPIENT, RESET_URL)
        self._assert_clean(caplog)

    def test_successful_send_logs_no_body(self, env_smtp, caplog, watch_mailer):
        caplog.set_level(logging.DEBUG)
        session = FakeSession(setting_value=CONFIG_UUID, config=FakeConfig())
        _service(session).send_password_reset(RECIPIENT, RESET_URL)
        with does_not_raise("a successful send must complete without logging the message body"):
            self._assert_clean(caplog)

    def test_invitation_and_verification_bodies_are_sensitive_too(self, no_env_smtp, caplog):
        caplog.set_level(logging.DEBUG)
        svc = _service(FakeSession(setting_value=None))
        with pytest.raises(EmailDeliveryError):
            svc.send_invitation(RECIPIENT, RESET_URL, "admin@example.com", 72, True)
        with pytest.raises(EmailDeliveryError):
            svc.send_email_verification(RECIPIENT, RESET_URL, 24)
        self._assert_clean(caplog)


class TestFailureIsVisibleWithoutDisclosingTheAddress:
    """Honest to the caller, silent about who the recipient was."""

    def test_missing_transport_raises_rather_than_returning(self, no_env_smtp):
        with pytest.raises(EmailDeliveryError) as exc:
            _service(FakeSession(setting_value=None)).send_password_reset(RECIPIENT, RESET_URL)
        assert RECIPIENT not in str(exc.value)

    def test_provider_error_is_scrubbed_of_the_recipient(self, env_smtp, monkeypatch):
        from app.services import watch_email_service

        monkeypatch.setattr(
            watch_email_service,
            "send_email",
            lambda *a, **k: (False, f"550 5.1.1 <{RECIPIENT}> user unknown"),
        )
        session = FakeSession(setting_value=CONFIG_UUID, config=FakeConfig())
        with pytest.raises(EmailDeliveryError) as exc:
            _service(session).send_password_reset(RECIPIENT, RESET_URL)
        message = str(exc.value)
        assert RECIPIENT not in message
        assert "550" in message  # the operator still learns what went wrong

    def test_smtp_recipients_refused_is_scrubbed(self, env_smtp, monkeypatch):
        def explode():
            raise smtplib.SMTPRecipientsRefused({RECIPIENT: (550, b"No such user")})

        monkeypatch.setattr(mod, "_env_smtp_connect", explode)
        with pytest.raises(EmailDeliveryError) as exc:
            _service(FakeSession(setting_value=None)).send_password_reset(RECIPIENT, RESET_URL)
        assert RECIPIENT not in str(exc.value)

    def test_invitation_failure_reaches_the_issuing_admin(self, no_env_smtp):
        with pytest.raises(EmailDeliveryError):
            _service(FakeSession(setting_value=None)).send_invitation(
                RECIPIENT, RESET_URL, "admin@example.com", 72, True
            )

    def test_security_notice_still_logs_in_dev_without_a_transport(self, no_env_smtp, caplog):
        """Non-credential mail keeps the dev console affordance; it is not a secret."""
        caplog.set_level(logging.INFO)
        _service(FakeSession(setting_value=None)).send_security_notice(
            RECIPIENT, "Your account email address was changed", "It changed."
        )
        assert "[DEV]" in "\n".join(r.getMessage() for r in caplog.records)


class TestFrontendUrlGuard:
    """A credential link built from the FRONTEND_URL default is refused."""

    def test_localhost_link_is_refused_once_a_transport_exists(self, env_smtp, watch_mailer):
        session = FakeSession(setting_value=CONFIG_UUID, config=FakeConfig())
        with pytest.raises(EmailDeliveryError) as exc:
            _service(session).send_password_reset(RECIPIENT, LOCALHOST_RESET_URL)
        assert "FRONTEND_URL" in str(exc.value)
        assert watch_mailer.calls == []

    def test_the_refusal_is_logged_at_critical(self, env_smtp, watch_mailer, caplog):
        caplog.set_level(logging.CRITICAL)
        session = FakeSession(setting_value=CONFIG_UUID, config=FakeConfig())
        with pytest.raises(EmailDeliveryError):
            _service(session).send_password_reset(RECIPIENT, LOCALHOST_RESET_URL)
        assert any(r.levelno == logging.CRITICAL for r in caplog.records)

    def test_a_real_url_passes(self, env_smtp, watch_mailer):
        session = FakeSession(setting_value=CONFIG_UUID, config=FakeConfig())
        _service(session).send_password_reset(RECIPIENT, RESET_URL)
        assert len(watch_mailer.calls) == 1

    def test_dev_without_a_transport_is_unaffected(self, no_env_smtp, caplog):
        """The stock dev stack has no transport, so the guard never fires there —
        the failure it reports is 'nothing configured', not 'bad URL'."""
        with pytest.raises(EmailDeliveryError) as exc:
            _service(FakeSession(setting_value=None)).send_password_reset(
                RECIPIENT, LOCALHOST_RESET_URL
            )
        assert "FRONTEND_URL" not in str(exc.value)


class _CountingQuery:
    """A query that answers ``first()`` and ``count()`` for the token rate limit."""

    def __init__(self, row=None, count: int = 0) -> None:
        self._row = row
        self._count = count

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._row

    def count(self):
        return self._count


class _CallerSession:
    """Enough of a session for the two token-issuing helpers, with no database."""

    def __init__(self, user) -> None:
        self._user = user
        self.added: list[object] = []
        self.commits = 0

    def query(self, model):
        return _CountingQuery(self._user if model.__name__ == "User" else None)

    def add(self, row):
        self.added.append(row)

    def commit(self):
        self.commits += 1


class _FakeUser:
    def __init__(self) -> None:
        self.id = 7
        self.email = RECIPIENT
        self.auth_type = "local"
        self.is_active = True
        self.email_verified = False


class TestCallersOnAntiEnumerationPathsAbsorb:
    """A mail outage must not become an account-existence oracle.

    ``request_password_reset`` and ``resend_verification`` return silently for an
    unknown address, so if a *known* address raised past them the response codes
    would differ (200 vs 500) and disclose which accounts exist. The sender is
    deliberately loud (see ``TestFailureIsVisibleWithoutDisclosingTheAddress``);
    these two callers are the ones that must swallow it.
    """

    def test_password_reset_swallows_a_delivery_failure(self, monkeypatch, caplog):
        from app.auth import password_reset

        def explode(*_args, **_kwargs):
            raise EmailDeliveryError("no transport")

        monkeypatch.setattr(password_reset.email_service, "send_password_reset", explode)
        caplog.set_level(logging.ERROR)
        session = _CallerSession(_FakeUser())

        password_reset.request_password_reset(cast(Any, session), RECIPIENT, "203.0.113.5")

        # The token was still persisted, so a later retry/manual send works.
        assert session.commits == 1
        assert any("could not be delivered" in r.getMessage() for r in caplog.records)

    def test_verification_resend_swallows_a_delivery_failure(self, monkeypatch, caplog):
        from app.auth import email_verification

        def explode(*_args, **_kwargs):
            raise EmailDeliveryError("no transport")

        monkeypatch.setattr(email_verification.email_service, "send_email_verification", explode)
        caplog.set_level(logging.ERROR)
        session = _CallerSession(_FakeUser())

        email_verification.issue_verification_token(
            cast(Any, session), cast(Any, _FakeUser()), "203.0.113.5"
        )

        assert session.commits == 1
        assert any("could not be delivered" in r.getMessage() for r in caplog.records)

    def test_invitation_does_not_absorb(self, no_env_smtp):
        """The counterpoint: an admin issuing an invite must learn it never went."""
        with pytest.raises(EmailDeliveryError):
            _service(FakeSession(setting_value=None)).send_invitation(
                RECIPIENT, RESET_URL, "admin@example.com", 72, True
            )


class TestPublicSignaturesAreStable:
    """Callers of these four methods are deliberately out of scope."""

    @pytest.mark.parametrize(
        ("name", "params"),
        [
            ("send_password_reset", ["self", "to_email", "reset_url"]),
            (
                "send_invitation",
                [
                    "self",
                    "to_email",
                    "accept_url",
                    "inviter",
                    "expires_in_hours",
                    "requires_password",
                ],
            ),
            ("send_email_verification", ["self", "to_email", "verify_url", "expires_in_hours"]),
            ("send_security_notice", ["self", "to_email", "subject", "message"]),
        ],
    )
    def test_signature(self, name, params):
        import inspect

        assert list(inspect.signature(getattr(EmailService, name)).parameters) == params
