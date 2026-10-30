"""Schema detection, v010-v366: the schema before the cloud seams.

Each arm here keys on one or two markers rather than on a cumulative fingerprint, so several
states stamp LOWER than the revision they carry (a v080-v130 schema stamps v073, for example);
that is survivable only because the re-run revisions are idempotent.

``probe()`` issues this era's queries in the order the ladder always has; ``ARMS`` is this
era's slice of the newest-first ladder (``ladder.py``), and ``v010_baseline`` is the fallback
in ``migrations._detect_schema_version``.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from app.db.schema_detect.probe import check_exists

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

    from app.db.schema_detect.ladder import Arm


@dataclass(frozen=True)
class LegacyFacts:
    """Probe answers for v010-v366."""

    has_ldap: bool
    has_keycloak_pki: bool
    has_fedramp: bool
    has_search_settings: bool
    has_overlap_column: bool
    has_pki_fingerprint: bool
    has_auth_config: bool
    has_error_category: bool
    has_suggestion_source: bool
    has_perf_indexes: bool
    has_fk_indexes: bool
    has_remaining_fk_indexes: bool
    has_model_tracking: bool
    has_segment_unique_constraint: bool
    has_queued_downloading_statuses: bool
    has_varchar_status: bool
    filestatus_enum_exists: bool
    has_word_timestamps: bool
    has_allow_local_fallback: bool
    has_keycloak_refresh_token: bool
    has_speaker_attributes: bool
    has_collection_default_prompt: bool
    has_refresh_token_jti: bool
    has_mfa_uuid: bool
    has_user_group: bool
    has_sharing_constraints: bool
    has_speaker_cluster: bool
    has_speaker_clustering_indexes: bool
    has_cluster_quality_metrics: bool
    has_avatar_path: bool
    has_password_reset_token: bool
    has_auto_labeling: bool
    has_asr_settings: bool
    has_gender_confirmed: bool
    has_speaker_cannot_link: bool
    has_cluster_suggested_name: bool
    has_shared_configs: bool
    has_user_media_source: bool
    has_diarization_disabled: bool
    has_ai_summary_setting: bool
    has_requested_whisper_model: bool
    has_content_redaction: bool
    has_prompt_shared_by: bool
    has_watch_sources: bool


def probe(conn: Connection, tables: list[str]) -> LegacyFacts:
    """Run this era's probes against *conn*, in ladder order."""
    _check_exists = partial(check_exists, conn)
    return LegacyFacts(
        has_ldap=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name='user' AND column_name='auth_type')"
        ),
        # NOTE ON THE RETIRED SPELLING IN THIS FILE. Every probe below describes a
        # schema *as it was at some past revision*, so the pre-v378 column names are the
        # correct — and only possible — fingerprints for those revisions. This module,
        # schema_detect/auth_identity.py (v379's data marker) and core/legacy_auth_env.py are
        # the modules under backend/app permitted to name the old provider at all; see
        # tests/unit/test_oidc_naming_invariant.py.
        has_keycloak_pki=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name='user' AND column_name='keycloak_id')"
        ),
        has_fedramp=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='user_mfa')"
        ),
        has_search_settings="system_settings" in tables
        and _check_exists(
            "SELECT EXISTS(SELECT 1 FROM system_settings WHERE key = 'search.embedding_model')"
        ),
        has_overlap_column=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name='transcript_segment' AND column_name='is_overlap')"
        ),
        has_pki_fingerprint=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name='user' AND column_name='pki_fingerprint_sha256')"
        ),
        has_auth_config="auth_config" in tables,
        has_error_category=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name='media_file' AND column_name='error_category')"
        ),
        has_suggestion_source=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name='speaker' AND column_name='suggestion_source')"
        ),
        has_perf_indexes=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_indexes "
            "WHERE indexname='idx_media_file_user_status_upload')"
        ),
        has_fk_indexes=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_indexes WHERE indexname='idx_comment_media_file_id')"
        ),
        has_remaining_fk_indexes=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_indexes WHERE indexname='idx_speaker_user_id')"
        ),
        has_model_tracking=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name='media_file' AND column_name='whisper_model')"
        ),
        has_segment_unique_constraint=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conname='uq_transcript_segment_content')"
        ),
        has_queued_downloading_statuses=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_enum e "
            "JOIN pg_type t ON e.enumtypid = t.oid "
            "WHERE t.typname = 'filestatus' AND e.enumlabel = 'queued')"
        ),
        # v073: filestatus native enum was converted to VARCHAR(50)
        # If the status column is VARCHAR and no native enum exists, we're at v073+
        has_varchar_status=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'media_file' AND column_name = 'status' "
            "AND data_type = 'character varying')"
        ),
        filestatus_enum_exists=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_type WHERE typname = 'filestatus')"
        ),
        has_word_timestamps=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'transcript_segment' AND column_name = 'words')"
        ),
        has_allow_local_fallback=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'user' AND column_name = 'allow_local_fallback')"
        ),
        has_keycloak_refresh_token=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'user' AND column_name = 'keycloak_refresh_token')"
        ),
        has_speaker_attributes=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'speaker' AND column_name = 'predicted_gender')"
        ),
        has_collection_default_prompt=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'collection' AND column_name = 'default_summary_prompt_id')"
        ),
        has_refresh_token_jti=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name='refresh_token' AND column_name='jti')"
        ),
        has_mfa_uuid=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name='user_mfa' AND column_name='uuid')"
        ),
        has_user_group="user_group" in tables
        and "user_group_member" in tables
        and "collection_share" in tables,
        has_sharing_constraints=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_constraint "
            "WHERE conname = '_collection_share_permission_check')"
        ),
        has_speaker_cluster=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'speaker_cluster')"
        ),
        has_speaker_clustering_indexes=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_indexes "
            "WHERE indexname = 'idx_speaker_cluster_member_speaker_id')"
        ),
        has_cluster_quality_metrics=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'speaker_cluster' AND column_name = 'min_similarity')"
        ),
        has_avatar_path=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'speaker_profile' AND column_name = 'avatar_path')"
        ),
        has_password_reset_token=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'password_reset_token')"
        ),
        has_auto_labeling=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'tag' AND column_name = 'normalized_name')"
        ),
        has_asr_settings=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'user_asr_settings')"
        ),
        has_gender_confirmed=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'speaker' AND column_name = 'gender_confirmed_by_user')"
        ),
        has_speaker_cannot_link=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'speaker_cannot_link')"
        ),
        has_cluster_suggested_name=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'speaker_cluster' AND column_name = 'suggested_name')"
        ),
        has_shared_configs=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'user_llm_settings' AND column_name = 'is_shared')"
        ),
        has_user_media_source=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'user_media_source')"
        ),
        has_diarization_disabled=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'media_file' AND column_name = 'diarization_disabled')"
        ),
        has_ai_summary_setting="system_settings" in tables
        and _check_exists(
            "SELECT EXISTS(SELECT 1 FROM system_settings WHERE key = 'ai.summary_enabled')"
        ),
        has_requested_whisper_model=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'media_file' AND column_name = 'requested_whisper_model')"
        ),
        has_content_redaction=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'transcript_segment' AND column_name = 'redactions')"
        ),
        has_prompt_shared_by=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'summary_prompt' AND column_name = 'shared_by')"
        ),
        has_watch_sources=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name = 'watch_source')"
        ),
    )


