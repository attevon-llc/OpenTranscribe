"""Characterization of ``_detect_schema_version()``: every arm, in ladder order, with no database.

``run_migrations()`` stamps an *untracked* database at whatever revision this ladder returns, and
then lets alembic apply everything above it. Stamping too HIGH skips DDL the database never got;
stamping too LOW re-runs idempotent DDL. The per-revision consistency tests check the ladder
against one schema only, the fully-migrated test database, which every arm from the newest down
already matches: they cannot notice an arm that went missing, moved, or stopped matching.

These tests answer the ladder's probes from a fake connection (``_schema_detection_fake.py``)
that models a schema as a set of markers, so every arm can be reached and its position pinned:

* :func:`test_each_arm_is_reached_by_its_own_markers` - one minimal schema per pre-v367 arm, and
  :func:`test_an_older_arm_never_overtakes_its_neighbour` - each adjacent pair of them merged.
* :func:`test_the_modern_chain_detects_each_revision_it_has_applied` - the cumulative schema
  after each revision from v367 to the head, so every newer arm must lose to the older one
  exactly when its own marker is missing (this is the ordering test across eras).
* :func:`test_the_legacy_chain_stamps_what_it_stamped_before` - the cumulative pre-v367 chain,
  including the states that deliberately stamp LOWER than the revision they carry.
* :func:`test_every_probe_runs_oldest_era_first_before_any_verdict` - the probes' order.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import NamedTuple

import pytest

from tests.unit._schema_detection_fake import FakeSchema
from tests.unit._schema_detection_fake import Marker
from tests.unit._schema_detection_fake import UnknownProbeError
from tests.unit._schema_detection_fake import col
from tests.unit._schema_detection_fake import detect
from tests.unit._schema_detection_fake import state

_FEDRAMP = {"user_mfa"}
_VARCHAR_STATUS = ("column_type", "media_file", "status", "character varying")
_SEGMENT_UNIQUE = ("constraint", "uq_transcript_segment_content")
_CLUSTER = {"speaker_cluster"}
_QUALITY = col("speaker_cluster", "min_similarity")
_GROUPS = {"user_group", "user_group_member", "collection_share"}
_ORPHANS = {"upload_session", "speaker_audio_clip", "user_certificate_preferences"}

#: One minimal schema per pre-v367 arm, in ladder order: ``user`` plus that arm's markers.
_LEGACY_ARMS: list[tuple[str, FakeSchema]] = [
    ("v366_add_watch_sources", state({"watch_source"})),
    ("v365_add_prompt_shared_by", state(markers={col("summary_prompt", "shared_by")})),
    ("v364_add_content_redaction", state(markers={col("transcript_segment", "redactions")})),
    (
        "v352_add_requested_whisper_model",
        state(markers={col("media_file", "requested_whisper_model")}),
    ),
    (
        "v351_add_ai_summary_settings",
        state({"system_settings"}, {("setting", "ai.summary_enabled")}),
    ),
    ("v350_add_diarization_disabled", state(markers={col("media_file", "diarization_disabled")})),
    ("v340_add_user_media_sources", state({"user_media_source"})),
    ("v330_add_shared_configs_and_prompts", state(markers={col("user_llm_settings", "is_shared")})),
    ("v320_add_cluster_suggested_name", state(markers={col("speaker_cluster", "suggested_name")})),
    ("v310_add_speaker_constraints", state({"speaker_cannot_link"})),
    ("v300_add_gender_confirmed", state(markers={col("speaker", "gender_confirmed_by_user")})),
    ("v290_add_password_reset_tokens", state({"password_reset_token"})),
    ("v270_add_asr_provider_support", state({"user_asr_settings"})),
    ("v270_add_profile_avatar", state(_CLUSTER, {_QUALITY, col("speaker_profile", "avatar_path")})),
    ("v260_add_cluster_quality_metrics", state(_CLUSTER, {_QUALITY})),
    (
        "v250_add_speaker_clustering_indexes",
        state(_CLUSTER, {("index", "idx_speaker_cluster_member_speaker_id")}),
    ),
    ("v230_add_auto_labeling", state(markers={col("tag", "normalized_name")})),
    ("v220_add_speaker_clusters", state(_CLUSTER)),
    (
        "v211_add_sharing_constraints_and_indexes",
        state(_GROUPS, {("constraint", "_collection_share_permission_check")}),
    ),
    ("v210_add_groups_and_sharing", state(_GROUPS)),
    (
        "v200_schema_reconciliation",
        state(
            markers={
                col("collection", "default_summary_prompt_id"),
                col("refresh_token", "jti"),
                col("user_mfa", "uuid"),
            }
        ),
    ),
    (
        "v190_add_collection_default_prompt",
        state(markers={col("collection", "default_summary_prompt_id")}),
    ),
    ("v180_add_speaker_attributes", state(markers={col("speaker", "predicted_gender")})),
    ("v170_add_keycloak_refresh_token", state(markers={col("user", "keycloak_refresh_token")})),
    ("v160_add_allow_local_fallback", state(markers={col("user", "allow_local_fallback")})),
    (
        "v140_add_word_timestamps",
        state(markers={col("transcript_segment", "words"), _VARCHAR_STATUS}),
    ),
    ("v073_convert_filestatus_enum_to_varchar", state(markers={_VARCHAR_STATUS, _SEGMENT_UNIQUE})),
    (
        "v072_add_queued_downloading_statuses",
        state(markers={("enum_label", "filestatus", "queued")}),
    ),
    ("v071_add_transcript_segment_unique_constraint", state(markers={_SEGMENT_UNIQUE})),
    ("v130_add_processing_model_tracking", state(markers={col("media_file", "whisper_model")})),
    ("v120_add_remaining_fk_indexes", state(_FEDRAMP, {("index", "idx_speaker_user_id")})),
    ("v110_add_missing_fk_indexes", state(_FEDRAMP, {("index", "idx_comment_media_file_id")})),
    (
        "v100_optimize_query_performance",
        state(_FEDRAMP, {("index", "idx_media_file_user_status_upload")}),
    ),
    ("v091_add_speaker_suggestion_source", state(_FEDRAMP, {col("speaker", "suggestion_source")})),
    ("v090_add_error_category", state(_FEDRAMP, {col("media_file", "error_category")})),
    ("v080_add_auth_config", state({"auth_config"})),
    ("v070_pki_security", state(markers={col("user", "pki_fingerprint_sha256")})),
    ("v060_add_transcript_overlap", state(markers={col("transcript_segment", "is_overlap")})),
    (
        "v050_add_search_settings",
        state(_FEDRAMP | {"system_settings"}, {("setting", "search.embedding_model")}),
    ),
    ("v040_add_fedramp_compliance", state(_FEDRAMP)),
    ("v031_add_keycloak_pki_auth", state(markers={col("user", "keycloak_id")})),
    ("v030_add_ldap_auth", state(markers={col("user", "auth_type")})),
    ("v020_add_system_settings", state({"system_settings"})),
    ("v010_baseline", state()),
]


class Step(NamedTuple):
    """One revision applied to a cumulative schema, and what the ladder must stamp after it."""

    name: str
    expected: str
    tables: frozenset[str]
    markers: frozenset[Marker]
    drop_tables: frozenset[str]
    drop_markers: frozenset[Marker]


def step(
    name: str,
    expected: str | None = None,
    *,
    tables: Iterable[str] = (),
    markers: Iterable[Marker] = (),
    drop_tables: Iterable[str] = (),
    drop_markers: Iterable[Marker] = (),
) -> Step:
    return Step(
        name,
        expected or name,
        frozenset(tables),
        frozenset(markers),
        frozenset(drop_tables),
        frozenset(drop_markers),
    )


_V073 = "v073_convert_filestatus_enum_to_varchar"

#: The cumulative pre-v367 chain and what HEAD's ladder stamps after each step. v080-v130
#: stamp v073 - the legacy arms are single-marker, not cumulative, and v073's arm sits above
#: them - which is survivable only because the re-run revisions are idempotent. Pinned, not
#: endorsed.
_LEGACY_CHAIN: list[Step] = [
    step("v010", "v010_baseline", markers={("pg_type", "filestatus")}),
    step("v020", "v020_add_system_settings", tables={"system_settings"}),
    step("v030", "v030_add_ldap_auth", markers={col("user", "auth_type")}),
    step("v031", "v031_add_keycloak_pki_auth", markers={col("user", "keycloak_id")}),
    step("v040", "v040_add_fedramp_compliance", tables=_FEDRAMP),
    step("v050", "v050_add_search_settings", markers={("setting", "search.embedding_model")}),
    step("v060", "v060_add_transcript_overlap", markers={col("transcript_segment", "is_overlap")}),
    step("v070", "v070_pki_security", markers={col("user", "pki_fingerprint_sha256")}),
    step("v071", "v071_add_transcript_segment_unique_constraint", markers={_SEGMENT_UNIQUE}),
    step(
        "v072",
        "v072_add_queued_downloading_statuses",
        markers={("enum_label", "filestatus", "queued")},
    ),
    step(
        "v073",
        _V073,
        markers={_VARCHAR_STATUS},
        drop_markers={("pg_type", "filestatus"), ("enum_label", "filestatus", "queued")},
    ),
    step("v080", _V073, tables={"auth_config"}),
    step("v090", _V073, markers={col("media_file", "error_category")}),
    step("v091", _V073, markers={col("speaker", "suggestion_source")}),
    step("v100", _V073, markers={("index", "idx_media_file_user_status_upload")}),
    step("v110", _V073, markers={("index", "idx_comment_media_file_id")}),
    step("v120", _V073, markers={("index", "idx_speaker_user_id")}),
    step("v130", _V073, markers={col("media_file", "whisper_model")}),
    step("v140", "v140_add_word_timestamps", markers={col("transcript_segment", "words")}),
    step("v160", "v160_add_allow_local_fallback", markers={col("user", "allow_local_fallback")}),
    step(
        "v170",
        "v170_add_keycloak_refresh_token",
        markers={col("user", "keycloak_refresh_token")},
    ),
    step("v180", "v180_add_speaker_attributes", markers={col("speaker", "predicted_gender")}),
    step(
        "v190",
        "v190_add_collection_default_prompt",
        markers={col("collection", "default_summary_prompt_id")},
    ),
    step(
        "v200",
        "v200_schema_reconciliation",
        markers={col("refresh_token", "jti"), col("user_mfa", "uuid")},
    ),
    step("v210", "v210_add_groups_and_sharing", tables=_GROUPS),
    step(
        "v211",
        "v211_add_sharing_constraints_and_indexes",
        markers={("constraint", "_collection_share_permission_check")},
    ),
    step("v220", "v220_add_speaker_clusters", tables=_CLUSTER),
    step("v230", "v230_add_auto_labeling", markers={col("tag", "normalized_name")}),
    step(
        "v250",
        "v250_add_speaker_clustering_indexes",
        markers={("index", "idx_speaker_cluster_member_speaker_id")},
    ),
    step("v260", "v260_add_cluster_quality_metrics", markers={_QUALITY}),
    step("v270_avatar", "v270_add_profile_avatar", markers={col("speaker_profile", "avatar_path")}),
    step("v270_asr", "v270_add_asr_provider_support", tables={"user_asr_settings"}),
    step("v290", "v290_add_password_reset_tokens", tables={"password_reset_token"}),
    step("v300", "v300_add_gender_confirmed", markers={col("speaker", "gender_confirmed_by_user")}),
    step("v310", "v310_add_speaker_constraints", tables={"speaker_cannot_link"}),
    step(
        "v320",
        "v320_add_cluster_suggested_name",
        markers={col("speaker_cluster", "suggested_name")},
    ),
    step(
        "v330",
        "v330_add_shared_configs_and_prompts",
        markers={col("user_llm_settings", "is_shared")},
    ),
    step("v340", "v340_add_user_media_sources", tables={"user_media_source"}),
    step(
        "v350", "v350_add_diarization_disabled", markers={col("media_file", "diarization_disabled")}
    ),
    step("v351", "v351_add_ai_summary_settings", markers={("setting", "ai.summary_enabled")}),
    step(
        "v352",
        "v352_add_requested_whisper_model",
        markers={col("media_file", "requested_whisper_model")},
    ),
    step("v364", "v364_add_content_redaction", markers={col("transcript_segment", "redactions")}),
    step("v365", "v365_add_prompt_shared_by", markers={col("summary_prompt", "shared_by")}),
    step("v366", "v366_add_watch_sources", tables={"watch_source"}),
]

#: Present in a pre-v367 schema and REMOVED by a modern revision (or data a data-only revision
#: rewrites). The modern chain starts from the full legacy chain plus these.
_MODERN_BASE_TABLES = _ORPHANS | {
    "auth_config_audit",
    "speaker",
    "speaker_profile",
    "speaker_match",
    "user_setting",
    "user_diarization_settings",
}
_MODERN_BASE_MARKERS = {
    ("legacy_varchar_uuid",),
    ("legacy_oidc_config_keys",),
    ("constraint", "users_role_check"),
    ("data", "v434_reset_unusable_pyannote_source"),
    ("data", "v436_lite_local_speaker_detection_to_automatic"),
    ("data", "v439_infer_legacy_voiceprint_suggestion_source"),
}

#: The cumulative chain from v367 to the head. Each state must detect exactly its own revision.
_MODERN_CHAIN: list[Step] = [
    step("v367_add_cloud_seams", tables={"organization"}),
    step("v368_uuid_native_type_guard", drop_markers={("legacy_varchar_uuid",)}),
    step(
        "v369_superuser_role_invariant",
        markers={("constraint", "ck_user_superuser_matches_role")},
    ),
    step("v370_add_media_file_quarantine", markers={col("media_file", "is_quarantined")}),
    step(
        "v371_repair_cloud_seams_columns",
        markers={col("media_file", "pre_quarantine_status"), col("user", "external_id")},
    ),
    step("v372_add_audit_organization_id", markers={col("watch_source", "organization_id")}),
    step("v373_add_cluster_organization_id", markers={col("speaker_cluster", "organization_id")}),
    step("v374_add_tag_user_id", markers={col("tag", "user_id")}),
    step("v375_add_chat_tables", tables={"chat_conversation"}),
    step("v376_add_chat_projects", tables={"chat_project"}),
    step(
        "v377_harden_user_auth_invariants",
        tables={"user_invitation"},
        markers={("constraint", "ck_user_auth_type_valid")},
    ),
    step(
        "v378_idp_group_mapping",
        tables={"group_mapping"},
        markers={col("user_group_member", "source")},
    ),
    step("v379_rename_keycloak_config_to_oidc", drop_markers={("legacy_oidc_config_keys",)}),
    step(
        "v380_oidc_identity_columns",
        markers={
            col("user", "oidc_subject"),
            col("user", "oidc_refresh_token"),
            col("refresh_token", "oidc_id_token"),
        },
    ),
    step(
        "v381_approval_state",
        markers={col("user", "approval_status"), ("constraint", "ck_user_approval_status_valid")},
    ),
    step(
        "v382_scim_tokens",
        tables={"scim_token"},
        markers={("constraint_def", "ck_group_mapping_source_valid", "proxy")},
    ),
    step(
        "v383_saml_auth_type",
        markers={
            col("user", "saml_subject"),
            ("constraint_def", "ck_user_auth_type_valid", "saml"),
        },
    ),
    step("v384_add_chat_reasoning_content", markers={col("chat_message", "reasoning_content")}),
    step("v385_drop_orphan_tables", drop_tables=_ORPHANS),
    step("v386_add_tag_share", tables={"tag_share"}),
    step(
        "v387_actor_fks_and_tag_share_check",
        markers={
            ("constraint_set_null", "auth_config_audit_changed_by_fkey"),
            ("constraint", "_tag_share_target_type_check"),
        },
        drop_markers={("constraint", "users_role_check")},
    ),
    step("v388_add_user_group_organization_id", markers={col("user_group", "organization_id")}),
    step(
        "v389_add_erasure_ledger",
        tables={"erasure_ledger"},
        markers={("constraint", "ck_erasure_ledger_counters_numeric")},
    ),
    step("v390_add_file_facts", tables={"file_facts"}),
    step(
        "v391_add_recorded_date_provenance",
        markers={("constraint", "ck_media_file_recorded_date_provenance")},
    ),
    step("v392_add_redaction_coverage", markers={col("media_file", "redaction_coverage")}),
    step(
        "v393_add_overlap_timing_columns",
        markers={col("file_pipeline_timing", "transcript_ready_ms")},
    ),
    step(
        "v397_add_platform_super_admin_link_authorized",
        markers={col("user", "platform_super_admin_link_authorized")},
    ),
    step("v420_add_tag_organization_id", markers={("index", "uq_tag_org_name")}),
    step("v421_add_media_playback_path", markers={col("media_file", "playback_path")}),
    step("v422_add_collection_tenancy", markers={("index", "uq_collection_org_name")}),
    step(
        "v430_per_tenant_speaker_and_vocab_names",
        markers={("index", "uq_speaker_profile_user_tenant_name")},
    ),
    step("v431_add_media_duration_provenance", markers={col("media_file", "duration_source")}),
    step(
        "v432_support_access_grant",
        tables={"support_access_grant"},
        markers={("constraint", "ck_support_access_grant_target_kind")},
    ),
    step(
        "v433_add_requested_transcription_options",
        markers={col("media_file", "requested_disable_diarization")},
    ),
    step(
        "v434_reset_unusable_pyannote_source",
        drop_markers={("data", "v434_reset_unusable_pyannote_source")},
    ),
    step(
        "v436_lite_local_speaker_detection_to_automatic",
        drop_markers={("data", "v436_lite_local_speaker_detection_to_automatic")},
    ),
    step(
        "v437_add_media_processing_provenance",
        markers={col("media_file", "processing_provenance")},
    ),
    step("v438_add_speaker_correction_carryover", tables={"speaker_correction_carryover"}),
    step(
        "v439_infer_legacy_voiceprint_suggestion_source",
        drop_markers={("data", "v439_infer_legacy_voiceprint_suggestion_source")},
    ),
    step(
        "v440_add_password_last_self_changed_at",
        markers={col("user", "password_last_self_changed_at")},
    ),
    step("v441_add_legal_acceptance", tables={"legal_acceptance"}),
    step(
        "v442_add_media_file_takedown_origin",
        markers={("trigger", "trg_media_file_takedown_origin")},
    ),
    step(
        "v443_add_takedown_notice_tables",
        markers={("constraint", "ck_takedown_notice_file_outcome_at")},
    ),
    step(
        "v444_add_takedown_notification",
        markers={("constraint", "ck_takedown_notification_status")},
    ),
    step(
        "v445_add_organization_policy",
        markers={("constraint", "ck_organization_policy_floor_custom_words_array")},
    ),
    step("v446_add_collection_visibility", markers={("constraint", "ck_collection_visibility")}),
    step("v447_add_content_hold", markers={("constraint", "ck_content_hold_collection_scope")}),
    step(
        "v448_task_one_active_row_per_file_step",
        markers={("index", "uq_task_active_per_file_type")},
    ),
    step(
        "v449_add_speaker_identity_source",
        markers={("constraint", "speaker_identity_source_user_id_fkey")},
    ),
]


def _apply(steps: list[Step], tables: set[str], markers: set[Marker]) -> list[FakeSchema]:
    states = []
    for s in steps:
        tables = (tables | s.tables) - s.drop_tables
        markers = (markers | s.markers) - s.drop_markers
        states.append(FakeSchema(tables=set(tables), markers=set(markers)))
    return states


def _legacy_chain_states() -> list[tuple[str, FakeSchema, str]]:
    states = _apply(_LEGACY_CHAIN, {"user"}, set())
    return [(s.name, schema, s.expected) for s, schema in zip(_LEGACY_CHAIN, states, strict=True)]


def _full_legacy_schema() -> FakeSchema:
    return _legacy_chain_states()[-1][1]


def _modern_chain_states() -> list[tuple[str, FakeSchema]]:
    legacy = _full_legacy_schema()
    states = _apply(
        _MODERN_CHAIN,
        legacy.tables | _MODERN_BASE_TABLES,
        legacy.markers | _MODERN_BASE_MARKERS,
    )
    return [(s.name, schema) for s, schema in zip(_MODERN_CHAIN, states, strict=True)]


@pytest.fixture
def lite(monkeypatch):
    """v436's probe only runs on lite; the modern chain runs there so its arm is reachable."""
    monkeypatch.setenv("DEPLOYMENT_MODE", "lite")


