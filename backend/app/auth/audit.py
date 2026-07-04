"""
Structured Audit Logging Service for FedRAMP AU-2/AU-3 Compliance.

This module provides JSON-structured audit logging with optional OpenSearch integration.
Supports logging of authentication events, security events, and administrative actions.
"""

import json
import logging
import os
import uuid
from contextvars import ContextVar
from datetime import UTC
from datetime import datetime
from enum import StrEnum
from typing import Any

from app.core.config import settings

# Context variable for request ID (set by middleware)
request_id_var: ContextVar[str] = ContextVar("request_id", default="")


class AuditEventType(StrEnum):
    """Audit event types for FedRAMP compliance."""

    # Authentication events
    AUTH_LOGIN_SUCCESS = "auth.login.success"
    AUTH_LOGIN_FAILURE = "auth.login.failure"
    AUTH_LOGOUT = "auth.logout"
    AUTH_LOGOUT_ALL = "auth.logout.all"

    # MFA events
    AUTH_MFA_SETUP = "auth.mfa.setup"
    AUTH_MFA_VERIFY = "auth.mfa.verify"
    AUTH_MFA_DISABLE = "auth.mfa.disable"
    AUTH_MFA_BACKUP_USED = "auth.mfa.backup_used"

    # Password events (event type names, not passwords)
    AUTH_PASSWORD_CHANGE = "auth.password.change"  # noqa: S105 # nosec B105
    AUTH_PASSWORD_RESET_REQUEST = "auth.password.reset_request"  # noqa: S105 # nosec B105
    AUTH_PASSWORD_RESET_COMPLETE = "auth.password.reset_complete"  # noqa: S105 # nosec B105
    AUTH_PASSWORD_EXPIRED = "auth.password.expired"  # noqa: S105 # nosec B105

    # Account events
    AUTH_ACCOUNT_LOCKOUT = "auth.account.lockout"
    AUTH_ACCOUNT_UNLOCK = "auth.account.unlock"
    AUTH_ACCOUNT_DISABLED = "auth.account.disabled"
    AUTH_ACCOUNT_EXPIRED = "auth.account.expired"

    # Token events (event type names, not passwords)
    AUTH_TOKEN_REFRESH = "auth.token.refresh"  # noqa: S105 # nosec B105
    AUTH_TOKEN_REVOKE = "auth.token.revoke"  # noqa: S105 # nosec B105
    AUTH_TOKEN_VERIFY = "auth.token.verify"  # noqa: S105 # nosec B105

    # Session events
    AUTH_SESSION_CREATED = "auth.session.created"
    AUTH_SESSION_EXPIRED = "auth.session.expired"
    AUTH_SESSION_TERMINATED = "auth.session.terminated"
    AUTH_SESSION_LIMIT_EXCEEDED = "auth.session.limit_exceeded"

    # Administrative events
    ADMIN_USER_CREATE = "admin.user.create"
    ADMIN_USER_UPDATE = "admin.user.update"
    ADMIN_USER_DELETE = "admin.user.delete"
    ADMIN_ROLE_CHANGE = "admin.role.change"
    ADMIN_SETTINGS_CHANGE = "admin.settings.change"

    # Abuse / DMCA / safe-harbor takedown events
    ADMIN_FILE_QUARANTINE = "admin.file.quarantine"
    ADMIN_FILE_RELEASE = "admin.file.release"

    # Prompt sharing events
    PROMPT_SHARE = "prompt.share"
    PROMPT_UNSHARE = "prompt.unshare"
    PROMPT_CLONE = "prompt.clone"

    # Banner acknowledgment
    AUTH_BANNER_ACKNOWLEDGED = "auth.banner.acknowledged"


class AuditOutcome(StrEnum):
    """Audit event outcomes."""

    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"


