"""Schema detection, v389-v433: erasure ledger, ingest artifacts, tenancy, support access.

The erasure ledger, file facts, recorded-date provenance, redaction coverage, overlap timing,
the super-admin link flag, tenant-owned tags/collections/names, duration provenance,
support-access grants and the per-file transcription request. ``matches_v389`` and
``matches_v433`` are the shared fingerprints the newer arms build on.

``probe()`` issues this era's queries in the order the ladder always has; ``ARMS`` is this
era's slice of the newest-first ladder (``ladder.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from app.db.schema_detect.cloud_seams import matches_v376
from app.db.schema_detect.probe import check_exists

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

    from app.db.schema_detect.ladder import Arm
    from app.db.schema_detect.ladder import SchemaFacts


@dataclass(frozen=True)
class IngestTenancyFacts:
    """Probe answers for v389-v433."""

    has_erasure_ledger: bool
    has_file_facts: bool
    has_recorded_date_provenance: bool
    has_redaction_coverage: bool
    has_overlap_timing_columns: bool
    has_platform_super_admin_link_authorized: bool
    has_tag_org_unique: bool
    has_media_playback_path: bool
    has_collection_org_unique: bool
    has_speaker_profile_tenant_unique: bool
    has_duration_source: bool
    has_support_access_grant: bool
    has_requested_options: bool


def probe(conn: Connection, tables: list[str]) -> IngestTenancyFacts:
    """Run this era's probes against *conn*, in ladder order."""
    _check_exists = partial(check_exists, conn)
    return IngestTenancyFacts(
        # v389: the GDPR Art. 17 erasure ledger. Probed on the CHECK rather than on the
        # table, because the constraint is the part that carries the guarantee — a hand-made
        # `erasure_ledger` without `ck_erasure_ledger_counters_numeric` is a table that can
        # store the personal data the ledger exists not to retain, and re-running the
        # revision is exactly the right repair for it.
        has_erasure_ledger="erasure_ledger" in tables
        and _check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_constraint "
            "WHERE conname = 'ck_erasure_ledger_counters_numeric')"
        ),
        # v390: file_facts — the deterministic ingest artifacts (#383 Phase 2). Probed on the
        # table rather than on a column, because the whole revision is one CREATE TABLE.
        has_file_facts="file_facts" in tables,
        # v391: recorded_date + its provenance on media_file. Probed on the CHECK rather than
        # on the column deliberately — the constraint is the half of the revision that carries
        # the design (a date with no source is unrepresentable), and keying on it means a
        # database left with the columns but not the CHECKs by a partial run falls through to
        # v390 and re-runs v391, which the idempotent DDL handles. Keying on the column would
        # stamp that database as done and leave the rule unenforced.
        has_recorded_date_provenance=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_constraint "
            "WHERE conname = 'ck_media_file_recorded_date_provenance')"
        ),
        # v392: media_file.redaction_coverage — which detectors a finished scan actually ran.
        # Single-marker revision (one ADD COLUMN, no constraint), so unlike v391 there is no
        # rule to key on and the column IS the fingerprint.
        has_redaction_coverage=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'media_file' AND column_name = 'redaction_coverage')"
        ),
        # v393: file_pipeline_timing's transcribe/diarize overlap markers. Three ADD COLUMNs,
        # no constraint, so a column IS the fingerprint — the same shape v392's
        # has_redaction_coverage uses. `transcript_ready_ms` is the probe rather than one of
        # the two diarize columns only because it is the one the revision's own docstring
        # calls the perceived-latency marker; all three land in one statement, so any of them
        # would do.
        has_overlap_timing_columns=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'file_pipeline_timing' AND column_name = 'transcript_ready_ms')"
        ),
        # v397: user.platform_super_admin_link_authorized — the escape hatch for
        # account_linking's super_admin JIT-link refusal (issue #993). Single ADD
        # COLUMN, no constraint, so the column is the fingerprint — same shape as v392's
        # has_redaction_coverage / v393's has_overlap_timing_columns. Numbered v397, not
        # v394: see the revision file's docstring for why (a reserved-but-unmerged range).
        has_platform_super_admin_link_authorized=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'user' AND column_name = 'platform_super_admin_link_authorized')"
        ),
        # v420: tenant-owned tags (issue #1050). Keyed on uq_tag_org_name rather than on
        # tag.organization_id: the index is created LAST, after the backfill, so a run that
        # died mid-backfill leaves the column without it and correctly re-runs v420.
        has_tag_org_unique=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_indexes WHERE indexname = 'uq_tag_org_name')"
        ),
        # v421: media_file.playback_path, the browser-playable rendition of an original no
        # browser decodes. Single ADD COLUMN, so the column is the fingerprint.
        has_media_playback_path=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'media_file' AND column_name = 'playback_path')"
        ),
        # v422: tenant-owned collections (issue #1051). Keyed on uq_collection_org_name,
        # created LAST (after the backfill), for the same reason as v420's marker.
        has_collection_org_unique=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_indexes WHERE indexname = 'uq_collection_org_name')"
        ),
        # v430: per-tenant unique names on speaker profiles, speaker collections and
        # vocabulary (issue #1110). Keyed on uq_speaker_profile_user_tenant_name, the index
        # the revision creates LAST (after the tenant-stamp backfill).
        has_speaker_profile_tenant_unique=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_indexes "
            "WHERE indexname = 'uq_speaker_profile_user_tenant_name')"
        ),
        # v431: media_file.duration_source — where `duration` came from (issue #969).
        # Single-marker revision (one ADD COLUMN plus one CHECK, no backfill), so the
        # column IS the fingerprint — the same shape v392/v393 use.
        has_duration_source=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'media_file' AND column_name = 'duration_source')"
        ),
        # v432: support-access grants (issue #1122). Keyed on the CHECK the revision creates
        # LAST, so a database interrupted part-way through the revision does not look finished
        # and is re-run (every statement in it is idempotent).
        has_support_access_grant="support_access_grant" in tables
        and _check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_constraint "
            "WHERE conname = 'ck_support_access_grant_target_kind')"
        ),
        # v433: the per-file transcription request (issues #1203, #1202, #1198). Keyed on the
        # last column the revision adds; the watch-source default drop has no marker of its own.
        has_requested_options=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'media_file' AND column_name = 'requested_disable_diarization')"
        ),
    )


