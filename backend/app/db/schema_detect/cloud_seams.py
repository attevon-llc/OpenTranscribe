"""Schema detection, v367-v376: cloud seams, native uuids, quarantine, tenancy stamps, chat.

From v367 on, every arm is a cumulative fingerprint: the newer arm requires its predecessor's
markers plus its own, so the FIRST match in the newest-first ladder is the right one.

``probe()`` issues this era's queries in the order the ladder always has; ``ARMS`` is this
era's slice of the newest-first ladder (``ladder.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from app.db.schema_detect.probe import check_exists

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

    from app.db.schema_detect.ladder import Arm
    from app.db.schema_detect.ladder import SchemaFacts


@dataclass(frozen=True)
class CloudSeamsFacts:
    """Probe answers for v367-v376."""

    has_cloud_seams: bool
    has_legacy_varchar_uuid: bool
    has_superuser_role_invariant: bool
    has_media_file_quarantine: bool
    has_pre_quarantine_status: bool
    has_external_identity_columns: bool
    has_watch_source_org: bool
    has_speaker_cluster_org: bool
    has_tag_user_id: bool
    has_chat_tables: bool
    has_chat_projects: bool


def probe(conn: Connection, tables: list[str]) -> CloudSeamsFacts:
    """Run this era's probes against *conn*, in ladder order."""
    _check_exists = partial(check_exists, conn)
    return CloudSeamsFacts(
        has_cloud_seams=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name = 'organization')"
        ),
        # v368 guard: every column named 'uuid' is already native uuid type. If any
        # legacy character-varying 'uuid' column remains, the DB pre-dates v368 and
        # must be stamped at v367 so v368's idempotent conversion runs on upgrade.
        has_legacy_varchar_uuid=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_schema='public' AND column_name='uuid' AND data_type <> 'uuid')"
        ),
        # v369 guard: the is_superuser/role invariant CHECK constraint exists.
        has_superuser_role_invariant=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_constraint "
            "WHERE conname = 'ck_user_superuser_matches_role')"
        ),
        # v370 guard: the abuse/DMCA takedown column on media_file.
        has_media_file_quarantine=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'media_file' AND column_name = 'is_quarantined')"
        ),
        # v371 guards: takedown prior-status column present AND the generic
        # external-identity column exists (a DB carrying the pre-release
        # vendor-named seam columns instead of user.external_id predates the v371
        # repair and must be stamped lower so the repair runs on upgrade).
        has_pre_quarantine_status=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'media_file' AND column_name = 'pre_quarantine_status')"
        ),
        has_external_identity_columns=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'user' AND column_name = 'external_id')"
        ),
        # v372 guard: creation-time tenant stamp on watch_source. (The audit-event
        # org attribution half of v372 lives in the OpenSearch event schema — no
        # relational column to detect, so this is the revision's sole DB marker.)
        has_watch_source_org=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'watch_source' AND column_name = 'organization_id')"
        ),
        # v373 guard: tenant scope on speaker clusters.
        has_speaker_cluster_org=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'speaker_cluster' AND column_name = 'organization_id')"
        ),
        # v374 guard: per-user tag ownership. tag.user_id is the revision's marker —
        # before it, tag was a global vocabulary with UNIQUE(name) and no owner.
        has_tag_user_id=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'tag' AND column_name = 'user_id')"
        ),
        # v375/v376 guards: the RAG chat tables + chat projects (issue #52/#360).
        has_chat_tables=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'chat_conversation')"
        ),
        has_chat_projects=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name = 'chat_project')"
        ),
    )


# Bound to a name: every arm from v377 to v449 is v376's fingerprint plus its own markers, and
# a copy-pasted 10-term conjunction in 13 places is a place for them to drift apart silently.
def matches_v376(f: SchemaFacts) -> bool:
    return (
        f.seams.has_cloud_seams
        and not f.seams.has_legacy_varchar_uuid
        and f.seams.has_media_file_quarantine
        and f.seams.has_pre_quarantine_status
        and f.seams.has_external_identity_columns
        and f.seams.has_watch_source_org
        and f.seams.has_speaker_cluster_org
        and f.seams.has_tag_user_id
        and f.seams.has_chat_tables
        and f.seams.has_chat_projects
    )