class AuditLogger:
    """
    Structured audit logging service.

    Provides JSON-formatted audit logs compliant with FedRAMP AU-2/AU-3 requirements.
    """

    def __init__(self):
        """Initialize the audit logger."""
        self._logger = logging.getLogger("audit")
        self._opensearch_client = None

        # Configure audit logger if not already configured
        if not self._logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)
            self._logger.setLevel(logging.INFO)
            self._logger.propagate = False

    def _get_opensearch_client(self):
        """Get or create OpenSearch client for audit indexing."""
        if not settings.AUDIT_LOG_TO_OPENSEARCH:
            return None

        if self._opensearch_client is None:
            try:
                from opensearchpy import OpenSearch

                self._opensearch_client = OpenSearch(
                    hosts=[
                        {
                            "host": settings.OPENSEARCH_HOST,
                            "port": int(settings.OPENSEARCH_PORT),
                        }
                    ],
                    http_auth=(settings.OPENSEARCH_USER, settings.OPENSEARCH_PASSWORD),
                    use_ssl=settings.OPENSEARCH_USE_TLS,
                    verify_certs=settings.OPENSEARCH_VERIFY_CERTS,
                    ssl_show_warn=False,
                )
            except Exception as e:
                self._logger.warning(f"Failed to initialize OpenSearch client: {e}")
                return None

        return self._opensearch_client

    def _format_json(self, event: dict) -> str:
        """Format event as JSON string."""
        return json.dumps(event, default=str, ensure_ascii=False)

    def _format_cef(self, event: dict) -> str:
        """Format event as CEF (Common Event Format) string.

        CEF Format: CEF:Version|Device Vendor|Device Product|Device Version|Signature ID|Name|Severity|Extension
        """
        severity_map = {
            AuditOutcome.SUCCESS: 3,
            AuditOutcome.FAILURE: 7,
            AuditOutcome.PARTIAL: 5,
        }

        outcome = event.get("outcome")
        severity = severity_map.get(outcome, 5) if outcome in severity_map else 5  # type: ignore[comparison-overlap]

        # Build extension fields
        extensions = []
        if event.get("source_ip"):
            extensions.append(f"src={event['source_ip']}")
        if event.get("user_id"):
            extensions.append(f"duid={event['user_id']}")
        if event.get("username"):
            extensions.append(f"suser={event['username']}")
        if event.get("request_id"):
            extensions.append(f"externalId={event['request_id']}")

        extension_str = " ".join(extensions)

        return (
            f"CEF:0|OpenTranscribe|AuthService|1.0|"
            f"{event.get('event_type', 'unknown')}|"
            f"{event.get('event_type', 'unknown')}|"
            f"{severity}|{extension_str}"
        )

    def _write_fallback_log(self, event: dict) -> None:
        """Write audit event to fallback file when OpenSearch is unavailable.

        Creates the directory structure if it doesn't exist and appends
        the event as a JSON line to the fallback file.

        Args:
            event: The audit event dictionary to write.
        """
        try:
            fallback_path = settings.AUDIT_LOG_FALLBACK_PATH
            # Create directory if it doesn't exist
            os.makedirs(os.path.dirname(fallback_path), exist_ok=True)

            # Append event as JSON line
            with open(fallback_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, default=str, ensure_ascii=False) + "\n")
        except Exception as e:
            self._logger.error(f"Failed to write to audit fallback log: {e}")

    def _index_to_opensearch(self, event: dict) -> None:
        """Index audit event to OpenSearch."""
        client = self._get_opensearch_client()
        if client is None:
            # OpenSearch client not available, use fallback if enabled
            if settings.AUDIT_LOG_FALLBACK_ENABLED:
                self._logger.warning("OpenSearch client unavailable, using fallback file logging")
                self._write_fallback_log(event)
            return

        try:
            index_name = f"audit-logs-{datetime.now(UTC).strftime('%Y.%m')}"

            # Ensure index exists with proper mappings
            if not client.indices.exists(index=index_name):
                client.indices.create(
                    index=index_name,
                    body={
                        "mappings": {
                            "properties": {
                                "timestamp": {"type": "date"},
                                "event_type": {"type": "keyword"},
                                "outcome": {"type": "keyword"},
                                "user_id": {"type": "integer"},
                                "organization_id": {"type": "integer"},
                                "username": {"type": "keyword"},
                                "source_ip": {"type": "ip"},
                                "user_agent": {"type": "text"},
                                "request_id": {"type": "keyword"},
                                "error_code": {"type": "keyword"},
                                "details": {"type": "object", "enabled": True},
                            }
                        },
                        "settings": {
                            "number_of_shards": 1,
                            "number_of_replicas": 0,
                        },
                    },
                )

            client.index(index=index_name, body=event)
        except Exception as e:
            self._logger.warning(f"Failed to index audit event to OpenSearch: {e}")
            # Use fallback logging if enabled
            if settings.AUDIT_LOG_FALLBACK_ENABLED:
                self._logger.warning("Using fallback file logging for audit event")
                self._write_fallback_log(event)

    def log(
        self,
        event_type: AuditEventType,
        outcome: AuditOutcome,
        user_id: int | None = None,
        username: str | None = None,
        source_ip: str | None = None,
        user_agent: str | None = None,
        error_code: str | None = None,
        details: dict[str, Any] | None = None,
        organization_id: int | None = None,
    ) -> None:
        """
        Log an audit event.

        Args:
            event_type: The type of audit event
            outcome: The outcome of the event (success/failure/partial)
            user_id: The user's database ID (if known)
            username: The username or email (for failed logins where user_id unknown)
            source_ip: The client's IP address
            user_agent: The client's User-Agent header
            error_code: Error code if applicable
            details: Additional event-specific details
            organization_id: Tenant the event occurred in (issue #262a). Call
                sites pass it when they have context — the request's resolved
                org (``ctx.org_id`` / ``request.state.org_id``) or the affected
                resource's org stamp. ``None`` (community, personal scope, or
                no-context writers like local login) omits the field; those
                events are attributed to an org at READ time via the member-id
                legacy fallback in :func:`query_audit_logs`.
        """
        if not settings.AUDIT_LOG_ENABLED:
            return

        event = {
            "timestamp": datetime.now(UTC).isoformat(),
            "request_id": request_id_var.get() or str(uuid.uuid4()),
            "event_type": event_type.value
            if isinstance(event_type, AuditEventType)
            else event_type,
            "outcome": outcome.value if isinstance(outcome, AuditOutcome) else outcome,
            "user_id": user_id,
            "organization_id": organization_id,
            "username": username,
            "source_ip": source_ip,
            "user_agent": user_agent,
            "error_code": error_code,
            "details": details or {},
        }

        # Log in configured format
        if settings.AUDIT_LOG_FORMAT.lower() == "cef":
            self._logger.info(self._format_cef(event))
        else:
            self._logger.info(self._format_json(event))

        # Index to OpenSearch if enabled
        if settings.AUDIT_LOG_TO_OPENSEARCH:
            self._index_to_opensearch(event)

    def log_login_success(
        self,
        user_id: int,
        username: str,
        source_ip: str,
        user_agent: str,
        auth_method: str,
    ) -> None:
        """Log a successful login."""
        self.log(
            event_type=AuditEventType.AUTH_LOGIN_SUCCESS,
            outcome=AuditOutcome.SUCCESS,
            user_id=user_id,
            username=username,
            source_ip=source_ip,
            user_agent=user_agent,
            details={"auth_method": auth_method},
        )

    def log_login_failure(
        self,
        username: str,
        source_ip: str,
        user_agent: str,
        error_code: str,
        auth_method: str,
        lockout_count: int = 0,
    ) -> None:
        """Log a failed login attempt."""
        self.log(
            event_type=AuditEventType.AUTH_LOGIN_FAILURE,
            outcome=AuditOutcome.FAILURE,
            username=username,
            source_ip=source_ip,
            user_agent=user_agent,
            error_code=error_code,
            details={"auth_method": auth_method, "lockout_count": lockout_count},
        )

    def log_logout(
        self,
        user_id: int,
        username: str,
        source_ip: str,
        user_agent: str,
        all_sessions: bool = False,
    ) -> None:
        """Log a logout event."""
        event_type = AuditEventType.AUTH_LOGOUT_ALL if all_sessions else AuditEventType.AUTH_LOGOUT
        self.log(
            event_type=event_type,
            outcome=AuditOutcome.SUCCESS,
            user_id=user_id,
            username=username,
            source_ip=source_ip,
            user_agent=user_agent,
            details={"all_sessions": all_sessions},
        )

    def log_mfa_event(
        self,
        event_type: AuditEventType,
        outcome: AuditOutcome,
        user_id: int,
        username: str,
        source_ip: str,
        user_agent: str,
        error_code: str | None = None,
    ) -> None:
        """Log an MFA-related event."""
        self.log(
            event_type=event_type,
            outcome=outcome,
            user_id=user_id,
            username=username,
            source_ip=source_ip,
            user_agent=user_agent,
            error_code=error_code,
        )

    def log_password_change(
        self,
        user_id: int,
        username: str,
        source_ip: str,
        user_agent: str,
        forced: bool = False,
    ) -> None:
        """Log a password change event."""
        self.log(
            event_type=AuditEventType.AUTH_PASSWORD_CHANGE,
            outcome=AuditOutcome.SUCCESS,
            user_id=user_id,
            username=username,
            source_ip=source_ip,
            user_agent=user_agent,
            details={"forced": forced},
        )

    def log_account_lockout(
        self,
        username: str,
        source_ip: str,
        user_agent: str,
        lockout_duration_minutes: int,
        failed_attempts: int,
    ) -> None:
        """Log an account lockout event."""
        self.log(
            event_type=AuditEventType.AUTH_ACCOUNT_LOCKOUT,
            outcome=AuditOutcome.SUCCESS,
            username=username,
            source_ip=source_ip,
            user_agent=user_agent,
            details={
                "lockout_duration_minutes": lockout_duration_minutes,
                "failed_attempts": failed_attempts,
            },
        )

    def log_token_refresh(
        self,
        user_id: int,
        username: str,
        source_ip: str,
        user_agent: str,
    ) -> None:
        """Log a token refresh event."""
        self.log(
            event_type=AuditEventType.AUTH_TOKEN_REFRESH,
            outcome=AuditOutcome.SUCCESS,
            user_id=user_id,
            username=username,
            source_ip=source_ip,
            user_agent=user_agent,
        )

    def log_admin_action(
        self,
        event_type: AuditEventType,
        admin_user_id: int,
        admin_username: str,
        source_ip: str,
        user_agent: str,
        target_user_id: int | None = None,
        details: dict | None = None,
    ) -> None:
        """Log an administrative action."""
        event_details = details or {}
        if target_user_id:
            event_details["target_user_id"] = target_user_id

        self.log(
            event_type=event_type,
            outcome=AuditOutcome.SUCCESS,
            user_id=admin_user_id,
            username=admin_username,
            source_ip=source_ip,
            user_agent=user_agent,
            details=event_details,
        )

    def log_banner_acknowledged(
        self,
        user_id: int,
        username: str,
        source_ip: str,
        user_agent: str,
    ) -> None:
        """Log login banner acknowledgment."""
        self.log(
            event_type=AuditEventType.AUTH_BANNER_ACKNOWLEDGED,
            outcome=AuditOutcome.SUCCESS,
            user_id=user_id,
            username=username,
            source_ip=source_ip,
            user_agent=user_agent,
        )