def test_no_user_table_means_nothing_to_stamp():
    schema = FakeSchema(tables={"system_settings", "media_file"})
    assert detect(schema) is None
    assert schema.queries == [], "an empty-of-users database must not be probed at all"


@pytest.mark.parametrize(
    ("revision", "schema"), _LEGACY_ARMS, ids=[revision for revision, _ in _LEGACY_ARMS]
)
def test_each_arm_is_reached_by_its_own_markers(revision, schema):
    assert detect(schema) == revision


@pytest.mark.parametrize(
    ("higher", "lower"),
    list(zip(_LEGACY_ARMS, _LEGACY_ARMS[1:], strict=False)),
    ids=[f"{a}-over-{b}" for (a, _), (b, _) in zip(_LEGACY_ARMS, _LEGACY_ARMS[1:], strict=False)],
)
def test_an_older_arm_never_overtakes_its_neighbour(higher, lower):
    """Neighbours' markers together still stamp the arm the ladder tries first.

    The pre-v367 arms are single-marker, so their own schemas never overlap and the test
    above cannot see two of them trade places. Some pairs cannot co-exist in a real database
    (v072's enum label without v073's dropped enum); the order is pinned regardless.
    """
    (revision, upper), (_, under) = higher, lower
    schema = FakeSchema(tables=upper.tables | under.tables, markers=upper.markers | under.markers)
    assert detect(schema) == revision