ARMS: tuple[Arm, ...] = (
    # v366: watch-source auto-import tables
    (lambda f: f.legacy.has_watch_sources, "v366_add_watch_sources"),
    # v365: shared_by attribution column on summary_prompt
    (lambda f: f.legacy.has_prompt_shared_by, "v365_add_prompt_shared_by"),
    # v364: content redaction columns (transcript_segment.redactions/toxicity, media_file.redaction_*)
    (lambda f: f.legacy.has_content_redaction, "v364_add_content_redaction"),
    # v352: per-transcription whisper model selection
    (lambda f: f.legacy.has_requested_whisper_model, "v352_add_requested_whisper_model"),
    # v351: AI summary enabled system setting
    (lambda f: f.legacy.has_ai_summary_setting, "v351_add_ai_summary_settings"),
    # v350: diarization_disabled flag on media_file
    (lambda f: f.legacy.has_diarization_disabled, "v350_add_diarization_disabled"),
    # v340: per-user media sources table
    (lambda f: f.legacy.has_user_media_source, "v340_add_user_media_sources"),
    # v330: sharing columns on LLM/ASR settings and prompts
    (lambda f: f.legacy.has_shared_configs, "v330_add_shared_configs_and_prompts"),
    # v320: suggested_name column on speaker_cluster for title-based name extraction
    (lambda f: f.legacy.has_cluster_suggested_name, "v320_add_cluster_suggested_name"),
    # v310: speaker constraint tables (cannot-link and profile blacklist)
    (lambda f: f.legacy.has_speaker_cannot_link, "v310_add_speaker_constraints"),
    # v300: gender_confirmed_by_user column on speaker table
    (lambda f: f.legacy.has_gender_confirmed, "v300_add_gender_confirmed"),
    # v290: password_reset_token table for self-service password recovery
    (lambda f: f.legacy.has_password_reset_token, "v290_add_password_reset_tokens"),
    # v270: ASR provider support tables (user_asr_settings)
    (lambda f: f.legacy.has_asr_settings, "v270_add_asr_provider_support"),
    # v270: profile avatar_path column
    (
        lambda f: (
            f.legacy.has_speaker_cluster
            and f.legacy.has_cluster_quality_metrics
            and f.legacy.has_avatar_path
        ),
        "v270_add_profile_avatar",
    ),
    # v260: cluster quality metrics (min_similarity, separation_score, margin)
    (
        lambda f: f.legacy.has_speaker_cluster and f.legacy.has_cluster_quality_metrics,
        "v260_add_cluster_quality_metrics",
    ),
    # v250: speaker clustering FK indexes
    (
        lambda f: f.legacy.has_speaker_cluster and f.legacy.has_speaker_clustering_indexes,
        "v250_add_speaker_clustering_indexes",
    ),
    # v230: auto-labeling support
    (lambda f: f.legacy.has_auto_labeling, "v230_add_auto_labeling"),
    # v220: speaker clustering tables
    (lambda f: f.legacy.has_speaker_cluster, "v220_add_speaker_clusters"),
    # v211: CHECK constraints and indexes on groups/sharing tables
    (
        lambda f: f.legacy.has_user_group and f.legacy.has_sharing_constraints,
        "v211_add_sharing_constraints_and_indexes",
    ),
    # v210: user groups and collection sharing tables
    (lambda f: f.legacy.has_user_group, "v210_add_groups_and_sharing"),
    # v200: schema reconciliation (jti on refresh_token + uuid on user_mfa)
    (
        lambda f: (
            f.legacy.has_collection_default_prompt
            and f.legacy.has_refresh_token_jti
            and f.legacy.has_mfa_uuid
        ),
        "v200_schema_reconciliation",
    ),
    # v190: default_summary_prompt_id column on collection table
    (lambda f: f.legacy.has_collection_default_prompt, "v190_add_collection_default_prompt"),
    # v180: speaker attribute detection columns
    (lambda f: f.legacy.has_speaker_attributes, "v180_add_speaker_attributes"),
    # v170: keycloak_refresh_token column for federated logout
    (lambda f: f.legacy.has_keycloak_refresh_token, "v170_add_keycloak_refresh_token"),
    # v160: allow_local_fallback column added to user table
    (lambda f: f.legacy.has_allow_local_fallback, "v160_add_allow_local_fallback"),
    # v140: words column added to transcript_segment
    (
        lambda f: (
            f.legacy.has_word_timestamps
            and f.legacy.has_varchar_status
            and not f.legacy.filestatus_enum_exists
        ),
        "v140_add_word_timestamps",
    ),
    # v073: status column is VARCHAR and no native enum exists
    # (This also matches fresh installs from init_db.sql which use VARCHAR)
    (
        lambda f: (
            f.legacy.has_varchar_status
            and not f.legacy.filestatus_enum_exists
            and f.legacy.has_segment_unique_constraint
        ),
        "v073_convert_filestatus_enum_to_varchar",
    ),
    (lambda f: f.legacy.has_queued_downloading_statuses, "v072_add_queued_downloading_statuses"),
    (
        lambda f: f.legacy.has_segment_unique_constraint,
        "v071_add_transcript_segment_unique_constraint",
    ),
    (lambda f: f.legacy.has_model_tracking, "v130_add_processing_model_tracking"),
    # The v120/v110/v100/v091/v090 markers can ALSO be present in older
    # init_db.sql-bootstrapped schemas (notably v0.3.3) that pre-date the
    # FedRAMP auth tables. Only treat the database as truly at v120/v110/etc
    # if the fedramp auth tables (created by v040) actually exist. Otherwise
    # the schema is inconsistent and we must stamp BEFORE v040 so the
    # missing tables get created.
    (
        lambda f: f.legacy.has_remaining_fk_indexes and f.legacy.has_fedramp,
        "v120_add_remaining_fk_indexes",
    ),
    (lambda f: f.legacy.has_fk_indexes and f.legacy.has_fedramp, "v110_add_missing_fk_indexes"),
    (
        lambda f: f.legacy.has_perf_indexes and f.legacy.has_fedramp,
        "v100_optimize_query_performance",
    ),
    (
        lambda f: f.legacy.has_suggestion_source and f.legacy.has_fedramp,
        "v091_add_speaker_suggestion_source",
    ),
    (lambda f: f.legacy.has_error_category and f.legacy.has_fedramp, "v090_add_error_category"),
    (lambda f: f.legacy.has_auth_config, "v080_add_auth_config"),
    (lambda f: f.legacy.has_pki_fingerprint, "v070_pki_security"),
    (lambda f: f.legacy.has_overlap_column, "v060_add_transcript_overlap"),
    (lambda f: f.legacy.has_fedramp and f.legacy.has_search_settings, "v050_add_search_settings"),
    (lambda f: f.legacy.has_fedramp, "v040_add_fedramp_compliance"),
    (lambda f: f.legacy.has_keycloak_pki, "v031_add_keycloak_pki_auth"),
    (lambda f: f.legacy.has_ldap, "v030_add_ldap_auth"),
    (lambda f: "system_settings" in f.tables, "v020_add_system_settings"),
)
