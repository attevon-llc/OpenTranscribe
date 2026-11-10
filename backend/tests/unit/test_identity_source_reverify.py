"""The speaker-provenance re-verify command (issue #1334 item 1).

The v449 backfill stamped an existing identity ``voiceprint`` on positive evidence, and a voiceprint
purge clears a stamped name for good. ``services/privacy/identity_reverify`` re-tests every stamp
the backfill left on today's data, clears the ones that rest on nothing when asked to (or all of
them), and audits every run. These tests run on the real database: the predicate is SQL, and an
in-memory stand-in would test nothing but itself.
"""

from __future__ import annotations

import importlib.util
import json
import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from app.auth.audit import audit_logger
from app.auth.audit_events import AuditEventType
from app.models.speaker import Speaker
from app.models.speaker import SpeakerProfile
from app.models.speaker_identity_event import SpeakerIdentityEvent
from app.services.privacy import identity_reverify
from app.services.privacy.identity_reverify import ReverifyScope
from app.services.privacy.identity_reverify import reverify_identity_sources
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_speaker

_V449 = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "v449_add_speaker_identity_source.py"
)
_T0 = datetime(2026, 1, 1, tzinfo=UTC)
BACKFILL = {"origin": "v449_backfill"}


def _v449():
    spec = importlib.util.spec_from_file_location("v449_for_reverify", _V449)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def events(monkeypatch) -> list[dict[str, Any]]:
    written: list[dict[str, Any]] = []
    monkeypatch.setattr(audit_logger, "log", lambda **kw: written.append(kw))
    return written


@pytest.fixture
def queued() -> list[tuple[int, str, int]]:
    return []


def _queue(queued):
    def record(files):
        queued.extend(files)
        return []

    return record


def _profile(db, user, name: str) -> SpeakerProfile:
    profile = SpeakerProfile(uuid=uuid_pkg.uuid4(), user_id=user.id, name=name, created_at=_T0)
    db.add(profile)
    db.flush()
    return profile


def _auto_accept(
    db, user, profile: SpeakerProfile, *, carrier: bool = True, summary: bool = False, **fields
) -> Speaker:
    """A later speaker in the profile auto-accept shape, with the evidence unless told otherwise."""
    if carrier:
        make_speaker(
            db,
            user,
            make_file(db, user),
            display_name=profile.name,
            verified=True,
            profile_id=profile.id,
            identity_source="human",
            created_at=_T0 - timedelta(days=3),
        )
    media = make_file(db, user)
    if summary:
        media.summary_data = {"bluf": "a summary"}
    values: dict[str, Any] = {
        "display_name": profile.name,
        "suggested_name": profile.name,
        "suggestion_source": "profile_match",
        "confidence": 0.82,
        "verified": True,
        "profile_id": profile.id,
        "created_at": _T0 + timedelta(days=1),
    }
    values.update(fields)
    speaker: Speaker = make_speaker(db, user, media, **values)
    db.flush()
    return speaker


def _stamp(db, speaker: Speaker) -> None:
    """What the v449 backfill wrote on a row it recognised."""
    db.execute(
        text(
            "UPDATE speaker SET identity_source = 'voiceprint', "
            "identity_source_detail = CAST(:d AS jsonb) WHERE id = :i"
        ),
        {"d": json.dumps(BACKFILL), "i": speaker.id},
    )


def _row(db, speaker: Speaker) -> tuple[Any, ...]:
    return tuple(
        db.execute(
            text(
                "SELECT identity_source, identity_source_detail, display_name, profile_id, verified "
                "FROM speaker WHERE id = :i"
            ),
            {"i": speaker.id},
        ).one()
    )


def _reverify_history(db, speaker: Speaker) -> list[SpeakerIdentityEvent]:
    """The history rows the re-verify wrote (creating the test's rows writes its own)."""
    rows: list[SpeakerIdentityEvent] = (
        db.query(SpeakerIdentityEvent)
        .filter_by(speaker_id=speaker.id, origin="identity_reverify")
        .all()
    )
    return rows


def _mine(normal_user) -> ReverifyScope:
    return ReverifyScope(user_id=normal_user.id)