@pytest.mark.parametrize(
    ("step_name", "schema", "expected"),
    _legacy_chain_states(),
    ids=[s.name for s in _LEGACY_CHAIN],
)
def test_the_legacy_chain_stamps_what_it_stamped_before(step_name, schema, expected):
    assert detect(schema) == expected


@pytest.mark.parametrize(
    ("revision", "schema"),
    _modern_chain_states(),
    ids=[s.name for s in _MODERN_CHAIN],
)
def test_the_modern_chain_detects_each_revision_it_has_applied(lite, revision, schema):
    assert detect(schema) == revision


def test_every_ladder_revision_is_pinned_by_a_test_above():
    """Guard the guard: an arm with no state here could move or vanish unnoticed."""
    pinned = {revision for revision, _ in _LEGACY_ARMS} | {s.name for s in _MODERN_CHAIN}
    assert {s.expected for s in _LEGACY_CHAIN} <= pinned
    # 43 pre-v367 arms plus the v010 fallback, and one arm per revision from v367 on.
    assert len(_LEGACY_ARMS) == 44
    assert len(_MODERN_CHAIN) == 50
    assert len(pinned) == 94


def test_on_the_full_image_the_lite_only_rows_are_ignored(monkeypatch):
    """v436 is a no-op off lite, so its rows must not hold a database below v436 there."""
    monkeypatch.setenv("DEPLOYMENT_MODE", "full")
    states = dict(_modern_chain_states())
    schema = states["v434_reset_unusable_pyannote_source"]
    assert ("data", "v436_lite_local_speaker_detection_to_automatic") in schema.markers
    assert detect(schema) == "v436_lite_local_speaker_detection_to_automatic"


