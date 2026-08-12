"""
Database migration utilities.

Runs Alembic migrations automatically on application startup.
Alembic is the sole authority for database schema creation and upgrades.
Handles empty databases, existing untracked databases, and tracked databases.
"""

import logging
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import text

from alembic import command  # type: ignore[attr-defined]
from app.core.config import settings

logger = logging.getLogger(__name__)


def get_alembic_config() -> Config:
    """Get Alembic configuration."""
    # Find alembic.ini relative to backend directory
    backend_dir = Path(__file__).parent.parent.parent
    alembic_ini = backend_dir / "alembic.ini"

    config = Config(str(alembic_ini))
    config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
    return config


def _detect_schema_version(conn, tables: list[str]) -> str | None:  # noqa: C901
    """Detect the schema version of an existing untracked database.

    Returns the Alembic revision to stamp, or None if no user table exists.
    """
    if "user" not in tables:
        return None

    def _check_exists(query: str) -> bool:
        return bool(conn.execute(text(query)).scalar())

    has_ldap = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name='user' AND column_name='auth_type')"
    )
    # NOTE ON THE RETIRED SPELLING IN THIS FILE. Every probe below describes a
    # schema *as it was at some past revision*, so the pre-v378 column names are the
    # correct — and only possible — fingerprints for those revisions. This file and
    # core/legacy_auth_env.py are the two modules under backend/app permitted to name
    # the old provider at all; see tests/unit/test_oidc_naming_invariant.py.
    has_keycloak_pki = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name='user' AND column_name='keycloak_id')"
    )
    has_fedramp = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='user_mfa')"
    )
    has_search_settings = "system_settings" in tables and _check_exists(
        "SELECT EXISTS(SELECT 1 FROM system_settings WHERE key = 'search.embedding_model')"
    )
    has_overlap_column = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name='transcript_segment' AND column_name='is_overlap')"
    )
    has_pki_fingerprint = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name='user' AND column_name='pki_fingerprint_sha256')"
    )
    has_auth_config = "auth_config" in tables
    has_error_category = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name='media_file' AND column_name='error_category')"
    )
    has_suggestion_source = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name='speaker' AND column_name='suggestion_source')"
    )
    has_perf_indexes = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM pg_indexes "
        "WHERE indexname='idx_media_file_user_status_upload')"
    )
    has_fk_indexes = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM pg_indexes WHERE indexname='idx_comment_media_file_id')"
    )
    has_remaining_fk_indexes = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM pg_indexes WHERE indexname='idx_speaker_user_id')"
    )
    has_model_tracking = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name='media_file' AND column_name='whisper_model')"
    )
    has_segment_unique_constraint = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conname='uq_transcript_segment_content')"
    )
    has_queued_downloading_statuses = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM pg_enum e "
        "JOIN pg_type t ON e.enumtypid = t.oid "
        "WHERE t.typname = 'filestatus' AND e.enumlabel = 'queued')"
    )
    # v073: filestatus native enum was converted to VARCHAR(50)
    # If the status column is VARCHAR and no native enum exists, we're at v073+
    has_varchar_status = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'media_file' AND column_name = 'status' "
        "AND data_type = 'character varying')"
    )
    filestatus_enum_exists = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM pg_type WHERE typname = 'filestatus')"
    )
    has_word_timestamps = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'transcript_segment' AND column_name = 'words')"
    )
    has_allow_local_fallback = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'user' AND column_name = 'allow_local_fallback')"
    )
    has_keycloak_refresh_token = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'user' AND column_name = 'keycloak_refresh_token')"
    )
    has_speaker_attributes = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'speaker' AND column_name = 'predicted_gender')"
    )
    has_collection_default_prompt = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'collection' AND column_name = 'default_summary_prompt_id')"
    )

    has_refresh_token_jti = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name='refresh_token' AND column_name='jti')"
    )
    has_mfa_uuid = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name='user_mfa' AND column_name='uuid')"
    )
    has_user_group = (
        "user_group" in tables and "user_group_member" in tables and "collection_share" in tables
    )

    has_sharing_constraints = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM pg_constraint "
        "WHERE conname = '_collection_share_permission_check')"
    )

    has_speaker_cluster = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
        "WHERE table_name = 'speaker_cluster')"
    )
    has_speaker_clustering_indexes = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM pg_indexes "
        "WHERE indexname = 'idx_speaker_cluster_member_speaker_id')"
    )
    has_cluster_quality_metrics = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'speaker_cluster' AND column_name = 'min_similarity')"
    )
    has_avatar_path = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'speaker_profile' AND column_name = 'avatar_path')"
    )
    has_password_reset_token = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
        "WHERE table_name = 'password_reset_token')"
    )
    has_auto_labeling = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'tag' AND column_name = 'normalized_name')"
    )
    has_asr_settings = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
        "WHERE table_name = 'user_asr_settings')"
    )
    has_gender_confirmed = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'speaker' AND column_name = 'gender_confirmed_by_user')"
    )
    has_speaker_cannot_link = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
        "WHERE table_name = 'speaker_cannot_link')"
    )
    has_cluster_suggested_name = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'speaker_cluster' AND column_name = 'suggested_name')"
    )
    has_shared_configs = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'user_llm_settings' AND column_name = 'is_shared')"
    )
    has_user_media_source = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
        "WHERE table_name = 'user_media_source')"
    )
    has_diarization_disabled = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'media_file' AND column_name = 'diarization_disabled')"
    )
    has_ai_summary_setting = "system_settings" in tables and _check_exists(
        "SELECT EXISTS(SELECT 1 FROM system_settings WHERE key = 'ai.summary_enabled')"
    )
    has_requested_whisper_model = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'media_file' AND column_name = 'requested_whisper_model')"
    )
    has_content_redaction = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'transcript_segment' AND column_name = 'redactions')"
    )
    has_prompt_shared_by = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'summary_prompt' AND column_name = 'shared_by')"
    )
    has_watch_sources = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name = 'watch_source')"
    )
    has_cloud_seams = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name = 'organization')"
    )
    # v368 guard: every column named 'uuid' is already native uuid type. If any
    # legacy character-varying 'uuid' column remains, the DB pre-dates v368 and
    # must be stamped at v367 so v368's idempotent conversion runs on upgrade.
    has_legacy_varchar_uuid = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND column_name='uuid' AND data_type <> 'uuid')"
    )

    # v369 guard: the is_superuser/role invariant CHECK constraint exists.
    has_superuser_role_invariant = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM pg_constraint "
        "WHERE conname = 'ck_user_superuser_matches_role')"
    )

    # v370 guard: the abuse/DMCA takedown column on media_file.
    has_media_file_quarantine = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'media_file' AND column_name = 'is_quarantined')"
    )

    # v371 guards: takedown prior-status column present AND the generic
    # external-identity column exists (a DB carrying the pre-release
    # vendor-named seam columns instead of user.external_id predates the v371
    # repair and must be stamped lower so the repair runs on upgrade).
    has_pre_quarantine_status = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'media_file' AND column_name = 'pre_quarantine_status')"
    )
    has_external_identity_columns = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'user' AND column_name = 'external_id')"
    )

    # v372 guard: creation-time tenant stamp on watch_source. (The audit-event
    # org attribution half of v372 lives in the OpenSearch event schema — no
    # relational column to detect, so this is the revision's sole DB marker.)
    has_watch_source_org = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'watch_source' AND column_name = 'organization_id')"
    )

    # v373 guard: tenant scope on speaker clusters.
    has_speaker_cluster_org = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'speaker_cluster' AND column_name = 'organization_id')"
    )

    # v374 guard: per-user tag ownership. tag.user_id is the revision's marker —
    # before it, tag was a global vocabulary with UNIQUE(name) and no owner.
    has_tag_user_id = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'tag' AND column_name = 'user_id')"
    )

    # v375/v376 guards: the RAG chat tables + chat projects (issue #52/#360).
    has_chat_tables = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
        "WHERE table_name = 'chat_conversation')"
    )
    has_chat_projects = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name = 'chat_project')"
    )

    # v377 guard (formerly v375 on this branch, renumbered — see NOTE ON THE
    # RENUMBERING below): the auth_type CHECK plus the invitation table. The
    # constraint alone was the marker while the revision only hardened the user
    # columns; it now also creates user_invitation / email_verification_token and
    # the user.email_verified columns, so a DB that has the constraint but not
    # the tables predates the extension and must NOT be stamped v377 — it would
    # never receive the new DDL.
    has_auth_type_check = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'ck_user_auth_type_valid')"
    )
    has_user_invitation = "user_invitation" in tables

    # v378 guards (formerly v376): IdP group mapping. BOTH markers are required —
    # the mapping table is useless without user_group_member.source (reconciliation
    # could not tell a directory-derived membership from a hand-added one and would
    # either never revoke or wipe manual work), so a DB carrying only one of them
    # predates the revision and must still receive its DDL.
    has_group_mapping = "group_mapping" in tables
    has_membership_source = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'user_group_member' AND column_name = 'source')"
    )

    # v380 guards (formerly v378): the OIDC identity rename. THREE markers, all
    # required — the revision is a single transaction, so a schema carrying only
    # part of it is a hand-edited database and must stamp lower and receive the
    # whole thing.
    has_oidc_subject = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'user' AND column_name = 'oidc_subject')"
    )
    has_oidc_user_refresh_token = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'user' AND column_name = 'oidc_refresh_token')"
    )
    has_session_id_token = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'refresh_token' AND column_name = 'oidc_id_token')"
    )

    # v381 guard (formerly v379): administrator approval of newly provisioned
    # accounts. TWO markers. The column alone is not the revision: without
    # ck_user_approval_status_valid an unrecognised value reads as neither pending
    # nor rejected, so the gate in api/endpoints/auth/dependencies.py fails OPEN —
    # which is precisely the state a database must not be stamped as already having.
    has_user_approval_status = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'user' AND column_name = 'approval_status')"
    )
    has_approval_status_check = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'ck_user_approval_status_valid')"
    )

    # v382 guards (formerly v380): SCIM tokens and the widened group-source CHECKs.
    # TWO markers, because the revision does two things and a schema carrying only
    # one of them is hand-edited: with the table but not the widened CHECK, every
    # proxy login and every SCIM group write would fail on a CheckViolation, which
    # is the sort of thing a re-run must be able to repair.
    has_scim_token = "scim_token" in tables
    has_proxy_group_source = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM pg_constraint "
        "WHERE conname = 'ck_group_mapping_source_valid' "
        "AND pg_get_constraintdef(oid) LIKE '%proxy%')"
    )

    # v383 guards (formerly v381): the SAML auth-type CHECK widening and its
    # identity column. Same two-marker reasoning as v382 — a schema carrying the
    # column without the widened CHECK would refuse every SAML JIT provision on a
    # CheckViolation.
    has_saml_subject = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'user' AND column_name = 'saml_subject')"
    )
    has_saml_auth_type_check = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM pg_constraint "
        "WHERE conname = 'ck_user_auth_type_valid' "
        "AND pg_get_constraintdef(oid) LIKE '%saml%')"
    )

    # v384 guard: the collapsible reasoning-display column on chat_message. A
    # single nullable TEXT column, so one marker is the whole revision.
    has_chat_reasoning_content = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'chat_message' AND column_name = 'reasoning_content')"
    )

    # v385 guard: three orphan tables dropped (issue #398). This revision REMOVES
    # objects, so the fingerprint is an absence, not a presence — the probe is
    # inverted relative to every additive revision above. All three must be gone;
    # a database with any of them still present predates v385.
    has_orphan_tables = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
        "WHERE table_name IN ('upload_session', 'speaker_audio_clip', "
        "'user_certificate_preferences'))"
    )

    # v379 guard (formerly v377): the auth-config data rename. This revision adds
    # NO DDL, so there is no column to probe — the fingerprint is the absence of
    # the retired key prefix in both config tables. A deployment that never
    # configured OIDC has no matching rows either way, which is correct: v379 is
    # a no-op there, so stamping it costs nothing and re-running it costs nothing.
    has_legacy_oidc_config_keys = (
        has_auth_config
        and "auth_config_audit" in tables
        and _check_exists(
            "SELECT EXISTS(SELECT 1 FROM auth_config WHERE config_key LIKE 'keycloak\\_%' "
            "UNION ALL SELECT 1 FROM auth_config_audit WHERE config_key LIKE 'keycloak\\_%')"
        )
    )

    # NOTE ON THE RENUMBERING. This branch's auth chain originally used v375-v381,
    # branching off v374_add_tag_user_id independently of mainline's
    # v375_add_chat_tables/v376_add_chat_projects (issue #52/#360) — both sides
    # revised v374, producing two heads. Reconciled by renumbering this branch's
    # seven revisions to v377-v383 (after master's chat chain) rather than
    # renumbering master's, since master's chain had already reached production.
    # Every revision id, detection arm, test file and doc reference below reflects
    # the NEW numbers; nothing about the schema DDL itself changed.

    # v386: the tag_share table (sharing a tag with specific users/groups).
    has_tag_share = "tag_share" in tables

    # v387 guard: the five "who did this" FKs into `user` became ON DELETE SET NULL,
    # so deleting an admin who had ever changed auth config, quarantined someone
    # else's file, or shared someone else's prompt stops being a 500. Probed on
    # `auth_config_audit.changed_by` because that one ALSO lost its NOT NULL — it is
    # the marker that cannot be reached by a partial hand-repair of the others.
    # `confdeltype = 'n'` is Postgres's code for SET NULL.
    has_actor_fk_set_null = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM pg_constraint "
        "WHERE conname = 'auth_config_audit_changed_by_fkey' AND confdeltype = 'n')"
    )
    # Second half of the same revision: the CHECK v386 left off tag_share.target_type
    # while mirroring collection_share. Required as well as the FK marker, so a
    # database that somehow carries one and not the other re-runs the revision.
    has_tag_share_type_check = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conname = '_tag_share_target_type_check')"
    )
    # Third half: the legacy duplicate of ck_user_role_valid is gone (v200 added it,
    # v387 drops it). An ABSENCE probe, like v385's — the same shape v380 used when it
    # removed the duplicate auth_type CHECK. Widening one of a pair and not the other is
    # a CheckViolation at login, not at migration time.
    has_legacy_role_check = _check_exists(
        "SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'users_role_check')"
    )

    # Return the highest version stamp that matches (newest first)
    # v387: the actor FKs are SET NULL and tag_share.target_type is constrained.
    # Everything v386 requires must also hold — this revision only alters existing
    # objects, so it is v386's fingerprint plus the two new rules.
    if (
        has_cloud_seams
        and not has_legacy_varchar_uuid
        and has_media_file_quarantine
        and has_pre_quarantine_status
        and has_external_identity_columns
        and has_watch_source_org
        and has_speaker_cluster_org
        and has_tag_user_id
        and has_chat_tables
        and has_chat_projects
        and has_auth_type_check
        and has_user_invitation
        and has_group_mapping
        and has_membership_source
        and not has_legacy_oidc_config_keys
        and has_oidc_subject
        and has_oidc_user_refresh_token
        and has_session_id_token
        and has_user_approval_status
        and has_approval_status_check
        and has_scim_token
        and has_proxy_group_source
        and has_saml_subject
        and has_saml_auth_type_check
        and has_chat_reasoning_content
        and not has_orphan_tables
        and has_tag_share
        and has_actor_fk_set_null
        and has_tag_share_type_check
        and not has_legacy_role_check
    ):
        return "v387_actor_fks_and_tag_share_check"
    # v386: tag_share exists. Everything v385 requires must also hold — this
    # revision only adds, so it is v385's fingerprint plus the new table.
    if (
        has_cloud_seams
        and not has_legacy_varchar_uuid
        and has_media_file_quarantine
        and has_pre_quarantine_status
        and has_external_identity_columns
        and has_watch_source_org
        and has_speaker_cluster_org
        and has_tag_user_id
        and has_chat_tables
        and has_chat_projects
        and has_auth_type_check
        and has_user_invitation
        and has_group_mapping
        and has_membership_source
        and not has_legacy_oidc_config_keys
        and has_oidc_subject
        and has_oidc_user_refresh_token
        and has_session_id_token
        and has_user_approval_status
        and has_approval_status_check
        and has_scim_token
        and has_proxy_group_source
        and has_saml_subject
        and has_saml_auth_type_check
        and has_chat_reasoning_content
        and not has_orphan_tables
        and has_tag_share
        # Mutual exclusion with the arm above, the same way v385 excludes v386: v387
        # only ALTERS objects v386 created, so without this both arms describe the
        # same database and the pair reads as ambiguous even though ordering saves it.
        and not (has_actor_fk_set_null and has_tag_share_type_check and not has_legacy_role_check)
    ):
        return "v386_add_tag_share"
    # v385: the three orphan tables are gone. Everything v384 requires must also
    # hold — this revision only subtracts, so it shares v384's fingerprint plus
    # the absence.
    if (
        has_cloud_seams
        and not has_legacy_varchar_uuid
        and has_media_file_quarantine
        and has_pre_quarantine_status
        and has_external_identity_columns
        and has_watch_source_org
        and has_speaker_cluster_org
        and has_tag_user_id
        and has_chat_tables
        and has_chat_projects
        and has_auth_type_check
        and has_user_invitation
        and has_group_mapping
        and has_membership_source
        and not has_legacy_oidc_config_keys
        and has_oidc_subject
        and has_oidc_user_refresh_token
        and has_session_id_token
        and has_user_approval_status
        and has_approval_status_check
        and has_scim_token
        and has_proxy_group_source
        and has_saml_subject
        and has_saml_auth_type_check
        and has_chat_reasoning_content
        and not has_orphan_tables
        # Without this, v385's arm matches a v386 database too and shadows it —
        # the ladder returns the FIRST match, and v386 only adds a table.
        and not has_tag_share
    ):
        return "v385_drop_orphan_tables"
    # v384: the chat_message.reasoning_content column (collapsible reasoning display).
    if (
        has_cloud_seams
        and not has_legacy_varchar_uuid
        and has_media_file_quarantine
        and has_pre_quarantine_status
        and has_external_identity_columns
        and has_watch_source_org
        and has_speaker_cluster_org
        and has_tag_user_id
        and has_chat_tables
        and has_chat_projects
        and has_auth_type_check
        and has_user_invitation
        and has_group_mapping
        and has_membership_source
        and not has_legacy_oidc_config_keys
        and has_oidc_subject
        and has_oidc_user_refresh_token
        and has_session_id_token
        and has_user_approval_status
        and has_approval_status_check
        and has_scim_token
        and has_proxy_group_source
        and has_saml_subject
        and has_saml_auth_type_check
        and has_chat_reasoning_content
    ):
        return "v384_add_chat_reasoning_content"
    # v383: SAML auth-type CHECK widening + user.saml_subject.
    if (
        has_cloud_seams
        and not has_legacy_varchar_uuid
        and has_media_file_quarantine
        and has_pre_quarantine_status
        and has_external_identity_columns
        and has_watch_source_org
        and has_speaker_cluster_org
        and has_tag_user_id
        and has_chat_tables
        and has_chat_projects
        and has_auth_type_check
        and has_user_invitation
        and has_group_mapping
        and has_membership_source
        and not has_legacy_oidc_config_keys
        and has_oidc_subject
        and has_oidc_user_refresh_token
        and has_session_id_token
        and has_user_approval_status
        and has_approval_status_check
        and has_scim_token
        and has_proxy_group_source
        and has_saml_subject
        and has_saml_auth_type_check
    ):
        return "v383_saml_auth_type"
    # v382: SCIM provisioning tokens + 'proxy'/'scim' group sources.
    if (
        has_cloud_seams
        and not has_legacy_varchar_uuid
        and has_media_file_quarantine
        and has_pre_quarantine_status
        and has_external_identity_columns
        and has_watch_source_org
        and has_speaker_cluster_org
        and has_tag_user_id
        and has_chat_tables
        and has_chat_projects
        and has_auth_type_check
        and has_user_invitation
        and has_group_mapping
        and has_membership_source
        and not has_legacy_oidc_config_keys
        and has_oidc_subject
        and has_oidc_user_refresh_token
        and has_session_id_token
        and has_user_approval_status
        and has_approval_status_check
        and has_scim_token
        and has_proxy_group_source
    ):
        return "v382_scim_tokens"
    # v381: administrator approval state on user.
    if (
        has_cloud_seams
        and not has_legacy_varchar_uuid
        and has_media_file_quarantine
        and has_pre_quarantine_status
        and has_external_identity_columns
        and has_watch_source_org
        and has_speaker_cluster_org
        and has_tag_user_id
        and has_chat_tables
        and has_chat_projects
        and has_auth_type_check
        and has_user_invitation
        and has_group_mapping
        and has_membership_source
        and not has_legacy_oidc_config_keys
        and has_oidc_subject
        and has_oidc_user_refresh_token
        and has_session_id_token
        and has_user_approval_status
        and has_approval_status_check
    ):
        return "v381_approval_state"
    # v380: OIDC identity columns and the auth_type value rename.
    if (
        has_cloud_seams
        and not has_legacy_varchar_uuid
        and has_media_file_quarantine
        and has_pre_quarantine_status
        and has_external_identity_columns
        and has_watch_source_org
        and has_speaker_cluster_org
        and has_tag_user_id
        and has_chat_tables
        and has_chat_projects
        and has_auth_type_check
        and has_user_invitation
        and has_group_mapping
        and has_membership_source
        and not has_legacy_oidc_config_keys
        and has_oidc_subject
        and has_oidc_user_refresh_token
        and has_session_id_token
    ):
        return "v380_oidc_identity_columns"
    # v379: auth_config / auth_config_audit keys renamed to the oidc_ prefix.
    if (
        has_cloud_seams
        and not has_legacy_varchar_uuid
        and has_media_file_quarantine
        and has_pre_quarantine_status
        and has_external_identity_columns
        and has_watch_source_org
        and has_speaker_cluster_org
        and has_tag_user_id
        and has_chat_tables
        and has_chat_projects
        and has_auth_type_check
        and has_user_invitation
        and has_group_mapping
        and has_membership_source
        and not has_legacy_oidc_config_keys
    ):
        return "v379_rename_keycloak_config_to_oidc"
    # v378: directory groups drive in-app groups and privileges.
    if (
        has_cloud_seams
        and not has_legacy_varchar_uuid
        and has_media_file_quarantine
        and has_pre_quarantine_status
        and has_external_identity_columns
        and has_watch_source_org
        and has_speaker_cluster_org
        and has_tag_user_id
        and has_chat_tables
        and has_chat_projects
        and has_auth_type_check
        and has_user_invitation
        and has_group_mapping
        and has_membership_source
    ):
        return "v378_idp_group_mapping"
    # v377: user auth invariants (role NOT NULL + auth_type CHECK) and the
    # invitation / email-verification schema.
    if (
        has_cloud_seams
        and not has_legacy_varchar_uuid
        and has_media_file_quarantine
        and has_pre_quarantine_status
        and has_external_identity_columns
        and has_watch_source_org
        and has_speaker_cluster_org
        and has_tag_user_id
        and has_chat_tables
        and has_chat_projects
        and has_auth_type_check
        and has_user_invitation
    ):
        return "v377_harden_user_auth_invariants"
    # v376: chat projects (chat_project table + chat_conversation.project_id).
    if (
        has_cloud_seams
        and not has_legacy_varchar_uuid
        and has_media_file_quarantine
        and has_pre_quarantine_status
        and has_external_identity_columns
        and has_watch_source_org
        and has_speaker_cluster_org
        and has_tag_user_id
        and has_chat_tables
        and has_chat_projects
    ):
        return "v376_add_chat_projects"
    # v375: RAG chat conversations + messages (chat_conversation table).
    if (
        has_cloud_seams
        and not has_legacy_varchar_uuid
        and has_media_file_quarantine
        and has_pre_quarantine_status
        and has_external_identity_columns
        and has_watch_source_org
        and has_speaker_cluster_org
        and has_tag_user_id
        and has_chat_tables
    ):
        return "v375_add_chat_tables"
    # v374: per-user tag ownership (tag.user_id).
    if (
        has_cloud_seams
        and not has_legacy_varchar_uuid
        and has_media_file_quarantine
        and has_pre_quarantine_status
        and has_external_identity_columns
        and has_watch_source_org
        and has_speaker_cluster_org
        and has_tag_user_id
    ):
        return "v374_add_tag_user_id"
    # v373: tenant scope on speaker clusters (speaker_cluster.organization_id).
    if (
        has_cloud_seams
        and not has_legacy_varchar_uuid
        and has_media_file_quarantine
        and has_pre_quarantine_status
        and has_external_identity_columns
        and has_watch_source_org
        and has_speaker_cluster_org
    ):
        return "v373_add_cluster_organization_id"
    # v372: org attribution for background imports (watch_source.organization_id).
    if (
        has_cloud_seams
        and not has_legacy_varchar_uuid
        and has_media_file_quarantine
        and has_pre_quarantine_status
        and has_external_identity_columns
        and has_watch_source_org
    ):
        return "v372_add_audit_organization_id"
    # v371: repaired seam shape (external_* columns) + takedown prior-status.
    if (
        has_cloud_seams
        and not has_legacy_varchar_uuid
        and has_media_file_quarantine
        and has_pre_quarantine_status
        and has_external_identity_columns
    ):
        return "v371_repair_cloud_seams_columns"
    # v370: abuse/DMCA quarantine + legal-hold columns on media_file.
    if (
        has_cloud_seams
        and not has_legacy_varchar_uuid
        and has_superuser_role_invariant
        and has_media_file_quarantine
    ):
        return "v370_add_media_file_quarantine"
    # v369: is_superuser mirrors (role == super_admin), enforced by CHECK.
    if has_cloud_seams and not has_legacy_varchar_uuid and has_superuser_role_invariant:
        return "v369_superuser_role_invariant"
    # v368: native-uuid type guard (no-op on current schemas; converts any
    # lingering varchar(36) uuid identifier columns to native uuid).
    if has_cloud_seams and not has_legacy_varchar_uuid:
        return "v368_uuid_native_type_guard"
    # v367: cloud-edition seams (organization/usage_event tables, external-id columns)
    if has_cloud_seams:
        return "v367_add_cloud_seams"
    # v366: watch-source auto-import tables
    if has_watch_sources:
        return "v366_add_watch_sources"
    # v365: shared_by attribution column on summary_prompt
    if has_prompt_shared_by:
        return "v365_add_prompt_shared_by"
    # v364: content redaction columns (transcript_segment.redactions/toxicity, media_file.redaction_*)
    if has_content_redaction:
        return "v364_add_content_redaction"
    # v352: per-transcription whisper model selection
    if has_requested_whisper_model:
        return "v352_add_requested_whisper_model"
    # v351: AI summary enabled system setting
    if has_ai_summary_setting:
        return "v351_add_ai_summary_settings"
    # v350: diarization_disabled flag on media_file
    if has_diarization_disabled:
        return "v350_add_diarization_disabled"
    # v340: per-user media sources table
    if has_user_media_source:
        return "v340_add_user_media_sources"
    # v330: sharing columns on LLM/ASR settings and prompts
    if has_shared_configs:
        return "v330_add_shared_configs_and_prompts"
    # v320: suggested_name column on speaker_cluster for title-based name extraction
    if has_cluster_suggested_name:
        return "v320_add_cluster_suggested_name"
    # v310: speaker constraint tables (cannot-link and profile blacklist)
    if has_speaker_cannot_link:
        return "v310_add_speaker_constraints"
    # v300: gender_confirmed_by_user column on speaker table
    if has_gender_confirmed:
        return "v300_add_gender_confirmed"
    # v290: password_reset_token table for self-service password recovery
    if has_password_reset_token:
        return "v290_add_password_reset_tokens"
    # v270: ASR provider support tables (user_asr_settings)
    if has_asr_settings:
        return "v270_add_asr_provider_support"
    # v270: profile avatar_path column
    if has_speaker_cluster and has_cluster_quality_metrics and has_avatar_path:
        return "v270_add_profile_avatar"
    # v260: cluster quality metrics (min_similarity, separation_score, margin)
    if has_speaker_cluster and has_cluster_quality_metrics:
        return "v260_add_cluster_quality_metrics"
    # v250: speaker clustering FK indexes
    if has_speaker_cluster and has_speaker_clustering_indexes:
        return "v250_add_speaker_clustering_indexes"
    # v230: auto-labeling support
    if has_auto_labeling:
        return "v230_add_auto_labeling"
    # v220: speaker clustering tables
    if has_speaker_cluster:
        return "v220_add_speaker_clusters"
    # v211: CHECK constraints and indexes on groups/sharing tables
    if has_user_group and has_sharing_constraints:
        return "v211_add_sharing_constraints_and_indexes"
    # v210: user groups and collection sharing tables
    if has_user_group:
        return "v210_add_groups_and_sharing"
    # v200: schema reconciliation (jti on refresh_token + uuid on user_mfa)
    if has_collection_default_prompt and has_refresh_token_jti and has_mfa_uuid:
        return "v200_schema_reconciliation"
    # v190: default_summary_prompt_id column on collection table
    if has_collection_default_prompt:
        return "v190_add_collection_default_prompt"
    # v180: speaker attribute detection columns
    if has_speaker_attributes:
        return "v180_add_speaker_attributes"
    # v170: keycloak_refresh_token column for federated logout
    if has_keycloak_refresh_token:
        return "v170_add_keycloak_refresh_token"
    # v160: allow_local_fallback column added to user table
    if has_allow_local_fallback:
        return "v160_add_allow_local_fallback"
    # v140: words column added to transcript_segment
    if has_word_timestamps and has_varchar_status and not filestatus_enum_exists:
        return "v140_add_word_timestamps"
    # v073: status column is VARCHAR and no native enum exists
    # (This also matches fresh installs from init_db.sql which use VARCHAR)
    if has_varchar_status and not filestatus_enum_exists and has_segment_unique_constraint:
        return "v073_convert_filestatus_enum_to_varchar"
    if has_queued_downloading_statuses:
        return "v072_add_queued_downloading_statuses"
    if has_segment_unique_constraint:
        return "v071_add_transcript_segment_unique_constraint"
    if has_model_tracking:
        return "v130_add_processing_model_tracking"
    # The v120/v110/v100/v091/v090 markers can ALSO be present in older
    # init_db.sql-bootstrapped schemas (notably v0.3.3) that pre-date the
    # FedRAMP auth tables. Only treat the database as truly at v120/v110/etc
    # if the fedramp auth tables (created by v040) actually exist. Otherwise
    # the schema is inconsistent and we must stamp BEFORE v040 so the
    # missing tables get created.
    if has_remaining_fk_indexes and has_fedramp:
        return "v120_add_remaining_fk_indexes"
    if has_fk_indexes and has_fedramp:
        return "v110_add_missing_fk_indexes"
    if has_perf_indexes and has_fedramp:
        return "v100_optimize_query_performance"
    if has_suggestion_source and has_fedramp:
        return "v091_add_speaker_suggestion_source"
    if has_error_category and has_fedramp:
        return "v090_add_error_category"
    if has_auth_config:
        return "v080_add_auth_config"
    if has_pki_fingerprint:
        return "v070_pki_security"
    if has_overlap_column:
        return "v060_add_transcript_overlap"
    if has_fedramp and has_search_settings:
        return "v050_add_search_settings"
    if has_fedramp:
        return "v040_add_fedramp_compliance"
    if has_keycloak_pki:
        return "v031_add_keycloak_pki_auth"
    if has_ldap:
        return "v030_add_ldap_auth"
    if "system_settings" in tables:
        return "v020_add_system_settings"
    return "v010_baseline"