# v389: same as v388 plus the erasure ledger. Purely additive, so — like v388 over
# v387 — the older arm needs no `not has_erasure_ledger` exclusion: this arm is
# strictly more specific and the ladder returns the FIRST match.
#
# Bound to a name rather than inlined four times: v390-v392 are each v389's
# fingerprint plus one more probe, and a copy-pasted 30-term conjunction is a place
# for them to drift apart silently.
def matches_v389(f: SchemaFacts) -> bool:
    return (
        matches_v376(f)
        and f.auth.has_auth_type_check
        and f.auth.has_user_invitation
        and f.auth.has_group_mapping
        and f.auth.has_membership_source
        and not f.auth.has_legacy_oidc_config_keys
        and f.auth.has_oidc_subject
        and f.auth.has_oidc_user_refresh_token
        and f.auth.has_session_id_token
        and f.auth.has_user_approval_status
        and f.auth.has_approval_status_check
        and f.auth.has_scim_token
        and f.auth.has_proxy_group_source
        and f.auth.has_saml_subject
        and f.auth.has_saml_auth_type_check
        and f.auth.has_chat_reasoning_content
        and not f.auth.has_orphan_tables
        and f.auth.has_tag_share
        and f.auth.has_actor_fk_set_null
        and f.auth.has_tag_share_type_check
        and not f.auth.has_legacy_role_check
        and f.auth.has_user_group_org
        and f.tenancy.has_erasure_ledger
    )


def matches_v433(f: SchemaFacts) -> bool:
    return (
        matches_v389(f)
        and f.tenancy.has_file_facts
        and f.tenancy.has_recorded_date_provenance
        and f.tenancy.has_redaction_coverage
        and f.tenancy.has_overlap_timing_columns
        and f.tenancy.has_platform_super_admin_link_authorized
        and f.tenancy.has_tag_org_unique
        and f.tenancy.has_media_playback_path
        and f.tenancy.has_collection_org_unique
        and f.tenancy.has_speaker_profile_tenant_unique
        and f.tenancy.has_duration_source
        and f.tenancy.has_support_access_grant
        and f.tenancy.has_requested_options
    )