def test_the_newest_arm_wins_over_every_older_fingerprint(lite):
    """The head schema carries every legacy marker too; the ladder still says head."""
    head_revision, head = _modern_chain_states()[-1]
    head.markers |= {("enum_label", "filestatus", "queued"), _SEGMENT_UNIQUE}
    assert detect(head) == head_revision


def test_cloud_seams_outrank_every_pre_seams_marker():
    schema = _full_legacy_schema()
    schema.tables.add("organization")
    assert detect(schema) == "v368_uuid_native_type_guard", "no varchar uuid column left"
    schema.markers.add(("legacy_varchar_uuid",))
    assert detect(schema) == "v367_add_cloud_seams"


def test_v386_is_not_shadowed_by_v385_nor_v387(lite):
    states = dict(_modern_chain_states())
    v386 = states["v386_add_tag_share"]
    v386.markers.add(("constraint_set_null", "auth_config_audit_changed_by_fkey"))
    assert detect(v386) == "v386_add_tag_share", "half of v387 must not stamp v387"


def test_a_ledger_without_its_check_is_not_v389(lite):
    states = dict(_modern_chain_states())
    schema = states["v388_add_user_group_organization_id"]
    schema.tables.add("erasure_ledger")
    assert detect(schema) == "v388_add_user_group_organization_id"
    assert detect(states["v389_add_erasure_ledger"]) == "v389_add_erasure_ledger"


