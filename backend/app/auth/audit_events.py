"""Audit event vocabulary: event types, outcomes and tenant-scope values.

The member names and values are the audit index's wire format (``event_type`` / ``outcome`` /
``tenant_scope`` keywords already stored in every monthly index and matched by SIEM rules):
never rename or re-value one. The writer is ``auth/audit.py``; the read path is
``auth/audit_query.py``.
"""

from enum import StrEnum

#: ``tenant_scope`` values: whose tenant the RESOURCE belongs to, whatever the actor's role.
TENANT_SCOPE_ORG = "org"
TENANT_SCOPE_PERSONAL = "personal"


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
    #: Admin clearing the progressive lockout counter (issue #570) — distinct from
    #: AUTH_ACCOUNT_UNLOCK, which also reactivates a disabled account. See
    #: `api/endpoints/admin_accounts.py::admin_reset_lockout_counter`.
    AUTH_LOCKOUT_COUNTER_RESET = "auth.lockout.counter_reset"  # noqa: S105 # nosec B105
    AUTH_ACCOUNT_DISABLED = "auth.account.disabled"
    AUTH_ACCOUNT_EXPIRED = "auth.account.expired"
    # Distinct from AUTH_ACCOUNT_EXPIRED: the AC-2 inactivity sweep declined to
    # deactivate a super_admin candidate (it would have zeroed active
    # super_admins). No account state changed here — see
    # `services/account_lifecycle_service.py`.
    AUTH_ACCOUNT_EXPIRATION_SKIPPED = "auth.account.expiration_skipped"

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
    # Takedown notice records (issue #1254). ``details`` carries ids, the notice type and
    # counts only, never the basis or a reason.
    ADMIN_TAKEDOWN_NOTICE_CREATE = "admin.takedown_notice.create"
    ADMIN_TAKEDOWN_NOTICE_UPDATE = "admin.takedown_notice.update"
    ADMIN_TAKEDOWN_NOTICE_NOTIFY = "admin.takedown_notice.notify"
    ADMIN_TAKEDOWN_NOTICE_EXPORT = "admin.takedown_notice.export"

    # Organization content holds (issue #1276). ``details`` carries the scope, counts, the
    # collection uuid and (on a refusal) the action and who holds the file: never the reason.
    CONTENT_HOLD_PLACE = "content.hold.place"
    CONTENT_HOLD_RELEASE = "content.hold.release"
    CONTENT_HOLD_REFUSED = "content.hold.refused"
    # A transcript edit on a file that is under a legal hold. Editing stays allowed by decision
    # (a hold blocks destruction, not correction), so the edit is recorded instead of refused
    # (issue #1307). ``details`` carries ids, the kind of edit and who holds the file: never the
    # text, the speaker's name or the hold's reason.
    CONTENT_TRANSCRIPT_EDIT = "content.transcript.edit"

    # Privacy controls (issues #1280, #1279). ``org.policy.change``: an organization
    # administrator changed the organization's policy; ``details.fields`` lists the NAMES of the
    # fields changed, never values (custom words can be sensitive). The two purge events delete
    # biometric / inferred data and are written twice, ``details.phase`` ``requested`` then
    # ``completed``, the latter with counts only.
    ORG_POLICY_CHANGE = "org.policy.change"
    PRIVACY_VOICEPRINT_PURGE = "privacy.voiceprints.purge"
    PRIVACY_GENDER_PURGE = "privacy.gender.purge"
    # A voiceprint switch changed and the speaker labels in the search index are being rebuilt
    # (issue #1280). Written when the resync is requested (``details.phase`` ``requested``) and
    # when a scope finishes (``completed``, or ``incomplete`` when some files could not be
    # queued and the scope stays pending), plus ``drift`` when the maintenance scan found
    # indexed labels that disagree with the policy. ``details`` holds the switch, its from/to
    # value, the scope kind and ids, and file COUNTS: never a name or a file id.
    PRIVACY_LABEL_RESYNC = "privacy.labels.resync"

    # An audit-log export, by a platform admin or an organization admin (issue #1277):
    # ``details`` holds the format, the row count and whether it was truncated, never the rows.
    AUDIT_EXPORT = "audit.export"

    # A forced OpenSearch orphan purge that overrode the sweep's ratio guard
    ADMIN_DATA_INTEGRITY_PURGE = "admin.data_integrity.purge"

    # A storage-object orphan sweep (issue #1212): requested, and again on completion
    # with counters only
    ADMIN_DATA_INTEGRITY_STORAGE_PURGE = "admin.data_integrity.storage_purge"

    # A permanent delete refused by services/delete_permissions.py (issue #1103)
    FILE_DELETE_DENIED = "file.delete.denied"

    # Prompt sharing events
    PROMPT_SHARE = "prompt.share"
    PROMPT_UNSHARE = "prompt.unshare"
    PROMPT_CLONE = "prompt.clone"

    # RAG chat events (metadata only — message content is NEVER audited)
    CHAT_CONVERSATION_CREATE = "chat.conversation.create"
    CHAT_CONVERSATION_DELETE = "chat.conversation.delete"
    CHAT_MESSAGE_SEND = "chat.message.send"

    # Banner acknowledgment
    AUTH_BANNER_ACKNOWLEDGED = "auth.banner.acknowledged"

    # An administrator acknowledged a versioned legal document (key, version, sha256 in details).
    LEGAL_ACKNOWLEDGED = "legal.acknowledged"

    # A user saved or removed a third-party service credential of their own (issue #1204,
    # the pyannote.ai key). ``details`` names the provider and purpose only, NEVER the key.
    USER_CREDENTIAL_SET = "user.credential.set"
    USER_CREDENTIAL_DELETE = "user.credential.delete"

    # Resource sharing (collections, tags) — see details.resource_type. Distinct from
    # PROMPT_SHARE/UNSHARE above, which predate this and are prompt-specific.
    RESOURCE_SHARE = "resource.share"
    RESOURCE_UNSHARE = "resource.unshare"

    # An organization collection was restricted to, or opened up from, its named people
    # (issue #1275). ``details`` carries the new mode and counts, never the collection name.
    COLLECTION_VISIBILITY_CHANGE = "collection.visibility.change"
    # Files added to or removed from a collection. ``details`` carries counts and at most 100
    # file uuids, never a name.
    COLLECTION_MEMBERSHIP_CHANGE = "collection.membership.change"
    # Content access (issue #1277). High volume, so the three below are recorded only when
    # ``services/content_audit.content_audit_enabled`` says so; governance events
    # (collection visibility and membership, legal holds) are always recorded. ``resource_type`` / ``resource_uuid`` / ``tenant_scope``
    # are top-level typed fields (see ``AuditLogger.log``). NEVER in these events: transcript
    # text, filenames, titles, collection names or hold reasons.
    CONTENT_FILE_VIEW = "content.file.view"
    #: ``details.kind`` = ``stream_url`` | ``download_prepare`` | ``download_stream``
    CONTENT_MEDIA_ACCESS = "content.media.access"
    #: ``details.format``, ``details.surface`` (``transcript`` | ``subtitles`` | ``summary`` |
    #: ``bulk`` | ``chat``) and ``details.file_count``
    CONTENT_EXPORT = "content.export"

    # A transcript was revealed past the owner's redaction policy (issue #85), on any read or
    # export surface (services/redaction/reveal_audit.py); ``details`` names the categories.
    TRANSCRIPT_VIEW_UNREDACTED = "transcript.view_unredacted"

    # Group membership (issue #443)
    GROUP_MEMBER_ADD = "group.member.add"
    GROUP_MEMBER_REMOVE = "group.member.remove"
    GROUP_MEMBER_ROLE_CHANGE = "group.member.role_change"

    # Platform-admin reach into tenant data (issue #1122). Content access is the forced
    # TENANCY_MODE=single escape hatch crossing a tenant; metadata access is a platform
    # route (retention preview, quarantine list) returning other tenants' filenames.
    PLATFORM_ADMIN_CONTENT_ACCESS = "platform_admin.content.access"
    PLATFORM_ADMIN_METADATA_ACCESS = "platform_admin.metadata.access"

    # Support-access grants (issue #1122). ``user_id`` is the actor, ``organization_id`` the
    # target tenant and ``target_user_id`` the subject user (personal workspace) or the
    # grantee (decisions). Details carry ids, levels and the route, never content.
    SUPPORT_ACCESS_REQUESTED = "support_access.requested"
    SUPPORT_ACCESS_APPROVED = "support_access.approved"
    SUPPORT_ACCESS_DENIED = "support_access.denied"
    SUPPORT_ACCESS_REVOKED = "support_access.revoked"
    SUPPORT_ACCESS_BREAK_GLASS = "support_access.break_glass"
    SUPPORT_ACCESS_USED = "support_access.used"


class AuditOutcome(StrEnum):
    """Audit event outcomes."""

    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"