# Global audit logger instance
audit_logger = AuditLogger()


def request_org_id(request: Any) -> int | None:
    """Return the request's resolved LOCAL org id for audit stamping, or None.

    ``request.state.org_id`` transiently holds the provider's RAW external org
    string (stashed by ``get_current_user`` for access logging) until
    ``resolve_org_context`` refines it to our integer ``Organization.id``. Only
    the refined integer is a valid audit stamp — a raw string means org context
    was never confirmed against the membership mirror, so it is treated as
    no-context (None) rather than trusted.
    """
    value = getattr(getattr(request, "state", None), "org_id", None)
    return value if isinstance(value, int) else None


def _build_audit_opensearch_client():
    """Construct an OpenSearch client for audit-log reads (None on failure)."""
    try:
        from opensearchpy import OpenSearch

        return OpenSearch(
            hosts=[
                {
                    "host": settings.OPENSEARCH_HOST,
                    "port": int(settings.OPENSEARCH_PORT),
                }
            ],
            http_auth=(settings.OPENSEARCH_USER, settings.OPENSEARCH_PASSWORD),
            use_ssl=settings.OPENSEARCH_USE_TLS,
            verify_certs=settings.OPENSEARCH_VERIFY_CERTS,
            ssl_show_warn=False,
        )
    except Exception as e:  # noqa: BLE001 — OpenSearch optional
        logging.getLogger("audit").warning(f"Failed to build audit OpenSearch client: {e}")
        return None


