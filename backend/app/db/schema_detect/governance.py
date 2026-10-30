"""Schema detection, v434-v449: data-only resets, provenance, takedowns, policy, holds.

The data-only revisions (v434, v436, v439) key on the ABSENCE of the rows they rewrite, read
through each revision's own ``DETECT_SQL`` so detection and rewrite cannot drift apart. Then
processing provenance, correction carry-over, the password-age column, legal acceptance, the
takedown origin/notice/notification tables, organization policy, collection visibility,
content holds, the one-active-task guard and speaker identity source.

``probe()`` issues this era's queries in the order the ladder always has; ``ARMS`` is this
era's slice of the newest-first ladder (``ladder.py``), and its first arm is the top of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import text

from app.db.schema_detect.ingest_tenancy import matches_v433
from app.db.schema_detect.probe import check_exists

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

    from app.db.schema_detect.ladder import Arm
    from app.db.schema_detect.ladder import SchemaFacts


def _revision_module(revision: str):
    """Load one revision file by path (its module-level constants are the source of truth)."""
    import importlib.util

    path = Path(__file__).resolve().parents[3] / "alembic" / "versions" / f"{revision}.py"
    spec = importlib.util.spec_from_file_location(revision, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load revision {revision} from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _has_unusable_pyannote_source(conn, tables: list[str]) -> bool:
    """v434's data marker: a stored pyannote.ai choice with no key and local transcription.

    The predicate is read from the revision itself, so detection and the reset it detects
    cannot drift apart.
    """
    if not {"user_setting", "user_diarization_settings", "user_asr_settings"} <= set(tables):
        return False
    module = _revision_module("v434_reset_unusable_pyannote_source")
    detect = text(module.DETECT_SQL)
    return bool(conn.execute(detect, {"env_local": module.env_asr_is_local()}).scalar())


def _has_lite_local_source_with_speakers(conn, tables: list[str]) -> bool:
    """v436's data marker: on lite, a stored "this server" choice Automatic would keep.

    Always False on the full image, where the revision is a no-op. The predicate is read
    from the revision itself, so detection and the rewrite cannot drift apart.
    """
    if not {"user_setting", "user_asr_settings"} <= set(tables):
        return False
    module = _revision_module("v436_lite_local_speaker_detection_to_automatic")
    if not module.deployment_is_lite():
        return False
    return bool(conn.execute(text(module.DETECT_SQL)).scalar())


def _has_unstamped_voiceprint_suggestion(conn, tables: list[str]) -> bool:
    """v439's data marker: a legacy suggestion that provably came from a voiceprint match.

    The predicate is read from the revision itself, so detection and the stamp it detects
    cannot drift apart.
    """
    if not {"speaker", "speaker_profile", "speaker_match"} <= set(tables):
        return False
    module = _revision_module("v439_infer_legacy_voiceprint_suggestion_source")
    return bool(conn.execute(text(module.DETECT_SQL)).scalar())


@dataclass(frozen=True)
class GovernanceFacts:
    """Probe answers for v434-v449."""

    has_unusable_pyannote_source: bool
    has_lite_local_source: bool
    has_processing_provenance: bool
    has_correction_carryover: bool
    has_unstamped_voiceprint_suggestion: bool
    has_password_last_self_changed: bool
    has_legal_acceptance: bool
    has_takedown_origin: bool
    has_takedown_notice: bool
    has_takedown_notification: bool
    has_organization_policy: bool
    has_collection_visibility: bool
    has_content_hold: bool
    has_task_active_unique: bool
    has_identity_source: bool


def probe(conn: Connection, tables: list[str]) -> GovernanceFacts:
    """Run this era's probes against *conn*, in ladder order."""
    _check_exists = partial(check_exists, conn)
    return GovernanceFacts(
        # v434: a data-only revision (issue #1225). No schema marker, so like v379 it keys on
        # the ABSENCE of the rows it rewrites: a database that still holds a pyannote.ai choice
        # with no key under local transcription is stamped v433 and re-runs the reset.
        has_unusable_pyannote_source=_has_unusable_pyannote_source(conn, tables),
        # v436: data-only and lite-only (issue #1228). Keyed on the ABSENCE of a lite user's
        # stored "this server" choice that Automatic would keep, like v434.
        has_lite_local_source=_has_lite_local_source_with_speakers(conn, tables),
        # v437: per-file processing provenance (issue #1229). Single-marker revision: the
        # column IS the fingerprint.
        has_processing_provenance=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'media_file' AND column_name = 'processing_provenance')"
        ),
        # v438: speaker corrections carried across a re-run (issue #1233). Single-marker
        # revision: the table IS the fingerprint.
        has_correction_carryover="speaker_correction_carryover" in tables,
        # v439: data-only (owner decision 2026-10-12). Keyed on the ABSENCE of a legacy
        # suggestion it would stamp with its voiceprint source, like v434.
        has_unstamped_voiceprint_suggestion=_has_unstamped_voiceprint_suggestion(conn, tables),
        # v440: when the account holder last chose their own password. Single-marker
        # revision: the column IS the fingerprint.
        has_password_last_self_changed=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'user' AND column_name = 'password_last_self_changed_at')"
        ),
        # v441: versioned legal-document acceptances. Single-marker revision: the table IS the
        # fingerprint (its UNIQUE key is part of the same CREATE, so a table without it cannot
        # exist from this revision).
        has_legal_acceptance="legal_acceptance" in tables,
        # v442: which authority holds a taken-down file (issue #1253). Keyed on the TRIGGER, which
        # the revision creates last: a column without the trigger is the state that should re-run
        # the revision, because only the trigger keeps a held row from having no origin.
        has_takedown_origin=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_trigger WHERE tgname = 'trg_media_file_takedown_origin' "
            "AND NOT tgisinternal)"
        ),
        # v443: takedown notice records (issue #1254). Keyed on the LAST-created constraint, not on
        # the tables: a table without its CHECKs is the state that must re-run the revision.
        has_takedown_notice=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_constraint "
            "WHERE conname = 'ck_takedown_notice_file_outcome_at')"
        ),
        # v444: takedown notification delivery log. Same reasoning: keyed on its last CHECK.
        has_takedown_notification=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_constraint "
            "WHERE conname = 'ck_takedown_notification_status')"
        ),
        # v445: per-organization privacy policy + redaction floor (issues #1280, #1279). Keyed on
        # the LAST-created CHECK, not the table: a table without its CHECKs must re-run the
        # revision.
        has_organization_policy=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_constraint "
            "WHERE conname = 'ck_organization_policy_floor_custom_words_array')"
        ),
        # v446: collection visibility mode (issue #1275). Keyed on its last-created CHECK.
        has_collection_visibility=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'ck_collection_visibility')"
        ),
        # v447: organization content holds (issue #1276). Keyed on its last-created CHECK.
        has_content_hold=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'ck_content_hold_collection_scope')"
        ),
        # v448: at most one active task row per (file, step) (issue #1185). Keyed on the partial
        # UNIQUE index, created last: a database without it still holds the duplicate rows the
        # revision resolves, so it must re-run.
        has_task_active_unique=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_indexes "
            "WHERE tablename = 'task' AND indexname = 'uq_task_active_per_file_type')"
        ),
        # v449: who wrote a speaker's name/link, and the record of how it and the recommendation came
        # to be (issue #1280). Keyed on the LAST object the revision creates, the foreign key of
        # ``identity_source_user_id`` (after the columns, their CHECK and the backfill, which run in
        # the same transaction).
        has_identity_source=_check_exists(
            "SELECT EXISTS(SELECT 1 FROM pg_constraint "
            "WHERE conname = 'speaker_identity_source_user_id_fkey')"
        ),
    )