ARMS: tuple[Arm, ...] = (
    # v376: chat projects (chat_project table + chat_conversation.project_id).
    (
        lambda f: matches_v376(f),
        "v376_add_chat_projects",
    ),
    # v375: RAG chat conversations + messages (chat_conversation table).
    (
        lambda f: (
            f.seams.has_cloud_seams
            and not f.seams.has_legacy_varchar_uuid
            and f.seams.has_media_file_quarantine
            and f.seams.has_pre_quarantine_status
            and f.seams.has_external_identity_columns
            and f.seams.has_watch_source_org
            and f.seams.has_speaker_cluster_org
            and f.seams.has_tag_user_id
            and f.seams.has_chat_tables
        ),
        "v375_add_chat_tables",
    ),
    # v374: per-user tag ownership (tag.user_id).
    (
        lambda f: (
            f.seams.has_cloud_seams
            and not f.seams.has_legacy_varchar_uuid
            and f.seams.has_media_file_quarantine
            and f.seams.has_pre_quarantine_status
            and f.seams.has_external_identity_columns
            and f.seams.has_watch_source_org
            and f.seams.has_speaker_cluster_org
            and f.seams.has_tag_user_id
        ),
        "v374_add_tag_user_id",
    ),
    # v373: tenant scope on speaker clusters (speaker_cluster.organization_id).
    (
        lambda f: (
            f.seams.has_cloud_seams
            and not f.seams.has_legacy_varchar_uuid
            and f.seams.has_media_file_quarantine
            and f.seams.has_pre_quarantine_status
            and f.seams.has_external_identity_columns
            and f.seams.has_watch_source_org
            and f.seams.has_speaker_cluster_org
        ),
        "v373_add_cluster_organization_id",
    ),
    # v372: org attribution for background imports (watch_source.organization_id).
    (
        lambda f: (
            f.seams.has_cloud_seams
            and not f.seams.has_legacy_varchar_uuid
            and f.seams.has_media_file_quarantine
            and f.seams.has_pre_quarantine_status
            and f.seams.has_external_identity_columns
            and f.seams.has_watch_source_org
        ),
        "v372_add_audit_organization_id",
    ),
    # v371: repaired seam shape (external_* columns) + takedown prior-status.
    (
        lambda f: (
            f.seams.has_cloud_seams
            and not f.seams.has_legacy_varchar_uuid
            and f.seams.has_media_file_quarantine
            and f.seams.has_pre_quarantine_status
            and f.seams.has_external_identity_columns
        ),
        "v371_repair_cloud_seams_columns",
    ),
    # v370: abuse/DMCA quarantine + legal-hold columns on media_file.
    (
        lambda f: (
            f.seams.has_cloud_seams
            and not f.seams.has_legacy_varchar_uuid
            and f.seams.has_superuser_role_invariant
            and f.seams.has_media_file_quarantine
        ),
        "v370_add_media_file_quarantine",
    ),
    # v369: is_superuser mirrors (role == super_admin), enforced by CHECK.
    (
        lambda f: (
            f.seams.has_cloud_seams
            and not f.seams.has_legacy_varchar_uuid
            and f.seams.has_superuser_role_invariant
        ),
        "v369_superuser_role_invariant",
    ),
    # v368: native-uuid type guard (no-op on current schemas; converts any
    # lingering varchar(36) uuid identifier columns to native uuid).
    (
        lambda f: f.seams.has_cloud_seams and not f.seams.has_legacy_varchar_uuid,
        "v368_uuid_native_type_guard",
    ),
    # v367: cloud-edition seams (organization/usage_event tables, external-id columns)
    (lambda f: f.seams.has_cloud_seams, "v367_add_cloud_seams"),
)