def build_org_scope_clause(scope_org_id: int, scope_user_ids: list[int] | None) -> dict[str, Any]:
    """Build the org-admin visibility clause for the audit-log query.

    Visible to an admin of ``scope_org_id``:
      * events STAMPED with the org (``organization_id == scope_org_id``) —
        including events with ``user_id`` NULL (e.g. failed logins against an
        org surface), which the member-id filter alone could never show; and
      * **legacy-window** events: events written before org stamping existed
        (or by writers with no tenant context, e.g. local login) carry no
        ``organization_id`` field. Those are attributed via the org's member
        user-ids instead. This preserves pre-v372 visibility, with a known
        limitation that shrinks as stamping coverage grows: a SHARED member's
        un-stamped activity from another context is visible to every org that
        member belongs to (exactly the pre-v372 behavior). An event stamped
        with a DIFFERENT org is never visible here — the must_not/exists split
        is exhaustive.

    Extracted so tests can pin the exact filter body without a cluster.
    """
    legacy_branch: dict[str, Any] = {
        "bool": {"must_not": [{"exists": {"field": "organization_id"}}]}
    }
    if scope_user_ids is not None:
        legacy_branch["bool"]["filter"] = [{"terms": {"user_id": scope_user_ids}}]
    return {
        "bool": {
            "should": [
                {"term": {"organization_id": scope_org_id}},
                legacy_branch,
            ],
            "minimum_should_match": 1,
        }
    }