ARMS: tuple[Arm, ...] = (
    # v433: same as v432 plus the requested per-file transcription options.
    (lambda f: matches_v433(f), "v433_add_requested_transcription_options"),
    # v432: same as v431 plus the support-access tables.
    (
        lambda f: (
            matches_v389(f)
            and f.tenancy.has_file_facts
            and f.tenancy.has_recorded_date_provenance
            and f.tenancy.has_redaction_coverage
            and f.tenancy.has_overlap_timing_columns
            and f.tenancy.has_platform_super_admin_link_authorized
            and f.tenancy.has_tag_org_unique
            and f.tenancy.has_media_playback_path
            and f.tenancy.has_collection_org_unique
            and f.tenancy.has_speaker_profile_tenant_unique
            and f.tenancy.has_duration_source
            and f.tenancy.has_support_access_grant
        ),
        "v432_support_access_grant",
    ),
    # v431: same as v430 plus media_file.duration_source.
    (
        lambda f: (
            matches_v389(f)
            and f.tenancy.has_file_facts
            and f.tenancy.has_recorded_date_provenance
            and f.tenancy.has_redaction_coverage
            and f.tenancy.has_overlap_timing_columns
            and f.tenancy.has_platform_super_admin_link_authorized
            and f.tenancy.has_tag_org_unique
            and f.tenancy.has_media_playback_path
            and f.tenancy.has_collection_org_unique
            and f.tenancy.has_speaker_profile_tenant_unique
            and f.tenancy.has_duration_source
        ),
        "v431_add_media_duration_provenance",
    ),
    # v430: same as v422 plus the per-tenant speaker/vocabulary uniqueness.
    (
        lambda f: (
            matches_v389(f)
            and f.tenancy.has_file_facts
            and f.tenancy.has_recorded_date_provenance
            and f.tenancy.has_redaction_coverage
            and f.tenancy.has_overlap_timing_columns
            and f.tenancy.has_platform_super_admin_link_authorized
            and f.tenancy.has_tag_org_unique
            and f.tenancy.has_media_playback_path
            and f.tenancy.has_collection_org_unique
            and f.tenancy.has_speaker_profile_tenant_unique
        ),
        "v430_per_tenant_speaker_and_vocab_names",
    ),
    # v422: same as v421 plus the per-tenant collection uniqueness.
    (
        lambda f: (
            matches_v389(f)
            and f.tenancy.has_file_facts
            and f.tenancy.has_recorded_date_provenance
            and f.tenancy.has_redaction_coverage
            and f.tenancy.has_overlap_timing_columns
            and f.tenancy.has_platform_super_admin_link_authorized
            and f.tenancy.has_tag_org_unique
            and f.tenancy.has_media_playback_path
            and f.tenancy.has_collection_org_unique
        ),
        "v422_add_collection_tenancy",
    ),
    # v421: same as v420 plus media_file.playback_path.
    (
        lambda f: (
            matches_v389(f)
            and f.tenancy.has_file_facts
            and f.tenancy.has_recorded_date_provenance
            and f.tenancy.has_redaction_coverage
            and f.tenancy.has_overlap_timing_columns
            and f.tenancy.has_platform_super_admin_link_authorized
            and f.tenancy.has_tag_org_unique
            and f.tenancy.has_media_playback_path
        ),
        "v421_add_media_playback_path",
    ),
    # v420: same as v397 plus the per-tenant tag uniqueness.
    (
        lambda f: (
            matches_v389(f)
            and f.tenancy.has_file_facts
            and f.tenancy.has_recorded_date_provenance
            and f.tenancy.has_redaction_coverage
            and f.tenancy.has_overlap_timing_columns
            and f.tenancy.has_platform_super_admin_link_authorized
            and f.tenancy.has_tag_org_unique
        ),
        "v420_add_tag_organization_id",
    ),
    # v397: same as v393 plus user.platform_super_admin_link_authorized.
    (
        lambda f: (
            matches_v389(f)
            and f.tenancy.has_file_facts
            and f.tenancy.has_recorded_date_provenance
            and f.tenancy.has_redaction_coverage
            and f.tenancy.has_overlap_timing_columns
            and f.tenancy.has_platform_super_admin_link_authorized
        ),
        "v397_add_platform_super_admin_link_authorized",
    ),
    # v393: same as v392 plus file_pipeline_timing's transcribe/diarize overlap markers.
    (
        lambda f: (
            matches_v389(f)
            and f.tenancy.has_file_facts
            and f.tenancy.has_recorded_date_provenance
            and f.tenancy.has_redaction_coverage
            and f.tenancy.has_overlap_timing_columns
        ),
        "v393_add_overlap_timing_columns",
    ),
    # v392: same as v391 plus the redaction-coverage column.
    (
        lambda f: (
            matches_v389(f)
            and f.tenancy.has_file_facts
            and f.tenancy.has_recorded_date_provenance
            and f.tenancy.has_redaction_coverage
        ),
        "v392_add_redaction_coverage",
    ),
    # v391: same as v390 plus the recorded-date provenance rule.
    (
        lambda f: (
            matches_v389(f) and f.tenancy.has_file_facts and f.tenancy.has_recorded_date_provenance
        ),
        "v391_add_recorded_date_provenance",
    ),
    # v390: same as v389 plus the ingest-artifacts sidecar.
    (lambda f: matches_v389(f) and f.tenancy.has_file_facts, "v390_add_file_facts"),
    (lambda f: matches_v389(f), "v389_add_erasure_ledger"),
)