def test_its_predicates_are_the_backfills_on_the_same_rows(db_session, normal_user, events):
    """Run the real v449 backfill, then the re-verify: every row the backfill stamped still holds
    its evidence, and the shape rows it left are counted the same way."""
    alice = _profile(db_session, normal_user, "Alice")
    stamped = _auto_accept(db_session, normal_user, alice)
    bob = _profile(db_session, normal_user, "Bob")
    # The shape without the evidence: a person typed the name, so the speaker is older.
    _auto_accept(db_session, normal_user, bob, carrier=False, created_at=_T0 - timedelta(days=1))
    carol = _profile(db_session, normal_user, "Carol")
    _auto_accept(db_session, normal_user, carol, suggestion_source="voice_match")

    v449 = _v449()
    conn = db_session.connection()
    scope_sql = f"s.user_id = {int(normal_user.id)}"
    shape_rows = conn.execute(
        text(f"SELECT count(*) FROM speaker s WHERE {v449.SHAPE_WHERE} AND {scope_sql}")
    ).scalar()
    conn.execute(text(f"{v449.BACKFILL_SQL} AND {scope_sql}"))

    report = reverify_identity_sources(db_session, scope=_mine(normal_user))

    assert _row(db_session, stamped)[:2] == ("voiceprint", BACKFILL)
    assert report.backfill_stamped == 1 and report.evidence_holds == 1
    assert report.evidence_lost == 0
    assert report.shape_left_unmarked == shape_rows - 1 == 1
    assert report.shape_left_unmarked_with_evidence == 0


def test_a_stamp_whose_evidence_is_gone_is_reported_and_a_dry_run_writes_nothing(
    db_session, normal_user, events, queued
):
    profile = _profile(db_session, normal_user, "Alice")
    holds = _auto_accept(db_session, normal_user, profile)
    lost = _auto_accept(db_session, normal_user, _profile(db_session, normal_user, "Dana"))
    _stamp(db_session, holds)
    _stamp(db_session, lost)
    # The voice the evidence rested on is gone (deleted with its file, merged away, ...).
    db_session.execute(
        text("DELETE FROM speaker WHERE profile_id = :p AND id <> :i"),
        {"p": lost.profile_id, "i": lost.id},
    )

    report = reverify_identity_sources(db_session, scope=_mine(normal_user), reindex=_queue(queued))

    assert report.dry_run is True
    assert (report.backfill_stamped, report.evidence_holds, report.evidence_lost) == (2, 1, 1)
    assert report.evidence_lost_uuids == [str(lost.uuid)]
    assert report.evidence_holds_uuids == [str(holds.uuid)]
    assert report.cleared == 0 and queued == []
    assert _row(db_session, lost)[:2] == ("voiceprint", BACKFILL), "a dry run changed a row"
    [event] = events
    assert event["event_type"] is AuditEventType.PRIVACY_IDENTITY_REVERIFY
    assert event["details"]["phase"] == "dry_run"
    assert event["details"]["counts"]["evidence_lost"] == 1
    assert event["details"]["cleared_speaker_uuids"] == []


def test_apply_clears_only_the_unfounded_stamp_and_keeps_the_name(
    db_session, normal_user, events, queued
):
    holds = _auto_accept(db_session, normal_user, _profile(db_session, normal_user, "Alice"))
    lost = _auto_accept(db_session, normal_user, _profile(db_session, normal_user, "Dana"))
    _stamp(db_session, holds)
    _stamp(db_session, lost)
    db_session.execute(
        text("DELETE FROM speaker WHERE profile_id = :p AND id <> :i"),
        {"p": lost.profile_id, "i": lost.id},
    )

    report = reverify_identity_sources(
        db_session, scope=_mine(normal_user), apply=True, reindex=_queue(queued)
    )

    assert report.cleared == 1 and report.cleared_uuids == [str(lost.uuid)]
    # Unknown again (read as a person's): the name, link and verified mark are untouched.
    assert _row(db_session, lost) == (None, None, "Dana", lost.profile_id, True)
    assert _row(db_session, holds)[:2] == ("voiceprint", BACKFILL)
    assert [f[0] for f in queued] == [lost.media_file_id]
    history = _reverify_history(db_session, lost)
    assert [(h.kind, h.actor_kind, h.origin, h.voiceprint) for h in history] == [
        ("confirmed", "system", "identity_reverify", False)
    ]
    assert history[0].detail == {
        "origin": "identity_reverify",
        "previous_origin": "v449_backfill",
        "reason": "evidence_lost",
    }
    assert _reverify_history(db_session, holds) == []
    [event] = events
    assert event["details"]["phase"] == "applied"
    assert event["details"]["cleared_speaker_uuids"] == [str(lost.uuid)]
    assert event["target_user_id"] == normal_user.id
    assert event["outcome"].value == "success"


