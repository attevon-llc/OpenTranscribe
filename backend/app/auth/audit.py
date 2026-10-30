"""
Structured Audit Logging Service for FedRAMP AU-2/AU-3 Compliance.

This module provides JSON-structured audit logging with optional OpenSearch integration.
Supports logging of authentication events, security events, and administrative actions.
"""

import json
import logging
import os
import uuid
from datetime import UTC
from datetime import datetime
from typing import Any

from app.auth.audit_events import AuditEventType
from app.auth.audit_events import AuditOutcome
from app.core.config import settings

# Re-exported for the existing `from app.auth.audit import request_id_var` callers.
# This is now the SAME object the middleware sets — it used to be a second
# ContextVar that merely shared the display name "request_id", so it was always
# empty here and every audit event invented a new uuid4 (see
# app/core/request_context.py).
from app.core.request_context import request_id_var

#: Writer-client limits. Indexing runs synchronously inside the request that produced
#: the event, so a slow OpenSearch must cost the request at most this long, once —
#: not the library's 10 s default multiplied by its retries (issue #997). A write
#: that misses the deadline goes to the fallback file like any other failure.
_AUDIT_WRITE_TIMEOUT_SECONDS = 2


def _audit_index_name() -> str:
    """The monthly index the current event belongs in."""
    return f"audit-logs-{datetime.now(UTC).strftime('%Y.%m')}"


#: Fields added for content governance (#1277). A pre-existing index gets them through
#: ``put_mapping``; a new month's index is created with every property below.
_TYPED_FIELDS = ("resource_type", "resource_uuid", "tenant_scope")

_AUDIT_PROPERTIES: dict[str, Any] = {
    "timestamp": {"type": "date"},
    "event_type": {"type": "keyword"},
    "outcome": {"type": "keyword"},
    "user_id": {"type": "integer"},
    "target_user_id": {"type": "integer"},
    "organization_id": {"type": "integer"},
    "username": {"type": "keyword"},
    "source_ip": {"type": "ip"},
    "user_agent": {"type": "text"},
    "request_id": {"type": "keyword"},
    "error_code": {"type": "keyword"},
    "resource_type": {"type": "keyword"},
    "resource_uuid": {"type": "keyword"},
    "tenant_scope": {"type": "keyword"},
    "details": {"type": "object", "enabled": True},
}