def query_audit_logs(
    *,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    event_type: str | None = None,
    user_id: int | None = None,
    outcome: str | None = None,
    scope_user_ids: list[int] | None = None,
    scope_org_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Query the OpenSearch audit-log indices with optional scoping.

    Shared by the super-admin (global) and org-admin (member-scoped) read
    endpoints so the query logic lives in exactly one place.

    Args:
        start_date / end_date: timestamp range filters.
        event_type / user_id / outcome: equality filters.
        scope_user_ids: when provided (without ``scope_org_id``), restrict
            results to events whose ``user_id`` is in this set. An **empty**
            list returns no results (a removed/empty org sees nothing) rather
            than everything.
        scope_org_id: org-admin tenant scope (issue #262a). When provided, the
            visibility rule becomes: events stamped ``organization_id ==
            scope_org_id`` (including user_id-NULL events such as failed
            logins) OR legacy events with NO org stamp whose ``user_id`` is in
            ``scope_user_ids`` — see :func:`build_org_scope_clause` for the
            legacy-window semantics. Events stamped with another org are never
            returned.
        limit / offset: pagination.

    Returns:
        ``{"logs": [...], "total": int, "offset": int, "limit": int}`` or
        ``{"error": str, "logs": [], "total": 0}`` when audit-to-OpenSearch is
        disabled or the query fails.
    """
    if not settings.AUDIT_LOG_TO_OPENSEARCH:
        return {
            "error": "Audit log querying requires AUDIT_LOG_TO_OPENSEARCH=true",
            "logs": [],
            "total": 0,
        }

    if scope_org_id is None and scope_user_ids is not None and len(scope_user_ids) == 0:
        # Member-only scope with no members — nothing is visible. (With an org
        # scope, org-stamped events remain visible even for an empty member set.)
        return {"logs": [], "total": 0, "offset": offset, "limit": limit}

    client = _build_audit_opensearch_client()
    if client is None:
        return {
            "error": "An internal error occurred while querying audit logs.",
            "logs": [],
            "total": 0,
        }

    try:
        must_clauses: list[dict] = []
        if start_date:
            must_clauses.append({"range": {"timestamp": {"gte": start_date.isoformat()}}})
        if end_date:
            must_clauses.append({"range": {"timestamp": {"lte": end_date.isoformat()}}})
        if event_type:
            must_clauses.append({"term": {"event_type": event_type}})
        if user_id is not None:
            must_clauses.append({"term": {"user_id": user_id}})
        if outcome:
            must_clauses.append({"term": {"outcome": outcome}})
        if scope_org_id is not None:
            # Org-admin scope: org-stamped events OR legacy member-attributed.
            must_clauses.append(build_org_scope_clause(scope_org_id, scope_user_ids))
        elif scope_user_ids is not None:
            # Member-only scope (pre-org callers): events acted by the members.
            must_clauses.append({"terms": {"user_id": scope_user_ids}})

        query = {
            "query": {"bool": {"must": must_clauses}} if must_clauses else {"match_all": {}},
            "sort": [{"timestamp": {"order": "desc"}}],
            "from": offset,
            "size": limit,
        }

        response = client.search(index="audit-logs-*", body=query)
        logs = [hit["_source"] for hit in response["hits"]["hits"]]
        total = response["hits"]["total"]["value"]
        return {"logs": logs, "total": total, "offset": offset, "limit": limit}
    except Exception as e:  # noqa: BLE001
        logging.getLogger("audit").error(f"Error querying audit logs: {e}")
        return {
            "error": "An internal error occurred while querying audit logs.",
            "logs": [],
            "total": 0,
        }