def test_include_evidenced_clears_every_backfill_stamp_and_nothing_else(
    db_session, normal_user, events, queued
):
    holds = _auto_accept(db_session, normal_user, _profile(db_session, normal_user, "Alice"))
    _stamp(db_session, holds)
    live_match = _auto_accept(db_session, normal_user, _profile(db_session, normal_user, "Eve"))
    db_session.execute(
        text(
            "UPDATE speaker SET identity_source = 'voiceprint', "
            "identity_source_detail = CAST(:d AS jsonb) WHERE id = :i"
        ),
        {"d": json.dumps({"origin": "profile_match"}), "i": live_match.id},
    )

    report = reverify_identity_sources(
        db_session,
        scope=_mine(normal_user),
        apply=True,
        include_evidenced=True,
        reindex=_queue(queued),
    )

    assert report.cleared_uuids == [str(holds.uuid)]
    assert _row(db_session, holds)[0] is None
    assert _row(db_session, live_match)[0] == "voiceprint", "a match made after 0.6.0 is not v449's"
    [history] = _reverify_history(db_session, holds)
    assert history.detail is not None and history.detail["reason"] == "operator_cleared"


def test_a_row_rewritten_since_the_measurement_keeps_its_new_record(db_session, normal_user):
    speaker = _auto_accept(db_session, normal_user, _profile(db_session, normal_user, "Alice"))
    db_session.execute(
        text(
            "UPDATE speaker SET identity_source = 'human', "
            "identity_source_detail = CAST(:d AS jsonb) WHERE id = :i"
        ),
        {"d": json.dumps({"origin": "typed"}), "i": speaker.id},
    )

    assert identity_reverify._clear(db_session, [speaker.id], reason="evidence_lost") == []
    assert _row(db_session, speaker)[:2] == ("human", {"origin": "typed"})


def test_the_scope_keeps_another_users_stamps_out(db_session, normal_user, other_user, events):
    mine = _auto_accept(db_session, normal_user, _profile(db_session, normal_user, "Alice"))
    theirs = _auto_accept(db_session, other_user, _profile(db_session, other_user, "Alice"))
    for speaker in (mine, theirs):
        _stamp(db_session, speaker)
        db_session.execute(
            text("DELETE FROM speaker WHERE profile_id = :p AND id <> :i"),
            {"p": speaker.profile_id, "i": speaker.id},
        )

    report = reverify_identity_sources(
        db_session, scope=_mine(normal_user), apply=True, reindex=lambda _files: []
    )

    assert report.cleared_uuids == [str(mine.uuid)]
    assert _row(db_session, theirs)[0] == "voiceprint"


def test_an_organization_scope_follows_the_files_tenant_not_the_speaker_row(
    db_session, normal_user, events
):
    """The pipeline never stamps ``speaker.organization_id``; the tenant is the file's."""
    from app.models.organization import Organization

    org_a = Organization(name=f"org-a-{uuid_pkg.uuid4().hex[:8]}")
    org_b = Organization(name=f"org-b-{uuid_pkg.uuid4().hex[:8]}")
    db_session.add_all([org_a, org_b])
    db_session.flush()
    speaker = _auto_accept(db_session, normal_user, _profile(db_session, normal_user, "Alice"))
    speaker.media_file.organization_id = org_a.id
    db_session.execute(
        text("DELETE FROM speaker WHERE profile_id = :p AND id <> :i"),
        {"p": speaker.profile_id, "i": speaker.id},
    )
    _stamp(db_session, speaker)
    db_session.flush()
    assert (
        db_session.execute(
            text("SELECT organization_id FROM speaker WHERE id = :i"), {"i": speaker.id}
        ).scalar_one()
        is None
    )

    other = reverify_identity_sources(
        db_session,
        scope=ReverifyScope(organization_id=org_b.id),
        apply=True,
        reindex=lambda _files: [],
    )
    assert other.backfill_stamped == 0
    assert _row(db_session, speaker)[0] == "voiceprint"

    own = reverify_identity_sources(
        db_session,
        scope=ReverifyScope(organization_id=org_a.id),
        apply=True,
        reindex=lambda _files: [],
    )
    assert own.cleared_uuids == [str(speaker.uuid)]


