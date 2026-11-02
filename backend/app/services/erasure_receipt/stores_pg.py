"""The Postgres stores of the erasure inventory.

Each entry is a ``SELECT count(*)`` over one table with a per-scope ``WHERE``. The table names are
constants in this file, never caller input, and every value is a bind parameter:

* ``:uid`` / ``:org`` -- the subject's user and organization ids,
* ``:fids`` / ``:sids`` -- the file and speaker ids captured BEFORE the destroy,
* ``:held`` -- the captured files a legal hold keeps.

A table that is not listed here but is reachable from a subject by a foreign key is either removed
by ``ON DELETE CASCADE`` from a listed parent or is a compliance record. That claim is not made in
prose: ``EXEMPT_TABLES`` states it per table, and
``tests/unit/test_erasure_inventory_coverage.py`` derives the foreign-key closure from the live
schema and fails on any table that is in neither place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Disposition = Literal["deleted", "de_attributed", "retained", "ttl"]

_ANY_F = "CAST(:fids AS integer[])"
_ORG_FILES = "SELECT id FROM media_file WHERE organization_id = :org"


@dataclass(frozen=True)
class PgStore:
    """One table counted per scope kind. A scope kind with no clause does not apply."""

    key: str
    table: str
    where: dict[str, str]
    disposition: Disposition = "deleted"
    reason: str | None = None
    #: What survives a legal hold is retained by Art. 17(3)(e), not a residual.
    retained_when_held: bool = False
    #: Exclude the captured held files from the count (they are counted under ``.legal_hold``).
    exclude_held: bool = False


def _file_children(key: str, table: str, column: str = "media_file_id") -> PgStore:
    clause = f"{column} = ANY({_ANY_F})"
    return PgStore(
        key, table, {"user": clause, "org_member": clause, "organization": clause, "file": clause}
    )


def _owned(key: str, table: str, *, org_member: bool = True, **extra: str) -> PgStore:
    where = {"user": "user_id = :uid", "organization": "organization_id = :org", **extra}
    if org_member:
        where["org_member"] = "user_id = :uid AND organization_id = :org"
    return PgStore(key, table, where)


PG_STORES: tuple[PgStore, ...] = (
    PgStore(
        "pg.media_file",
        "media_file",
        {
            "user": "user_id = :uid",
            "org_member": "user_id = :uid AND organization_id = :org",
            "organization": "organization_id = :org",
            "file": f"id = ANY({_ANY_F})",
        },
        exclude_held=True,
    ),
    PgStore(
        "pg.media_file.legal_hold",
        "media_file",
        {
            k: "id = ANY(CAST(:held AS integer[]))"
            for k in ("user", "org_member", "organization", "file")
        },
        disposition="retained",
        reason="legal_hold",
    ),
    _file_children("pg.transcript_segment", "transcript_segment"),
    _file_children("pg.speaker", "speaker"),
    _file_children("pg.task", "task"),
    _file_children("pg.analytics", "analytics"),
    _file_children("pg.file_tag", "file_tag"),
    _file_children("pg.collection_member", "collection_member"),
    _file_children("pg.file_facts", "file_facts"),
    _file_children("pg.file_pipeline_timing", "file_pipeline_timing", "file_id"),
    _file_children("pg.topic_suggestion", "topic_suggestion"),
    # Written by the subject on somebody else's file as well as on their own.
    PgStore(
        "pg.comment",
        "comment",
        {
            "user": f"user_id = :uid OR media_file_id = ANY({_ANY_F})",
            "org_member": (
                f"media_file_id = ANY({_ANY_F}) OR "
                f"(user_id = :uid AND media_file_id IN ({_ORG_FILES}))"
            ),
            "organization": f"media_file_id = ANY({_ANY_F})",
            "file": f"media_file_id = ANY({_ANY_F})",
        },
    ),
    PgStore(
        "pg.speaker_match",
        "speaker_match",
        {
            k: "speaker1_id = ANY(CAST(:sids AS integer[])) "
            "OR speaker2_id = ANY(CAST(:sids AS integer[]))"
            for k in ("user", "org_member", "organization", "file")
        },
    ),
    PgStore(
        "pg.speaker_cluster_member",
        "speaker_cluster_member",
        {
            k: "speaker_id = ANY(CAST(:sids AS integer[]))"
            for k in ("user", "org_member", "organization", "file")
        },
    ),
    _owned("pg.speaker_profile", "speaker_profile"),
    _owned("pg.speaker_collection", "speaker_collection"),
    _owned("pg.speaker_cluster", "speaker_cluster"),
    _owned("pg.chat_conversation", "chat_conversation"),
    _owned("pg.summary_prompt", "summary_prompt"),
    _owned("pg.user_setting", "user_setting"),
    _owned("pg.custom_vocabulary", "custom_vocabulary"),
    _owned("pg.watch_source", "watch_source"),
    # A personal collection or tag goes; an organization's survives with the subject NULLed
    # (``pg.*.attributed`` counts what is still attributed to them).
    PgStore(
        "pg.collection",
        "collection",
        {"user": "user_id = :uid AND organization_id IS NULL"},
    ),
    PgStore("pg.tag", "tag", {"user": "user_id = :uid AND organization_id IS NULL"}),
    PgStore(
        "pg.collection.org",
        "collection",
        {
            "user": "user_id = :uid AND organization_id IS NOT NULL",
            "org_member": "user_id = :uid AND organization_id = :org",
        },
        disposition="de_attributed",
        reason="tenant_vocabulary",
    ),
    PgStore(
        "pg.tag.org",
        "tag",
        {
            "user": "user_id = :uid AND organization_id IS NOT NULL",
            "org_member": "user_id = :uid AND organization_id = :org",
        },
        disposition="de_attributed",
        reason="tenant_vocabulary",
    ),
    PgStore("pg.chat_project", "chat_project", {"user": "user_id = :uid"}),
    PgStore("pg.upload_batch", "upload_batch", {"user": "user_id = :uid"}),
    PgStore("pg.user_media_source", "user_media_source", {"user": "user_id = :uid"}),
    PgStore(
        "pg.organization_membership",
        "organization_membership",
        {"user": "user_id = :uid", "organization": "organization_id = :org"},
        retained_when_held=True,
    ),
    PgStore("pg.user", "user", {"user": "id = :uid"}, retained_when_held=True),
    PgStore(
        "pg.organization",
        "organization",
        {"organization": "id = :org"},
        retained_when_held=True,
    ),
    # Pseudonymised on purpose: the record of which terms were accepted outlives the account.
    PgStore(
        "pg.legal_acceptance",
        "legal_acceptance",
        {"user": "user_id = :uid"},
        disposition="de_attributed",
        reason="legal_acceptance_retention",
    ),
    PgStore(
        "pg.erasure_ledger",
        "erasure_ledger",
        {"user": "subject_user_id = :uid", "organization": "subject_organization_id = :org"},
        disposition="retained",
        reason="compliance_record",
    ),
)

PG_STORE_TABLES = frozenset(s.table for s in PG_STORES)

_CASCADE_USER = "removed by ON DELETE CASCADE with the user row (pg.user)"
_CASCADE_FILE = "removed by ON DELETE CASCADE with the media file (pg.media_file)"
_CASCADE_SPEAKER = "removed by ON DELETE CASCADE with the speaker (pg.speaker)"
_COMPLIANCE = (
    "compliance record kept by design (Art. 17(3)(b)/(e), Art. 30), keyed by surrogate ids"
)
_NOT_PERSONAL = "references the user as an acting administrator, not as the data subject"

#: Every other table the foreign-key closure from a subject reaches, with the reason it is not
#: counted on its own. The coverage test fails on a table that is in neither this table nor a
#: store, and on an entry here that no longer exists.
EXEMPT_TABLES: dict[str, str] = {
    "refresh_token": _CASCADE_USER,
    "password_history": _CASCADE_USER,
    "password_reset_token": _CASCADE_USER,
    "email_verification_token": _CASCADE_USER,
    "user_mfa": _CASCADE_USER,
    "user_asr_settings": _CASCADE_USER,
    "user_llm_settings": _CASCADE_USER,
    "user_diarization_settings": _CASCADE_USER,
    "user_group": _CASCADE_USER,
    "user_group_member": "removed by ON DELETE CASCADE with the user or the group",
    "user_invitation": _CASCADE_USER,
    "collection_share": "removed by ON DELETE CASCADE with the shared collection or the user",
    "tag_share": "removed by ON DELETE CASCADE with the shared tag or the user",
    "chat_message": "removed by ON DELETE CASCADE with the conversation (pg.chat_conversation)",
    "speaker_collection_member": "removed by ON DELETE CASCADE with the speaker collection",
    "speaker_cannot_link": _CASCADE_SPEAKER,
    "speaker_profile_blacklist": _CASCADE_SPEAKER,
    "speaker_correction_carryover": _CASCADE_SPEAKER,
    "content_hold_file": _CASCADE_FILE,
    "watch_source_file": "FK SET NULL: the import record outlives the file, detached (no content)",
    "watch_source_email": "removed by ON DELETE CASCADE with the watch source (pg.watch_source)",
    "group_mapping": "directory mapping owned by a group, not by the data subject",
    "usage_event": "FK SET NULL: usage metering counts, detached from the file and the user",
    "content_hold": _COMPLIANCE,
    "takedown_notice": _COMPLIANCE,
    "takedown_notice_file": _COMPLIANCE,
    "takedown_notification": _COMPLIANCE,
    "support_access_grant": _COMPLIANCE,
    "support_access_use": _COMPLIANCE,
    "auth_config": _NOT_PERSONAL,
    "auth_config_audit": _COMPLIANCE,
    "scim_token": _NOT_PERSONAL,
    "email_notification_config": _NOT_PERSONAL,
    "organization_policy": "organization settings; the actor key is FK SET NULL",
    "erasure_receipt": _COMPLIANCE,
}
