"""The compliance-audited route set: which route must write which audit event.

Read by two tests:

* ``unit/test_compliance_audit_route_coverage.py`` (static, fast): every route in a compliance
  family, and every route that statically reaches a compliance event, is classified here; every
  classified route still exists; and each audited route still REACHES its declared event, so a
  deleted emitter fails without a stack.
* ``integration/test_compliance_audit_rows.py`` (live OpenSearch): each route whose entry names a
  ``live`` case is driven for real and its event is read back through the org/platform audit
  query, with a non-empty actor and the right target.

A route added to a family below with no entry fails the unit test, which is the point: the
author has to say what it records (and the live test then proves it) or why it records nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: The events this register is about. Authentication and settings events are covered by their
#: own suites (``unit/test_audit_actor_target.py``, ``unit/test_audit_event_emitters.py``).
COMPLIANCE_EVENTS: frozenset[str] = frozenset(
    {
        "ADMIN_FILE_QUARANTINE",
        "ADMIN_FILE_RELEASE",
        "ADMIN_TAKEDOWN_NOTICE_CREATE",
        "ADMIN_TAKEDOWN_NOTICE_UPDATE",
        "ADMIN_TAKEDOWN_NOTICE_NOTIFY",
        "ADMIN_TAKEDOWN_NOTICE_EXPORT",
        "CONTENT_HOLD_PLACE",
        "CONTENT_HOLD_RELEASE",
        "CONTENT_HOLD_REFUSED",
        "CONTENT_EXPORT",
        "ORG_POLICY_CHANGE",
        "PRIVACY_VOICEPRINT_PURGE",
        "PRIVACY_GENDER_PURGE",
        "AUDIT_EXPORT",
        "ADMIN_DATA_INTEGRITY_PURGE",
        "ADMIN_DATA_INTEGRITY_STORAGE_PURGE",
        "FILE_DELETE_DENIED",
        "ADMIN_USER_DELETE",
        "SUPPORT_ACCESS_REQUESTED",
        "SUPPORT_ACCESS_APPROVED",
        "SUPPORT_ACCESS_DENIED",
        "SUPPORT_ACCESS_REVOKED",
        "SUPPORT_ACCESS_BREAK_GLASS",
        "SUPPORT_ACCESS_USED",
    }
)

#: Path shapes that ARE compliance surfaces. Any route matching one must be classified.
COMPLIANCE_PATH_FAMILIES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"^/api/(admin|org-admin)/files/\{file_uuid\}/(quarantine|release)$",
        r"^/api/org-admin/holds",
        r"/gdpr/",
        r"^/api/privacy/me/purge$",
        r"^/api/admin/privacy-policy/purge$",
        r"^/api/org-admin/policy",
        r"support-access",
        r"^/api/admin/takedown-notices",
        r"/export(/document)?$",
        r"^/api/admin/data-integrity",
    )
)


@dataclass(frozen=True)
class Audited:
    """A route that must write ``events``; ``live`` names its case in the integration test."""

    events: tuple[str, ...]
    #: The live case id(s) in the integration test; several when one route writes several events.
    live: str | tuple[str, ...] | None = None
    #: When ``live`` is None: the suite(s) that pin the emit site instead. The unit test checks
    #: that together they NAME the route and every event; a file merely existing is not evidence.
    pinned_by: str | tuple[str, ...] | None = None
    #: Neither driven live nor pinned anywhere yet: deferred work, printed on every run.
    backlog: str | None = None

    @property
    def live_cases(self) -> tuple[str, ...]:
        return (self.live,) if isinstance(self.live, str) else (self.live or ())

    @property
    def pinned_files(self) -> tuple[str, ...]:
        return (self.pinned_by,) if isinstance(self.pinned_by, str) else (self.pinned_by or ())


A = Audited
_NO_DRIVER = (
    "no test drives this route's hold refusal and reads the event: force-delete needs a platform "
    "admin acting through a write grant, and member removal a collection-scoped hold"
)
AUDITED_ROUTES: dict[tuple[str, str], Audited] = {
    # Takedown and its platform legal hold. The quarantine/release payload is being reshaped on
    # another branch; the live case asserts actor + target + event name only.
    ("POST", "/api/admin/files/{file_uuid}/quarantine"): A(
        ("ADMIN_FILE_QUARANTINE",), "quarantine"
    ),
    ("POST", "/api/admin/files/{file_uuid}/release"): A(("ADMIN_FILE_RELEASE",), "release"),
    ("POST", "/api/org-admin/files/{file_uuid}/quarantine"): A(
        ("ADMIN_FILE_QUARANTINE",), "org-quarantine"
    ),
    ("POST", "/api/org-admin/files/{file_uuid}/release"): A(("ADMIN_FILE_RELEASE",), "org-release"),
    # Organization legal holds.
    ("POST", "/api/org-admin/holds"): A(("CONTENT_HOLD_PLACE",), "hold-place"),
    ("POST", "/api/org-admin/holds/{hold_uuid}/release"): A(
        ("CONTENT_HOLD_RELEASE",), "hold-release"
    ),
    # Exports (content.export is opt-in; the live cases switch content auditing on).
    ("GET", "/api/files/{file_uuid}/export"): A(("CONTENT_EXPORT",), "transcript-export"),
    ("GET", "/api/files/{file_uuid}/subtitles"): A(("CONTENT_EXPORT",), "subtitles-export"),
    ("POST", "/api/files/{file_uuid}/export/document"): A(("CONTENT_EXPORT",), "document-export"),
    ("GET", "/api/files/{file_uuid}/summary/export"): A(
        ("CONTENT_EXPORT",), pinned_by="api/test_content_audit_events.py"
    ),
    ("GET", "/api/chat/conversations/{conversation_uuid}/export"): A(
        ("CONTENT_EXPORT",), pinned_by="api/test_content_audit_events.py"
    ),
    ("POST", "/api/files/bulk-export/prepare"): A(
        ("CONTENT_EXPORT",), pinned_by="api/test_content_audit_events.py"
    ),
    # Privacy purges.
    ("POST", "/api/privacy/me/purge"): A(("PRIVACY_VOICEPRINT_PURGE",), "privacy-purge"),
    ("POST", "/api/org-admin/policy/purge"): A(
        ("PRIVACY_VOICEPRINT_PURGE",), pinned_by="api/test_privacy_purge.py"
    ),
    ("POST", "/api/admin/privacy-policy/purge"): A(
        ("PRIVACY_VOICEPRINT_PURGE",), pinned_by="api/test_privacy_purge.py"
    ),
    ("PATCH", "/api/org-admin/policy"): A(
        ("ORG_POLICY_CHANGE",), pinned_by="api/test_org_admin_policy.py"
    ),
    # Support access lifecycle (the per-request USE is a dependency, see DEPENDENCY_EMITTERS).
    ("POST", "/api/support-access/grants"): A(("SUPPORT_ACCESS_REQUESTED",), "grant-request"),
    ("POST", "/api/org-admin/support-access/{grant_uuid}/approve"): A(
        ("SUPPORT_ACCESS_APPROVED",), "grant-approve"
    ),
    ("POST", "/api/org-admin/support-access/{grant_uuid}/deny"): A(
        ("SUPPORT_ACCESS_DENIED",), "grant-deny"
    ),
    ("POST", "/api/users/me/support-access/{grant_uuid}/approve"): A(
        ("SUPPORT_ACCESS_APPROVED",), "personal-approve"
    ),
    ("POST", "/api/users/me/support-access/{grant_uuid}/deny"): A(
        ("SUPPORT_ACCESS_DENIED",), "personal-deny"
    ),
    ("POST", "/api/support-access/grants/{grant_uuid}/revoke"): A(
        ("SUPPORT_ACCESS_REVOKED",), "grant-revoke"
    ),
    ("POST", "/api/support-access/grants/break-glass"): A(
        ("SUPPORT_ACCESS_BREAK_GLASS",), "break-glass"
    ),
    # Erasure.
    ("POST", "/api/admin/gdpr/erase-user/{user_uuid}"): A(("ADMIN_USER_DELETE",), "gdpr-erase"),
    ("POST", "/api/org-admin/gdpr/erase-user/{user_uuid}"): A(
        ("ADMIN_USER_DELETE",), "org-member-erase"
    ),
    ("POST", "/api/org-admin/gdpr/erase-organization"): A(("ADMIN_USER_DELETE",), "org-erase"),
    # Single-recording erasure, the account holder's own request (and its withdrawal), the
    # receipt reads, and the per-file voiceprint opt-out whose purge is a compliance event.
    ("POST", "/api/admin/gdpr/erase-file/{file_uuid}"): A(
        ("ADMIN_USER_DELETE",), pinned_by="api/test_erasure_receipts_api.py"
    ),
    ("POST", "/api/org-admin/gdpr/erase-file/{file_uuid}"): A(
        ("ADMIN_USER_DELETE",), pinned_by="api/test_erasure_receipts_api.py"
    ),
    ("POST", "/api/users/me/erasure-request"): A(
        ("ADMIN_USER_DELETE",), pinned_by="api/test_self_erasure_api.py"
    ),
    ("DELETE", "/api/users/me/erasure-request/{ledger_uuid}"): A(
        ("ADMIN_USER_DELETE",), pinned_by="api/test_self_erasure_api.py"
    ),
    ("GET", "/api/admin/gdpr/erasures/{ledger_uuid}/receipts"): A(
        ("GDPR_RECEIPT_VIEW",), pinned_by="api/test_erasure_receipts_api.py"
    ),
    ("GET", "/api/org-admin/gdpr/erasures/{ledger_uuid}/receipts"): A(
        ("GDPR_RECEIPT_VIEW",), pinned_by="api/test_erasure_receipts_api.py"
    ),
    ("PUT", "/api/admin/gdpr/erasure-receipt/ignored-indices"): A(
        ("ADMIN_SETTINGS_CHANGE",), pinned_by="api/test_erasure_receipts_api.py"
    ),
    ("PATCH", "/api/files/{file_uuid}/voiceprint-consent"): A(
        ("PRIVACY_VOICEPRINT_PURGE",), pinned_by="api/test_file_voiceprint_consent.py"
    ),
    ("DELETE", "/api/admin/users/{user_uuid}"): A(("ADMIN_USER_DELETE",), "admin-user-delete"),
    ("DELETE", "/api/users/{user_uuid}"): A(("ADMIN_USER_DELETE",), "users-delete"),
    ("DELETE", "/api/files/{file_uuid}"): A(
        ("FILE_DELETE_DENIED", "CONTENT_HOLD_REFUSED"),
        ("file-delete-denied", "hold-refused-delete"),
    ),
    # Destroy paths that refuse a held file or collection (``content.hold.refused``).
    ("DELETE", "/api/files/{file_uuid}/force"): A(("CONTENT_HOLD_REFUSED",), backlog=_NO_DRIVER),
    ("POST", "/api/files/management/bulk-action"): A(
        ("CONTENT_HOLD_REFUSED",), "hold-refused-bulk"
    ),
    ("POST", "/api/files/{file_uuid}/reprocess"): A(
        ("CONTENT_HOLD_REFUSED",), "hold-refused-reprocess"
    ),
    ("POST", "/api/files/{file_uuid}/retry"): A(("CONTENT_HOLD_REFUSED",), "hold-refused-retry"),
    ("DELETE", "/api/files/{file_uuid}/summary"): A(
        ("CONTENT_HOLD_REFUSED",), pinned_by="api/test_hold_guards_on_derived_content.py"
    ),
    ("DELETE", "/api/speakers/{speaker_uuid}"): A(
        ("CONTENT_HOLD_REFUSED",), pinned_by="api/test_hold_guards_on_derived_content.py"
    ),
    ("DELETE", "/api/collections/{collection_uuid}"): A(
        ("CONTENT_HOLD_REFUSED",), pinned_by="api/test_org_admin_holds.py"
    ),
    ("DELETE", "/api/collections/{collection_uuid}/media"): A(
        ("CONTENT_HOLD_REFUSED",), backlog=_NO_DRIVER
    ),
    # Resolves the grant itself instead of through get_current_context.
    ("GET", "/api/files/{file_uuid}/thumbnail"): A(("SUPPORT_ACCESS_USED",), "thumbnail-use"),
    # Audit-log exports.
    ("GET", "/api/admin/audit-logs/export"): A(("AUDIT_EXPORT",), "audit-export"),
    ("GET", "/api/org-admin/audit-logs/export"): A(
        ("AUDIT_EXPORT",), pinned_by="api/test_org_audit_view.py"
    ),
    # Takedown notice records.
    ("POST", "/api/admin/takedown-notices"): A(
        ("ADMIN_TAKEDOWN_NOTICE_CREATE",), pinned_by="api/endpoints/test_takedown_notices_routes.py"
    ),
    ("PATCH", "/api/admin/takedown-notices/{notice_uuid}/files/{file_uuid}"): A(
        ("ADMIN_TAKEDOWN_NOTICE_UPDATE",), "notice-update"
    ),
    ("POST", "/api/admin/takedown-notices/{notice_uuid}/notify"): A(
        ("ADMIN_TAKEDOWN_NOTICE_NOTIFY",), "notice-notify"
    ),
    ("GET", "/api/admin/takedown-notices/export"): A(
        ("ADMIN_TAKEDOWN_NOTICE_EXPORT",), "notice-export"
    ),
    # Orphan purges.
    ("POST", "/api/admin/data-integrity"): A(
        ("ADMIN_DATA_INTEGRITY_PURGE",), "data-integrity-purge"
    ),
    ("POST", "/api/admin/data-integrity/storage"): A(
        ("ADMIN_DATA_INTEGRITY_STORAGE_PURGE",),
        pinned_by="api/endpoints/test_admin_storage_integrity_routes.py",
    ),
}

#: Compliance-family routes that deliberately write none of the events above, with the reason.
_READ = "a read: lists, fetches and status of compliance records write no compliance event"
NOT_AUDITED: dict[tuple[str, str], str] = {
    ("GET", "/api/admin/data-integrity/counts"): _READ,
    ("GET", "/api/admin/data-integrity/status"): _READ,
    ("GET", "/api/admin/data-integrity/storage/counts"): _READ,
    ("GET", "/api/admin/data-integrity/storage/status"): _READ,
    ("GET", "/api/admin/data-integrity/storage/keys"): _READ,
    ("GET", "/api/admin/gdpr/erasures"): _READ,
    ("GET", "/api/admin/gdpr/erasure-receipt/ignored-indices"): _READ,
    ("GET", "/api/admin/takedown-notices"): _READ,
    ("GET", "/api/admin/takedown-notices/{notice_uuid}"): _READ,
    ("GET", "/api/org-admin/holds"): _READ,
    ("GET", "/api/org-admin/holds/{hold_uuid}"): _READ,
    ("GET", "/api/org-admin/policy"): _READ,
    ("GET", "/api/org-admin/support-access"): _READ,
    ("GET", "/api/org-admin/support-access/{grant_uuid}/uses"): _READ,
    ("GET", "/api/support-access/grants"): _READ,
    ("GET", "/api/support-access/grants/{grant_uuid}"): _READ,
    ("GET", "/api/support-access/grants/{grant_uuid}/uses"): _READ,
    ("GET", "/api/support-access/targets/organizations"): _READ,
    ("GET", "/api/users/me/support-access"): _READ,
    ("GET", "/api/users/me/support-access/{grant_uuid}/uses"): _READ,
    ("GET", "/api/privacy/purge/{task_id}"): (
        "a purge status read; it NAMES the purge's event types for the caller, emits none"
    ),
    ("GET", "/api/custom-vocabulary/export"): "the caller's own vocabulary list, not content",
}

#: Events written by a dependency rather than a route, keyed by the dependency's dotted path.
DEPENDENCY_EMITTERS: dict[str, tuple[str, ...]] = {
    "app.api.deps_context.get_current_context": ("SUPPORT_ACCESS_USED",),
}

#: Events no request writes, with where they come from instead.
NON_ROUTE_EVENTS: dict[str, str] = {
    "PRIVACY_GENDER_PURGE": "same routes as the voiceprint purge, chosen by the body's targets",
}