def test_it_lists_the_summaries_a_voiceprint_name_may_be_in(db_session, normal_user, events):
    profile = _profile(db_session, normal_user, "Alice")
    with_summary = _auto_accept(db_session, normal_user, profile, summary=True)
    without_summary = _auto_accept(db_session, normal_user, profile, carrier=False)
    person_named = _auto_accept(db_session, normal_user, profile, carrier=False, summary=True)
    for speaker in (with_summary, without_summary):
        _stamp(db_session, speaker)
    db_session.execute(
        text("UPDATE speaker SET identity_source = 'human' WHERE id = :i"), {"i": person_named.id}
    )
    files = {
        s.id: str(
            db_session.execute(
                text("SELECT uuid FROM media_file WHERE id = :i"), {"i": s.media_file_id}
            ).scalar()
        )
        for s in (with_summary, person_named)
    }

    report = reverify_identity_sources(db_session, scope=_mine(normal_user))

    assert report.summary_file_uuids == [files[with_summary.id]]
    assert report.summaries_to_review == 1


def test_a_reindex_that_could_not_be_queued_is_reported_with_its_file(
    db_session, normal_user, events
):
    speaker = _auto_accept(db_session, normal_user, _profile(db_session, normal_user, "Alice"))
    _stamp(db_session, speaker)
    db_session.execute(
        text("DELETE FROM speaker WHERE profile_id = :p AND id <> :i"),
        {"p": speaker.profile_id, "i": speaker.id},
    )

    report = reverify_identity_sources(
        db_session,
        scope=_mine(normal_user),
        apply=True,
        reindex=lambda files: [file_uuid for _, file_uuid, _ in files],
    )

    assert report.cleared == 1 and report.reindex_queue_failed == 1
    assert len(report.reindex_failed_file_uuids) == 1
    [event] = events
    assert event["outcome"].value == "partial"
    assert event["details"]["reindex_failed_file_uuids"] == report.reindex_failed_file_uuids


def test_the_command_refuses_include_evidenced_without_apply():
    from app.scripts import reverify_identity_sources as command

    with pytest.raises(SystemExit) as exit_info:
        command.main(["--include-evidenced"])
    assert exit_info.value.code == 2


@pytest.mark.parametrize("protection", ["legal_hold", "quarantine"])
def test_apply_skips_the_speakers_of_a_held_or_quarantined_file_and_counts_them(
    db_session, normal_user, events, queued, protection
):
    """Every background write skips a frozen file (``hold_guard``); this operator CLI included."""
    from app.models.media import MediaFile

    frozen = _auto_accept(db_session, normal_user, _profile(db_session, normal_user, "Alice"))
    free = _auto_accept(db_session, normal_user, _profile(db_session, normal_user, "Dana"))
    _stamp(db_session, frozen)
    _stamp(db_session, free)
    media = db_session.get(MediaFile, frozen.media_file_id)
    setattr(media, "legal_hold" if protection == "legal_hold" else "is_quarantined", True)
    db_session.flush()

    report = reverify_identity_sources(
        db_session,
        scope=_mine(normal_user),
        apply=True,
        include_evidenced=True,
        reindex=_queue(queued),
    )

    assert report.cleared_uuids == [str(free.uuid)]
    assert report.skipped_protected == 1
    assert _row(db_session, frozen)[:2] == ("voiceprint", BACKFILL)
    assert _reverify_history(db_session, frozen) == []
    assert [f[0] for f in queued] == [free.media_file_id]
    [event] = events
    assert event["details"]["counts"]["skipped_protected"] == 1