class AuditLogger:
    """
    Structured audit logging service.

    Provides JSON-formatted audit logs compliant with FedRAMP AU-2/AU-3 requirements.
    """

    def __init__(self):
        """Initialize the audit logger."""
        self._logger = logging.getLogger("audit")
        self._opensearch_client = None
        # Indices this process has already seen exist (or created). Keyed by the
        # monthly index name, so a new month is checked exactly once.
        self._known_indices: set[str] = set()

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

                from app.core.opensearch_auth import opensearch_connection_kwargs

                self._opensearch_client = OpenSearch(
                    **opensearch_connection_kwargs(
                        timeout=_AUDIT_WRITE_TIMEOUT_SECONDS,
                        max_retries=0,
                        retry_on_timeout=False,
                    )
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

        index_name = _audit_index_name()
        try:
            self._ensure_index(client, index_name)
            client.index(index=index_name, body=event)
        except Exception as e:
            # Forget the index: if it was deleted underneath us, the next write
            # re-creates it with the proper mapping instead of trusting the cache.
            self._known_indices.discard(index_name)
            self._logger.warning(f"Failed to index audit event to OpenSearch: {e}")
            # Use fallback logging if enabled
            if settings.AUDIT_LOG_FALLBACK_ENABLED:
                self._logger.warning("Using fallback file logging for audit event")
                self._write_fallback_log(event)

    def _ensure_index(self, client: Any, index_name: str) -> None:
        """Create *index_name* with the audit mapping unless this process knows it exists.

        Checked once per index per process rather than before every document: the
        HEAD round trip it replaces was the dominant cost of an audited request.
        No lock around the round trip: while OpenSearch is slow, a lock would queue
        every request thread behind one another's timeout. Two threads racing on a
        brand-new month at worst both check, which is harmless.
        """
        if index_name in self._known_indices:
            return
        if not client.indices.exists(index=index_name):
            client.indices.create(
                index=index_name,
                body={
                    "mappings": {"properties": _AUDIT_PROPERTIES},
                    "settings": {
                        "number_of_shards": 1,
                        "number_of_replicas": 0,
                    },
                },
            )
        else:
            self._add_typed_fields(client, index_name)
        self._known_indices.add(index_name)

    def _add_typed_fields(self, client: Any, index_name: str) -> None:
        """Add the #1277 keyword fields to an index created before they existed.

        Once per index per process. If dynamic mapping already typed one of them (an event
        carrying it was indexed before this ran), OpenSearch refuses the change: that is
        logged and the index keeps working, because the next month's index is created with
        the right mapping.
        """
        try:
            client.indices.put_mapping(
                index=index_name,
                body={"properties": {k: _AUDIT_PROPERTIES[k] for k in _TYPED_FIELDS}},
            )
        except Exception as e:
            self._logger.warning(
                f"Could not add typed content-audit fields to {index_name}: {e}. "
                "Filtering by them is unreliable in this index until next month's."
            )

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
        target_user_id: int | None = None,
        target_username: str | None = None,
        resource_type: str | None = None,
        resource_uuid: str | None = None,
        tenant_scope: str | None = None,
    ) -> None:
        """
        Log an audit event.

        ``user_id`` is the ACTOR — who performed the action. ``target_user_id``
        is who it was performed ON. For a self-service action they are the same
        person and only ``user_id`` need be set; for an administrative action
        they differ and BOTH are required (issue #443).

        Why this is not a convention but a field: ``user_id`` is not a display
        value. ``query_audit_logs`` filters on it, and ``build_org_scope_clause``
        attributes un-stamped events to an org by member user-id — so whether an
        emitter keyed it on the actor or the subject changed **who could see the
        record**. Five emitters of ``auth.account.disabled`` disagreed three
        ways (actor, subject, NULL), which meant "actions Bob performed" returned
        Bob's own privilege escalation while "what the admin did" missed every
        IdP-driven promotion.

        The `details` dict was the previous workaround and it failed: the target
        appears as ``details.target_user`` (a UUID string) in one place,
        ``details.target_user_id`` (an int) in another, ``details.target_email``
        in a third. ``details`` is a dynamic object mapping, so no single query
        can answer "everything done TO user X" — which is the field's whole
        purpose. These are top-level and typed instead.

        Back-compatible: pre-existing documents simply lack the keys, and the
        fields are omitted from the event entirely when unset rather than written
        as null, so the OpenSearch mapping does not gain sparse nulls.

        Args:
            event_type: The type of audit event
            outcome: The outcome of the event (success/failure/partial)
            user_id: The ACTOR's database ID (if known)
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
            target_user_id: The database ID of the user the action was performed
                ON, when that differs from the actor. Omitted for self-service
                actions.
            target_username: The target's username/email, for the same reason
                ``username`` exists alongside ``user_id`` — it survives the
                account being deleted, which is exactly when an administrative
                audit record matters most.
            resource_type: What the event is about (``media_file``, ``collection``,
                ``conversation``, ``content_hold``), as a top-level keyword so "everything
                that happened to this file" is one filter (issue #1277). Omitted when unset.
            resource_uuid: The resource's uuid, never its name or title.
            tenant_scope: ``org`` or ``personal``: whose tenant the RESOURCE belongs to,
                regardless of who acted. ``build_org_scope_clause`` refuses to attribute a
                ``personal`` event to an organization through its actor's membership (issue
                #1277): a member's own-file view must not be readable by every
                admin of every organization the member belongs to.
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

        # Omitted rather than written as null when unset: `details` is a dynamic
        # mapping but these are top-level, and a sparse null on every
        # self-service event would bloat the index for no query value.
        if target_user_id is not None:
            event["target_user_id"] = target_user_id
        if target_username is not None:
            event["target_username"] = target_username
        if resource_type is not None:
            event["resource_type"] = resource_type
        if resource_uuid is not None:
            event["resource_uuid"] = resource_uuid
        if tenant_scope is not None:
            event["tenant_scope"] = tenant_scope

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
        details: dict[str, Any] | None = None,
    ) -> None:
        """Log a successful login.

        Args:
            details: Extra method-specific diagnostics merged alongside
                ``auth_method`` — e.g. OIDC's ``claim_keys`` (P1.2), so an admin
                can see what an IdP actually sent without this becoming a second
                place claim *values* are logged. ``auth_method`` always wins the
                key if a caller's dict happens to carry one.
        """
        self.log(
            event_type=AuditEventType.AUTH_LOGIN_SUCCESS,
            outcome=AuditOutcome.SUCCESS,
            user_id=user_id,
            username=username,
            source_ip=source_ip,
            user_agent=user_agent,
            details={**(details or {}), "auth_method": auth_method},
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
        """Log an account lockout event.

        This is a SYSTEM-initiated event (the progressive lockout threshold was
        crossed by a login attempt, not by an administrator), so there is no
        actor: ``user_id``/``username`` stay unset and the locked account is the
        TARGET (issue #443's contract — the subject always goes in
        ``target_user_id``/``target_username``, never in the actor fields, even
        when there is no human actor to distinguish it from). This was
        previously the exact pre-#443 shape: the locked account named in
        ``username`` with no target at all, invisible to "everything done TO
        this identifier" queries.
        """
        self.log(
            event_type=AuditEventType.AUTH_ACCOUNT_LOCKOUT,
            outcome=AuditOutcome.SUCCESS,
            source_ip=source_ip,
            user_agent=user_agent,
            target_username=username,
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