def test_a_data_only_revision_holds_every_newer_arm_back(lite):
    """v439's rows still present: even a head-shaped schema re-runs from v438."""
    head = _modern_chain_states()[-1][1]
    head.markers.add(("data", "v439_infer_legacy_voiceprint_suggestion_source"))
    assert detect(head) == "v438_add_speaker_correction_carryover"


def test_the_fedramp_tables_gate_the_v090_to_v120_markers():
    """A v0.3.3 init_db.sql schema carries v120's index without v040's tables."""
    schema = state({"auth_config"}, {("index", "idx_speaker_user_id")})
    assert detect(schema) == "v080_add_auth_config"


def test_the_varchar_status_shadows_the_model_tracking_column():
    schema = state(markers={_VARCHAR_STATUS, _SEGMENT_UNIQUE, col("media_file", "whisper_model")})
    assert detect(schema) == _V073


def test_settings_rows_are_only_read_when_the_table_exists():
    """Querying system_settings on a database without it would abort the transaction."""
    schema = state(_FEDRAMP)
    assert detect(schema) == "v040_add_fedramp_compliance"
    assert not any("FROM system_settings" in sql for sql in schema.queries)


def test_every_probe_runs_oldest_era_first_before_any_verdict(lite):
    """The probes' order is part of the contract, not just the arms'.

    A probe that fails aborts the transaction and with it the detection, so WHICH probes run
    and in what order decides whether a damaged database fails detection or is stamped. The
    ladder has always issued every probe before testing any arm, oldest revision first.
    """
    head = _modern_chain_states()[-1][1]
    detect(head)
    anchors = [
        "column_name='auth_type'",  # v030, the first probe
        "table_name = 'organization'",  # v367
        "conname = 'ck_user_auth_type_valid')",  # v377
        "conname = 'ck_erasure_ledger_counters_numeric'",  # v389
        "FROM user_setting s",  # v434
        "conname = 'speaker_identity_source_user_id_fkey'",  # v449, the last probe
    ]
    positions = [next(i for i, sql in enumerate(head.queries) if a in sql) for a in anchors]
    assert positions == sorted(positions), f"probe eras ran out of order: {positions}"
    assert positions[0] == 0
    assert positions[-1] == len(head.queries) - 1

    baseline = state()
    assert detect(baseline) == "v010_baseline"
    remaining = iter(head.queries)
    assert all(sql in remaining for sql in baseline.queries), (
        "a v010 database must be asked the same probes, in the same order, as a head one "
        "(only the table-guarded probes may be skipped)"
    )


def test_an_unknown_probe_fails_loudly():
    """The fake must refuse a query it cannot classify, or this file proves nothing."""
    with pytest.raises(UnknownProbeError):
        FakeSchema().answer("SELECT EXISTS(SELECT 1 FROM pg_class WHERE relname = 'x')")