def _repair_skipped_v230(config) -> None:
    """Apply v230 schema changes if they were skipped due to branch merge.

    The v230_add_auto_labeling migration may have been skipped on databases
    that were upgraded through the v250→v260→v270 branch before the migration
    chain was linearised. All SQL is idempotent (IF NOT EXISTS).
    """

    engine = create_engine(settings.DATABASE_URL)
    try:
        with engine.connect() as conn:
            missing = not _check_column_exists(conn, "media_file", "upload_batch_id")
        if not missing:
            return

        logger.info("Detected missing v230 schema changes — applying repair...")
        with engine.connect() as conn:
            # Import and run the v230 upgrade function directly
            # Run inside an alembic operation context
            from alembic.operations import Operations
            from alembic.runtime.migration import MigrationContext

            from alembic.versions.v230_add_auto_labeling import upgrade as v230_upgrade

            mc = MigrationContext.configure(conn)
            ops = Operations(mc)  # noqa: F841
            v230_upgrade()
            conn.commit()
        logger.info("v230 repair complete")
    except Exception:
        # Fall back to raw SQL for the critical missing column
        logger.warning("v230 module import failed, applying critical columns directly")
        with engine.connect() as conn:
            conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS upload_batch (
                    id SERIAL PRIMARY KEY,
                    uuid UUID UNIQUE NOT NULL DEFAULT gen_random_uuid(),
                    user_id INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
                    source VARCHAR(50) NOT NULL,
                    file_count INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    grouping_status VARCHAR(50) DEFAULT 'pending'
                )
            """)
            )
            conn.execute(
                text("""
                DO $$ BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'media_file' AND column_name = 'upload_batch_id'
                    ) THEN
                        ALTER TABLE media_file
                        ADD COLUMN upload_batch_id INTEGER
                        REFERENCES upload_batch(id) ON DELETE SET NULL;
                    END IF;
                END $$
            """)
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_media_file_upload_batch_id "
                    "ON media_file(upload_batch_id)"
                )
            )
            # tag columns
            conn.execute(
                text("""
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'tag' AND column_name = 'source')
                    THEN ALTER TABLE tag ADD COLUMN source VARCHAR(50);
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'tag' AND column_name = 'normalized_name')
                    THEN ALTER TABLE tag ADD COLUMN normalized_name VARCHAR;
                    END IF;
                END $$
            """)
            )
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_tag_normalized_name ON tag(normalized_name)")
            )
            # file_tag columns
            conn.execute(
                text("""
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'file_tag' AND column_name = 'source')
                    THEN ALTER TABLE file_tag ADD COLUMN source VARCHAR(50);
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'file_tag' AND column_name = 'ai_confidence')
                    THEN ALTER TABLE file_tag ADD COLUMN ai_confidence FLOAT;
                    END IF;
                END $$
            """)
            )
            # collection columns
            conn.execute(
                text("""
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'collection' AND column_name = 'source')
                    THEN ALTER TABLE collection ADD COLUMN source VARCHAR(50);
                    END IF;
                END $$
            """)
            )
            # collection_member columns
            conn.execute(
                text("""
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'collection_member' AND column_name = 'source')
                    THEN ALTER TABLE collection_member ADD COLUMN source VARCHAR(50);
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'collection_member' AND column_name = 'ai_confidence')
                    THEN ALTER TABLE collection_member ADD COLUMN ai_confidence FLOAT;
                    END IF;
                END $$
            """)
            )
            # topic_suggestion columns
            conn.execute(
                text("""
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'topic_suggestion' AND column_name = 'auto_applied_tags')
                    THEN ALTER TABLE topic_suggestion ADD COLUMN auto_applied_tags JSONB DEFAULT '[]'::jsonb;
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'topic_suggestion' AND column_name = 'auto_applied_collections')
                    THEN ALTER TABLE topic_suggestion ADD COLUMN auto_applied_collections JSONB DEFAULT '[]'::jsonb;
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'topic_suggestion' AND column_name = 'auto_apply_completed_at')
                    THEN ALTER TABLE topic_suggestion ADD COLUMN auto_apply_completed_at TIMESTAMPTZ;
                    END IF;
                END $$
            """)
            )
            # Backfill normalized_name
            conn.execute(
                text("""
                UPDATE tag
                SET normalized_name = LOWER(TRIM(REGEXP_REPLACE(
                    REGEXP_REPLACE(name, '[-_]+', ' ', 'g'), '\\s+', ' ', 'g')))
                WHERE normalized_name IS NULL
            """)
            )
            conn.commit()
        logger.info("v230 repair (direct SQL) complete")
    finally:
        engine.dispose()


def _check_column_exists(conn, table: str, column: str) -> bool:
    """Check if a column exists in a table."""
    result = conn.execute(
        text(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :table AND column_name = :column)"
        ),
        {"table": table, "column": column},
    )
    return bool(result.scalar())


def run_migrations() -> None:
    """Run database migrations on startup.

    Alembic is the sole authority for database schema. Handles:
    1. Empty database: Run all migrations from scratch (alembic upgrade head)
    2. Existing untracked DB: Detect version, stamp, then upgrade
    3. Already tracked: Apply any pending migrations

    Uses a PostgreSQL advisory lock to prevent concurrent migration runs
    when multiple backend instances start simultaneously.
    """

    logger.info("Checking database migrations...")

    engine = create_engine(settings.DATABASE_URL)

    # Acquire the advisory lock on a DEDICATED connection held open for the whole
    # upgrade (issue #284 A1.4).
    #
    # pg_advisory_lock is SESSION-scoped. The previous code took it on a pooled
    # connection, returned that connection to the pool, then called engine.dispose()
    # below — closing the session and silently dropping the lock BEFORE
    # command.upgrade() ever ran. Concurrent replicas could therefore race Alembic.
    # The matching unlock in the finally used a *fresh* connection, which is a no-op
    # for the same reason: a session cannot release a lock it does not hold.
    #
    # A separate engine is used so the engine.dispose() further down (which exists to
    # free pooled connections before Alembic opens its own) cannot close this one.
    lock_engine = create_engine(settings.DATABASE_URL)
    lock_conn = lock_engine.connect()
    lock_conn.execute(text("SELECT pg_advisory_lock(42)"))
    lock_conn.commit()
    logger.info("Acquired migration advisory lock")

    try:
        # Ensure alembic_version column is wide enough for long revision IDs
        with engine.connect() as conn:
            if "alembic_version" in inspect(engine).get_table_names():
                conn.execute(
                    text("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(128)")
                )
                conn.commit()

        with engine.connect() as conn:
            context = MigrationContext.configure(conn)
            current_rev = context.get_current_revision()
            inspector = inspect(engine)
            tables = inspector.get_table_names()
            detected_version = _detect_schema_version(conn, tables)

        # Dispose the engine to release all pooled connections before Alembic opens its own
        engine.dispose()

        config = get_alembic_config()

        # Get the head revision from Alembic scripts
        from alembic.script import ScriptDirectory

        script_dir = ScriptDirectory.from_config(config)
        head_rev = script_dir.get_current_head()

        if current_rev:
            logger.info(f"Current migration version: {current_rev}")
            if current_rev == head_rev:
                logger.info("Database is up to date, no migrations needed")
            else:
                logger.info(f"Upgrading from {current_rev} to {head_rev}...")
                command.upgrade(config, "head")
        elif detected_version:
            logger.info(f"Existing database detected, stamping {detected_version}...")
            command.stamp(config, detected_version)
            if detected_version != head_rev:
                logger.info("Applying migrations to upgrade to current version...")
                command.upgrade(config, "head")
        elif tables:
            logger.info("Existing untracked database detected, stamping current version...")
            command.stamp(config, "head")
        else:
            logger.info("Empty database detected, running full migration...")
            command.upgrade(config, "head")

        # Post-migration repair: apply any idempotent schema changes from v230
        # that may have been skipped due to a branch merge ordering issue.
        _repair_skipped_v230(config)

        logger.info("Database migrations complete")
    finally:
        # Release from the SAME session that took it — advisory locks are
        # session-scoped, so unlocking from anywhere else does nothing.
        try:
            lock_conn.execute(text("SELECT pg_advisory_unlock(42)"))
            lock_conn.commit()
            logger.info("Released migration advisory lock")
        except Exception as exc:  # noqa: BLE001 - closing the session releases it anyway
            logger.warning("Advisory unlock failed (%s); closing the session releases it", exc)
        finally:
            lock_conn.close()
            lock_engine.dispose()