# Bound to a name: v443 and v444 are each v442's fingerprint plus one more probe.
def matches_v442(f: SchemaFacts) -> bool:
    return (
        matches_v433(f)
        and not f.governance.has_unusable_pyannote_source
        and not f.governance.has_lite_local_source
        and f.governance.has_processing_provenance
        and f.governance.has_correction_carryover
        and not f.governance.has_unstamped_voiceprint_suggestion
        and f.governance.has_password_last_self_changed
        and f.governance.has_legal_acceptance
        and f.governance.has_takedown_origin
    )


ARMS: tuple[Arm, ...] = (
    # v449: same as v448 plus speaker.identity_source. The newest revision on this chain, so
    # this is the top of the ladder.
    (
        lambda f: (
            matches_v442(f)
            and f.governance.has_takedown_notice
            and f.governance.has_takedown_notification
            and f.governance.has_organization_policy
            and f.governance.has_collection_visibility
            and f.governance.has_content_hold
            and f.governance.has_task_active_unique
            and f.governance.has_identity_source
        ),
        "v449_add_speaker_identity_source",
    ),
    # v448: same as v447 plus the one-active-row-per-file-step guard.
    (
        lambda f: (
            matches_v442(f)
            and f.governance.has_takedown_notice
            and f.governance.has_takedown_notification
            and f.governance.has_organization_policy
            and f.governance.has_collection_visibility
            and f.governance.has_content_hold
            and f.governance.has_task_active_unique
        ),
        "v448_task_one_active_row_per_file_step",
    ),
    # v447: same as v446 plus the organization content holds.
    (
        lambda f: (
            matches_v442(f)
            and f.governance.has_takedown_notice
            and f.governance.has_takedown_notification
            and f.governance.has_organization_policy
            and f.governance.has_collection_visibility
            and f.governance.has_content_hold
        ),
        "v447_add_content_hold",
    ),
    # v446: same as v445 plus the collection visibility mode.
    (
        lambda f: (
            matches_v442(f)
            and f.governance.has_takedown_notice
            and f.governance.has_takedown_notification
            and f.governance.has_organization_policy
            and f.governance.has_collection_visibility
        ),
        "v446_add_collection_visibility",
    ),
    # v445: same as v444 plus the organization policy table.
    (
        lambda f: (
            matches_v442(f)
            and f.governance.has_takedown_notice
            and f.governance.has_takedown_notification
            and f.governance.has_organization_policy
        ),
        "v445_add_organization_policy",
    ),
    # v444: same as v443 plus the notification delivery log.
    (
        lambda f: (
            matches_v442(f)
            and f.governance.has_takedown_notice
            and f.governance.has_takedown_notification
        ),
        "v444_add_takedown_notification",
    ),
    # v443: same as v442 plus the takedown notice tables.
    (
        lambda f: matches_v442(f) and f.governance.has_takedown_notice,
        "v443_add_takedown_notice_tables",
    ),
    # v442: same as v441 plus the takedown-origin column, CHECK and trigger.
    (lambda f: matches_v442(f), "v442_add_media_file_takedown_origin"),
    # v441: same as v440 plus the legal acceptance table.
    (
        lambda f: (
            matches_v433(f)
            and not f.governance.has_unusable_pyannote_source
            and not f.governance.has_lite_local_source
            and f.governance.has_processing_provenance
            and f.governance.has_correction_carryover
            and not f.governance.has_unstamped_voiceprint_suggestion
            and f.governance.has_password_last_self_changed
            and f.governance.has_legal_acceptance
        ),
        "v441_add_legal_acceptance",
    ),
    # v440: same as v439 plus the self-service password change column.
    (
        lambda f: (
            matches_v433(f)
            and not f.governance.has_unusable_pyannote_source
            and not f.governance.has_lite_local_source
            and f.governance.has_processing_provenance
            and f.governance.has_correction_carryover
            and not f.governance.has_unstamped_voiceprint_suggestion
            and f.governance.has_password_last_self_changed
        ),
        "v440_add_password_last_self_changed_at",
    ),
    # v439: same as v438, with no provably-voiceprint legacy suggestion left unstamped.
    (
        lambda f: (
            matches_v433(f)
            and not f.governance.has_unusable_pyannote_source
            and not f.governance.has_lite_local_source
            and f.governance.has_processing_provenance
            and f.governance.has_correction_carryover
            and not f.governance.has_unstamped_voiceprint_suggestion
        ),
        "v439_infer_legacy_voiceprint_suggestion_source",
    ),
    # v438: same as v437 plus the speaker correction carry-over table.
    (
        lambda f: (
            matches_v433(f)
            and not f.governance.has_unusable_pyannote_source
            and not f.governance.has_lite_local_source
            and f.governance.has_processing_provenance
            and f.governance.has_correction_carryover
        ),
        "v438_add_speaker_correction_carryover",
    ),
    # v437: same as v436 plus the processing provenance column. The data-only arms below it
    # do not require the column, so a database without it is stamped v436 or lower and
    # re-runs it.
    (
        lambda f: (
            matches_v433(f)
            and not f.governance.has_unusable_pyannote_source
            and not f.governance.has_lite_local_source
            and f.governance.has_processing_provenance
        ),
        "v437_add_media_processing_provenance",
    ),
    # v436: same as v434, with no lite "this server" choice left that Automatic would keep.
    (
        lambda f: (
            matches_v433(f)
            and not f.governance.has_unusable_pyannote_source
            and not f.governance.has_lite_local_source
        ),
        "v436_lite_local_speaker_detection_to_automatic",
    ),
    # v434: same schema as v433, with no unusable pyannote.ai choice left.
    (
        lambda f: matches_v433(f) and not f.governance.has_unusable_pyannote_source,
        "v434_reset_unusable_pyannote_source",
    ),
)
