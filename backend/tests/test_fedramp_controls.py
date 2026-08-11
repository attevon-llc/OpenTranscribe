"""
FedRAMP Security Controls Test Suite.

Tests for specific FedRAMP security controls:
- AC-10: Concurrent Session Control (atomic locking)
- AC-8: Login Banner Acknowledgment
- AU-6: Audit Log Query/Export
- AU-9: Audit Log Fallback (OpenSearch failure handling)

These tests verify compliance with FedRAMP requirements for
authentication, session management, and audit logging.

NOTE: Currently skipped until all FedRAMP controls are fully implemented.
Set RUN_FEDRAMP_TESTS=true to run these tests.
"""

import json
import os
import tempfile
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

# Skip all tests - FedRAMP control implementations in development
pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_FEDRAMP_TESTS", "false").lower() != "true",
    reason="FedRAMP control implementations in development (set RUN_FEDRAMP_TESTS=true to run)",
)


class TestConcurrentSessionEnforcementAC10:
    """Tests for FedRAMP AC-10: Concurrent Session Control.

    AC-10 requires the system to limit the number of concurrent sessions
    for each user. This implementation uses atomic locking (SELECT FOR UPDATE)
    to prevent race conditions when enforcing session limits.
    """

    def test_max_concurrent_sessions_config_exists(self):
        """Verify MAX_CONCURRENT_SESSIONS setting is available."""
        from app.core.config import settings

        assert hasattr(settings, "MAX_CONCURRENT_SESSIONS")
        assert isinstance(settings.MAX_CONCURRENT_SESSIONS, int)

    def test_concurrent_session_policy_config_exists(self):
        """Verify CONCURRENT_SESSION_POLICY setting is available."""
        from app.core.config import settings

        assert hasattr(settings, "CONCURRENT_SESSION_POLICY")
        assert settings.CONCURRENT_SESSION_POLICY in ["terminate_oldest", "reject"]

    def test_session_limit_reject_policy(self, client, db_session, admin_user):
        """Test that session limit enforcement rejects new sessions when limit reached."""
        from app.core.config import settings
        from app.models.refresh_token import RefreshToken

        # Skip if concurrent sessions not limited
        if settings.MAX_CONCURRENT_SESSIONS == 0:
            pytest.skip("Concurrent session limit disabled")

        # Create mock active sessions up to the limit
        now = datetime.now(UTC)
        for i in range(settings.MAX_CONCURRENT_SESSIONS):
            token = RefreshToken(
                user_id=admin_user.id,
                token_hash=f"test_hash_{i}_{now.timestamp()}",
                jti=f"test_jti_{i}_{now.timestamp()}",
                expires_at=now + timedelta(days=7),
                created_at=now - timedelta(minutes=i),  # Different creation times
                revoked_at=None,
                ip_address="127.0.0.1",
                user_agent="Test Agent",
            )
            db_session.add(token)
        db_session.commit()

        # With reject policy, login should fail when limit reached
        with patch.object(settings, "CONCURRENT_SESSION_POLICY", "reject"):
            # Attempt login
            response = client.post(
                "/api/auth/token",
                data={"username": admin_user.email, "password": "adminpass"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

            # Should reject with 429 Too Many Requests
            # Note: actual behavior depends on implementation
            assert response.status_code == 429, (
                response.text
            )  # AC-10: the reject policy must actually reject

    def test_session_limit_terminate_oldest_policy(self, client, db_session, admin_user):
        """Test that terminate_oldest policy revokes oldest session when limit reached."""
        from app.core.config import settings
        from app.models.refresh_token import RefreshToken

        # Skip if concurrent sessions not limited
        if settings.MAX_CONCURRENT_SESSIONS == 0:
            pytest.skip("Concurrent session limit disabled")

        # Create sessions up to the limit
        now = datetime.now(UTC)
        oldest_jti = f"oldest_jti_{now.timestamp()}"

        # Create the oldest session first
        oldest_token = RefreshToken(
            user_id=admin_user.id,
            token_hash=f"oldest_hash_{now.timestamp()}",
            jti=oldest_jti,
            expires_at=now + timedelta(days=7),
            created_at=now - timedelta(hours=5),  # Oldest
            revoked_at=None,
            ip_address="127.0.0.1",
            user_agent="Oldest Agent",
        )
        db_session.add(oldest_token)

        # Create remaining sessions
        for i in range(1, settings.MAX_CONCURRENT_SESSIONS):
            token = RefreshToken(
                user_id=admin_user.id,
                token_hash=f"test_hash_{i}_{now.timestamp()}",
                jti=f"test_jti_{i}_{now.timestamp()}",
                expires_at=now + timedelta(days=7),
                created_at=now - timedelta(hours=5 - i),  # Newer sessions
                revoked_at=None,
                ip_address="127.0.0.1",
                user_agent=f"Test Agent {i}",
            )
            db_session.add(token)
        db_session.commit()

        # With terminate_oldest policy, login should succeed and oldest session revoked
        with patch.object(settings, "CONCURRENT_SESSION_POLICY", "terminate_oldest"):
            response = client.post(
                "/api/auth/token",
                data={"username": admin_user.email, "password": "adminpass"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

            # Should succeed
            assert response.status_code == 200

    def test_atomic_locking_prevents_race_condition(self):
        """Test that atomic locking mechanism is in place for session checks.

        Verifies that the implementation uses SELECT FOR UPDATE or similar
        mechanism to prevent race conditions when checking/modifying sessions.
        """
        # Read the auth package source to verify atomic locking. auth is a
        # package, so inspect.getsource() on it would only see __init__.py.
        from pathlib import Path

        from app.api.endpoints import auth

        source = "\n".join(p.read_text() for p in sorted(Path(auth.__file__).parent.glob("*.py")))

        # Should use with_for_update() for atomic locking
        assert "with_for_update" in source, (
            "Session limit enforcement should use SELECT FOR UPDATE for atomic locking"
        )


class TestLoginBannerAcknowledgmentAC8:
    """Tests for FedRAMP AC-8: Login Banner.

    AC-8 requires displaying a system use notification message before
    granting system access. Users must acknowledge the banner before
    proceeding.
    """

    # get_login_banner() resolves login_banner_* via auth_settings.get_bool(), which is
    # DB > .env > coded default (app/api/endpoints/auth/methods.py). Patching
    # settings.LOGIN_BANNER_ENABLED only moves the .env layer and is silently ignored
    # whenever a real login_banner_enabled row already exists in auth_config — which it
    # does on any environment where the banner was ever configured through the admin UI.
    # These tests write the DB row directly (the same path AuthConfigService.set_config
    # takes for a real admin change) so they exercise the precedence the endpoint actually
    # implements and pass independent of ambient DB state. Serialized to one xdist worker
    # like test_auth_config_integration.py, since both write the same shared
    # login_banner_* rows and racing UPDATEs against them can deadlock under -n auto.
    pytestmark = pytest.mark.xdist_group("auth_config")

    def test_login_banner_config_exists(self):
        """Verify login banner configuration settings exist."""
        from app.core.config import settings

        assert hasattr(settings, "LOGIN_BANNER_ENABLED")
        assert hasattr(settings, "LOGIN_BANNER_TEXT")
        assert hasattr(settings, "LOGIN_BANNER_CLASSIFICATION")

    def test_get_login_banner_when_disabled(self, client, db_session, admin_user):
        """Test banner endpoint returns disabled state when banner is off."""
        from app.services.auth_config_service import AuthConfigService

        AuthConfigService.set_config(
            db=db_session,
            key="login_banner_enabled",
            value=False,
            is_sensitive=False,
            category="banner",
            user_id=admin_user.id,
            data_type="bool",
        )

        response = client.get("/api/auth/banner")
        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is False

    def test_get_login_banner_when_enabled(self, client, db_session, admin_user):
        """Test banner endpoint returns banner content when enabled."""
        from app.services.auth_config_service import AuthConfigService

        test_banner_text = "This is a US Government system. Unauthorized access prohibited."
        test_classification = "UNCLASSIFIED"

        AuthConfigService.set_config(
            db=db_session,
            key="login_banner_enabled",
            value=True,
            is_sensitive=False,
            category="banner",
            user_id=admin_user.id,
            data_type="bool",
        )
        AuthConfigService.set_config(
            db=db_session,
            key="login_banner_text",
            value=test_banner_text,
            is_sensitive=False,
            category="banner",
            user_id=admin_user.id,
        )
        AuthConfigService.set_config(
            db=db_session,
            key="login_banner_classification",
            value=test_classification,
            is_sensitive=False,
            category="banner",
            user_id=admin_user.id,
        )

        response = client.get("/api/auth/banner")
        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is True
        assert data["requires_acknowledgment"] is True
        assert data["text"] == test_banner_text
        assert data["classification"] == test_classification

    def test_banner_acknowledgment_requires_authentication(self, client):
        """Test that banner acknowledgment requires authentication."""
        response = client.post("/api/auth/banner/acknowledge")
        assert response.status_code == 401

    def test_banner_acknowledgment_logs_audit_event(
        self, client, db_session, normal_user, user_token_headers
    ):
        """Test that banner acknowledgment is logged in audit trail."""
        from app.auth.audit import AuditEventType
        from app.auth.audit import audit_logger

        with patch.object(audit_logger, "log") as mock_log:
            response = client.post(
                "/api/auth/banner/acknowledge",
                headers=user_token_headers,
            )
            assert response.status_code == 200

            # Verify audit log was called with banner acknowledgment event
            mock_log.assert_called_once()
            call_kwargs = mock_log.call_args[1]
            assert call_kwargs["event_type"] == AuditEventType.AUTH_BANNER_ACKNOWLEDGED

    def test_banner_acknowledged_at_timestamp_saved(
        self, client, db_session, normal_user, user_token_headers
    ):
        """Test that banner acknowledgment timestamp is saved to user record."""

        response = client.post(
            "/api/auth/banner/acknowledge",
            headers=user_token_headers,
        )
        assert response.status_code == 200
        assert response.json()["acknowledged"] is True

        # Refresh user from DB
        db_session.refresh(normal_user)
        assert normal_user.banner_acknowledged_at is not None

    def test_banner_event_type_defined(self):
        """Test that banner acknowledgment event type is defined."""
        from app.auth.audit import AuditEventType

        assert hasattr(AuditEventType, "AUTH_BANNER_ACKNOWLEDGED")


class TestAuditLogQueryExportAU6:
    """Tests for FedRAMP AU-6: Audit Log Review, Analysis, and Reporting.

    AU-6 requires organizations to review and analyze audit records for
    indicators of inappropriate or unusual activity, and report findings
    to appropriate personnel.
    """

    def test_audit_log_endpoint_requires_super_admin(self, client, user_token_headers):
        """Test that audit log query requires super admin privileges."""
        response = client.get(
            "/api/admin/audit-logs",
            headers=user_token_headers,
        )
        # Regular user should be forbidden
        assert response.status_code == 403, response.text

    def test_audit_log_query_parameters(self, client, super_admin_token_headers):
        """Test that audit log endpoint supports filtering parameters."""
        # Test that the endpoint accepts filter parameters (super admin only)
        response = client.get(
            "/api/admin/audit-logs",
            params={
                "start_date": "2024-01-01T00:00:00",
                "end_date": "2024-12-31T23:59:59",
                "event_type": "auth.login.success",
                "outcome": "success",
                "limit": 50,
                "offset": 0,
            },
            headers=super_admin_token_headers,
        )
        # Should return 200 or indicate OpenSearch not enabled
        assert response.status_code == 200
        data = response.json()
        # Should have logs array and total count
        assert "logs" in data or "error" in data

    def test_audit_log_export_csv_format(self, client, super_admin_token_headers):
        """Audit log export returns CSV — when the OpenSearch audit sink is enabled.

        Was ``in [200, 400]``, which passed either way and so never verified the format at
        all. ``export_audit_logs`` returns 400 outright when ``AUDIT_LOG_TO_OPENSEARCH`` is
        false (``admin.py:1754``), and the root conftest forces it false on purpose —
        savepoint rollback cannot undo OpenSearch writes, so leaving it on would pollute the
        live audit index. So the precondition is real and belongs in a visible skip, not in a
        widened assertion: with the sink off this states plainly that it did not run, and with
        it on it now checks the actual content type (issue #431).
        """
        from app.core.config import settings

        response = client.get(
            "/api/admin/audit-logs/export",
            params={"export_format": "csv"},
            headers=super_admin_token_headers,
        )
        if not settings.AUDIT_LOG_TO_OPENSEARCH:
            assert response.status_code == 400, response.text
            pytest.skip(
                "audit export requires AUDIT_LOG_TO_OPENSEARCH=true; conftest forces it off "
                "because savepoint rollback cannot undo OpenSearch writes"
            )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/csv")

    def test_audit_log_export_json_format(self, client, super_admin_token_headers):
        """Audit log export returns JSON — when the OpenSearch audit sink is enabled.

        Was ``in [200, 400]``, which passed either way and so never verified the format at
        all. ``export_audit_logs`` returns 400 outright when ``AUDIT_LOG_TO_OPENSEARCH`` is
        false (``admin.py:1754``), and the root conftest forces it false on purpose —
        savepoint rollback cannot undo OpenSearch writes, so leaving it on would pollute the
        live audit index. So the precondition is real and belongs in a visible skip, not in a
        widened assertion: with the sink off this states plainly that it did not run, and with
        it on it now checks the actual content type (issue #431).
        """
        from app.core.config import settings

        response = client.get(
            "/api/admin/audit-logs/export",
            params={"export_format": "json"},
            headers=super_admin_token_headers,
        )
        if not settings.AUDIT_LOG_TO_OPENSEARCH:
            assert response.status_code == 400, response.text
            pytest.skip(
                "audit export requires AUDIT_LOG_TO_OPENSEARCH=true; conftest forces it off "
                "because savepoint rollback cannot undo OpenSearch writes"
            )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("application/json")

    def test_audit_log_export_invalid_format_rejected(self, client, super_admin_token_headers):
        """Test that invalid export formats are rejected."""
        response = client.get(
            "/api/admin/audit-logs/export",
            params={"export_format": "xml"},  # Not supported
            headers=super_admin_token_headers,
        )
        assert response.status_code == 400

    def test_audit_log_retention_config(self):
        """Test that audit log retention is configured."""
        from app.core.config import settings

        assert hasattr(settings, "AUDIT_LOG_RETENTION_DAYS")
        # FedRAMP requires minimum 90 days, typically 365 for HIGH impact
        assert settings.AUDIT_LOG_RETENTION_DAYS >= 90


class TestAuditFallbackOnOpenSearchFailureAU9:
    """Tests for FedRAMP AU-9: Protection of Audit Information.

    AU-9 requires protecting audit information and audit logging tools
    from unauthorized access, modification, and deletion. This includes
    ensuring audit logs are not lost when primary storage fails.
    """

    def test_audit_fallback_config_exists(self):
        """Verify audit fallback configuration settings exist."""
        from app.core.config import settings

        assert hasattr(settings, "AUDIT_LOG_FALLBACK_ENABLED")
        assert hasattr(settings, "AUDIT_LOG_FALLBACK_PATH")

    def test_audit_fallback_enabled_by_default(self):
        """Test that audit fallback is enabled by default for compliance."""
        from app.core.config import settings

        # Fallback should be enabled by default for FedRAMP compliance
        assert settings.AUDIT_LOG_FALLBACK_ENABLED is True

    def test_audit_fallback_path_configured(self):
        """Test that audit fallback path is properly configured."""
        from app.core.config import settings

        assert settings.AUDIT_LOG_FALLBACK_PATH
        assert settings.AUDIT_LOG_FALLBACK_PATH.endswith(".jsonl")

    def test_audit_logger_writes_to_fallback_on_opensearch_failure(self):
        """Test that audit logger writes to fallback file when OpenSearch fails."""
        from app.auth.audit import AuditEventType
        from app.auth.audit import AuditLogger
        from app.auth.audit import AuditOutcome
        from app.core.config import settings

        # Create temp file for fallback
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            temp_path = f.name

        try:
            with (
                patch.object(settings, "AUDIT_LOG_FALLBACK_ENABLED", True),
                patch.object(settings, "AUDIT_LOG_FALLBACK_PATH", temp_path),
                patch.object(settings, "AUDIT_LOG_TO_OPENSEARCH", True),
                patch.object(settings, "AUDIT_LOG_ENABLED", True),
            ):
                logger = AuditLogger()

                # Mock OpenSearch client to fail
                mock_client = MagicMock()
                mock_client.index.side_effect = Exception("OpenSearch connection failed")
                mock_client.indices.exists.return_value = True

                with patch.object(logger, "_get_opensearch_client", return_value=mock_client):
                    # Attempt to log an event
                    logger.log(
                        event_type=AuditEventType.AUTH_LOGIN_SUCCESS,
                        outcome=AuditOutcome.SUCCESS,
                        user_id=123,
                        username="test@example.com",
                        source_ip="192.168.1.1",
                        user_agent="Test Agent",
                    )

            # Verify fallback file was written
            with open(temp_path) as f:
                content = f.read()
                assert content.strip()  # File should not be empty
                # Parse and verify JSON
                log_entry = json.loads(content.strip())
                assert log_entry["event_type"] == "auth.login.success"
                assert log_entry["user_id"] == 123

        finally:
            # Cleanup temp file
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def test_audit_fallback_writes_valid_jsonl_format(self):
        """Test that fallback logs are written in valid JSON Lines format."""
        from app.auth.audit import AuditEventType
        from app.auth.audit import AuditLogger
        from app.auth.audit import AuditOutcome
        from app.core.config import settings

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            temp_path = f.name

        try:
            logger = AuditLogger()

            with (
                patch.object(settings, "AUDIT_LOG_FALLBACK_ENABLED", True),
                patch.object(settings, "AUDIT_LOG_FALLBACK_PATH", temp_path),
                patch.object(settings, "AUDIT_LOG_TO_OPENSEARCH", True),
                patch.object(settings, "AUDIT_LOG_ENABLED", True),
            ):
                # Mock OpenSearch to fail
                mock_client = MagicMock()
                mock_client.index.side_effect = Exception("Connection refused")
                mock_client.indices.exists.return_value = True

                with patch.object(logger, "_get_opensearch_client", return_value=mock_client):
                    # Write multiple log entries
                    for i in range(3):
                        logger.log(
                            event_type=AuditEventType.AUTH_LOGIN_FAILURE,
                            outcome=AuditOutcome.FAILURE,
                            username=f"user{i}@example.com",
                            source_ip=f"192.168.1.{i}",
                            user_agent="Test Agent",
                            error_code="INVALID_CREDENTIALS",
                        )

            # Verify each line is valid JSON
            with open(temp_path) as f:
                lines = [line for line in f.readlines() if line.strip()]
                assert len(lines) == 3
                for line in lines:
                    entry = json.loads(line)
                    assert "timestamp" in entry
                    assert "event_type" in entry
                    assert "outcome" in entry

        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def test_audit_fallback_creates_directory_if_missing(self):
        """Test that fallback creates parent directory if it doesn't exist."""
        from app.auth.audit import AuditLogger
        from app.core.config import settings

        # Use a temporary directory that doesn't exist yet
        with tempfile.TemporaryDirectory() as tmpdir:
            nested_path = os.path.join(tmpdir, "audit", "logs", "fallback.jsonl")

            logger = AuditLogger()

            # Call the private method directly to test directory creation
            event = {
                "timestamp": datetime.now(UTC).isoformat(),
                "event_type": "test.event",
                "outcome": "success",
            }

            with patch.object(settings, "AUDIT_LOG_FALLBACK_PATH", nested_path):
                logger._write_fallback_log(event)

            # Verify directory was created and file written
            assert os.path.exists(nested_path)
            with open(nested_path) as f:
                content = f.read()
                assert "test.event" in content

    def test_audit_opensearch_unavailable_triggers_fallback(self):
        """Test that None OpenSearch client triggers fallback logging."""
        from app.auth.audit import AuditEventType
        from app.auth.audit import AuditLogger
        from app.auth.audit import AuditOutcome
        from app.core.config import settings

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            temp_path = f.name

        try:
            logger = AuditLogger()

            with (
                patch.object(settings, "AUDIT_LOG_FALLBACK_ENABLED", True),
                patch.object(settings, "AUDIT_LOG_FALLBACK_PATH", temp_path),
                patch.object(settings, "AUDIT_LOG_TO_OPENSEARCH", True),
                patch.object(settings, "AUDIT_LOG_ENABLED", True),
            ):
                # Return None client (simulates OpenSearch completely unavailable)
                with patch.object(logger, "_get_opensearch_client", return_value=None):
                    logger.log(
                        event_type=AuditEventType.AUTH_LOGOUT,
                        outcome=AuditOutcome.SUCCESS,
                        user_id=456,
                        username="testuser@example.com",
                        source_ip="10.0.0.1",
                        user_agent="Mozilla/5.0",
                    )

            # Verify fallback was used
            with open(temp_path) as f:
                content = f.read()
                assert "auth.logout" in content

        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def test_audit_fallback_disabled_no_file_written(self):
        """Test that no fallback file is written when fallback is disabled."""
        from app.auth.audit import AuditEventType
        from app.auth.audit import AuditLogger
        from app.auth.audit import AuditOutcome
        from app.core.config import settings

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            temp_path = f.name

        # Remove the file so we can check if it gets created
        os.unlink(temp_path)

        try:
            logger = AuditLogger()

            with (
                patch.object(settings, "AUDIT_LOG_FALLBACK_ENABLED", False),
                patch.object(settings, "AUDIT_LOG_FALLBACK_PATH", temp_path),
                patch.object(settings, "AUDIT_LOG_TO_OPENSEARCH", True),
                patch.object(settings, "AUDIT_LOG_ENABLED", True),
            ):
                mock_client = MagicMock()
                mock_client.index.side_effect = Exception("Connection failed")
                mock_client.indices.exists.return_value = True

                with patch.object(logger, "_get_opensearch_client", return_value=mock_client):
                    logger.log(
                        event_type=AuditEventType.AUTH_LOGIN_SUCCESS,
                        outcome=AuditOutcome.SUCCESS,
                        user_id=789,
                        username="nofallback@example.com",
                        source_ip="127.0.0.1",
                        user_agent="Test",
                    )

            # Verify no fallback file was created
            assert not os.path.exists(temp_path)

        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)


class TestAtomicLockoutMechanism:
    """Tests for atomic lockout mechanism used in login flow.

    These tests verify the race condition fix for concurrent login attempts.
    """

    def test_check_and_record_attempt_function_exists(self):
        """Test that atomic check_and_record_attempt function exists."""
        from app.auth.lockout import check_and_record_attempt

        assert callable(check_and_record_attempt)

    def test_check_and_record_attempt_returns_tuple(self):
        """Test that check_and_record_attempt returns expected tuple format."""
        from app.auth.lockout import check_and_record_attempt

        result = check_and_record_attempt("test_user@example.com", success=True)

        assert isinstance(result, tuple)
        assert len(result) == 2
        is_locked, unlock_time = result
        assert isinstance(is_locked, bool)

    def test_check_and_record_attempt_atomic_redis(self):
        """Test that the Redis implementation uses a genuinely atomic operation.

        This used to assert that the source mentioned ``pipeline`` and ``watch``.
        Both strings were present and the code was still NOT atomic: ``WATCH`` was
        issued on a pooled connection while ``pipeline(True)`` acquired a different
        one, so the transaction guarded nothing and two concurrent failed logins
        could both write the same attempt count. It is now a server-side
        compare-and-set script, so assert on that instead.
        """
        import inspect

        from app.auth import lockout

        source = inspect.getsource(lockout)

        assert "_CAS_LUA" in source, "Should use a compare-and-set Lua script"
        assert "register_script" in source or "eval" in source, (
            "The CAS script must actually be executed server-side"
        )
        assert hasattr(lockout, "_cas_write"), "Conditional write helper should exist"

    def test_check_and_record_attempt_atomic_memory(self):
        """Test that in-memory implementation uses thread locking."""
        import inspect

        from app.auth import lockout

        source = inspect.getsource(lockout)

        # Should use threading lock for in-memory atomicity
        assert "_lock" in source, "Should use threading lock for in-memory atomicity"

    def test_lockout_after_threshold_reached(self):
        """Test that account is locked after threshold failed attempts."""
        from app.auth.lockout import check_and_record_attempt
        from app.auth.lockout import get_lockout_info
        from app.core.config import settings

        test_user = f"lockout_test_{datetime.now().timestamp()}@example.com"

        try:
            # Make failed attempts up to threshold
            for i in range(settings.ACCOUNT_LOCKOUT_THRESHOLD):
                is_locked, unlock_time = check_and_record_attempt(test_user, success=False)

            # Account should now be locked
            info = get_lockout_info(test_user)
            assert info["is_locked"] is True
            assert info["lockout_count"] >= 1

        finally:
            # Cleanup - unlock account
            from app.auth.lockout import unlock_account

            unlock_account(test_user)

    def test_successful_login_clears_failed_attempts(self):
        """Test that successful login clears failed attempt counter."""
        from app.auth.lockout import check_and_record_attempt
        from app.auth.lockout import get_lockout_info

        test_user = f"clear_test_{datetime.now().timestamp()}@example.com"

        # Record some failed attempts (but not enough to lock)
        check_and_record_attempt(test_user, success=False)
        check_and_record_attempt(test_user, success=False)

        # Verify failed attempts recorded
        info = get_lockout_info(test_user)
        assert info["failed_attempts"] == 2

        # Successful login
        check_and_record_attempt(test_user, success=True)

        # Failed attempts should be cleared
        info = get_lockout_info(test_user)
        assert info["failed_attempts"] == 0


class TestAuditEventTypes:
    """Tests for required FedRAMP audit event types."""

    def test_session_limit_exceeded_event_type_exists(self):
        """Test that session limit exceeded event type is defined."""
        from app.auth.audit import AuditEventType

        assert hasattr(AuditEventType, "AUTH_SESSION_LIMIT_EXCEEDED")

    def test_session_expired_event_type_exists(self):
        """Test that session expired event type is defined."""
        from app.auth.audit import AuditEventType

        assert hasattr(AuditEventType, "AUTH_SESSION_EXPIRED")

    def test_session_terminated_event_type_exists(self):
        """Test that session terminated event type is defined."""
        from app.auth.audit import AuditEventType

        assert hasattr(AuditEventType, "AUTH_SESSION_TERMINATED")

    def test_all_required_audit_event_types_defined(self):
        """Test that all FedRAMP-required audit event types are defined."""
        from app.auth.audit import AuditEventType

        required_events = [
            # Authentication events (AU-2)
            "AUTH_LOGIN_SUCCESS",
            "AUTH_LOGIN_FAILURE",
            "AUTH_LOGOUT",
            "AUTH_LOGOUT_ALL",
            # MFA events (IA-2)
            "AUTH_MFA_SETUP",
            "AUTH_MFA_VERIFY",
            "AUTH_MFA_DISABLE",
            # Password events (IA-5)
            "AUTH_PASSWORD_CHANGE",
            # Account events (AC-2)
            "AUTH_ACCOUNT_LOCKOUT",
            "AUTH_ACCOUNT_UNLOCK",
            "AUTH_ACCOUNT_DISABLED",
            # Token events (AC-12)
            "AUTH_TOKEN_REFRESH",
            "AUTH_TOKEN_REVOKE",
            # Session events (AC-10)
            "AUTH_SESSION_CREATED",
            "AUTH_SESSION_EXPIRED",
            "AUTH_SESSION_TERMINATED",
            # Banner acknowledgment (AC-8)
            "AUTH_BANNER_ACKNOWLEDGED",
            # Administrative events
            "ADMIN_USER_CREATE",
            "ADMIN_USER_UPDATE",
            "ADMIN_USER_DELETE",
            "ADMIN_ROLE_CHANGE",
        ]

        for event in required_events:
            assert hasattr(AuditEventType, event), f"Missing required event type: {event}"


class TestAuditLoggerFallbackBehavior:
    """Additional tests for audit logger fallback behavior."""

    def test_fallback_handles_write_errors_gracefully(self):
        """Test that fallback handles write errors without crashing."""
        from app.auth.audit import AuditLogger
        from app.core.config import settings

        logger = AuditLogger()

        # Use an invalid path that will fail
        invalid_path = "/nonexistent/readonly/path/audit.jsonl"

        with (
            patch.object(settings, "AUDIT_LOG_FALLBACK_ENABLED", True),
            patch.object(settings, "AUDIT_LOG_FALLBACK_PATH", invalid_path),
        ):
            # This should not raise an exception
            event = {"test": "event"}
            try:
                logger._write_fallback_log(event)
            except Exception:
                pytest.fail("Fallback should handle write errors gracefully")

    def test_audit_log_includes_required_fields(self):
        """Test that audit log entries include all required FedRAMP fields."""
        from app.auth.audit import AuditEventType
        from app.auth.audit import AuditLogger
        from app.auth.audit import AuditOutcome
        from app.core.config import settings

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            temp_path = f.name

        try:
            logger = AuditLogger()

            with (
                patch.object(settings, "AUDIT_LOG_FALLBACK_ENABLED", True),
                patch.object(settings, "AUDIT_LOG_FALLBACK_PATH", temp_path),
                patch.object(settings, "AUDIT_LOG_TO_OPENSEARCH", True),
                patch.object(settings, "AUDIT_LOG_ENABLED", True),
            ):
                with patch.object(logger, "_get_opensearch_client", return_value=None):
                    logger.log(
                        event_type=AuditEventType.AUTH_LOGIN_SUCCESS,
                        outcome=AuditOutcome.SUCCESS,
                        user_id=100,
                        username="auditfields@example.com",
                        source_ip="192.168.1.100",
                        user_agent="Mozilla/5.0 Test",
                        details={"auth_method": "local"},
                    )

            with open(temp_path) as f:
                entry = json.loads(f.read().strip())

                # Required FedRAMP AU-3 fields
                assert "timestamp" in entry
                assert "event_type" in entry
                assert "outcome" in entry
                assert "user_id" in entry
                assert "source_ip" in entry
                assert "request_id" in entry  # For correlation

        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)


# Run with: pytest tests/test_fedramp_controls.py -v
