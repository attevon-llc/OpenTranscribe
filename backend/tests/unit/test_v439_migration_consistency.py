"""v439: legacy speaker suggestions keep their voiceprint label only when they can prove it.

Rows written before ``suggestion_source`` existed carry none, and the canonical label rule
(owner decision 2026-10-12) treats "no source" as unknown, i.e. never the name. ``v439``
stamps exactly the two provable shapes -- a profile link whose profile IS the suggestion,
and a stored voiceprint match to a same-owner speaker of that name at the speaker's own
confidence -- and leaves every other row alone, above all one an LLM could have written.

Like the other ``test_v43x`` suites this EXECUTES the revision's SQL, twice (the startup
runner re-runs a revision over its own output), restricted to this test's own speakers.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text

from app.utils.speaker_labels import canonical_speaker_label_for_row
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_profile
from tests.fixtures.speaker_cleanup_seams import make_speaker

REVISION = "v439_infer_legacy_voiceprint_suggestion_source"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"

#: A cosine score as a voiceprint matcher stores it; an LLM confidence is a round number.
_COSINE = 0.8734512019


def _revision_module():
    spec = importlib.util.spec_from_file_location(REVISION, _REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stamp(db, module, speaker_ids: list[int]) -> None:
    for statement in module.STAMP_STATEMENTS:
        db.execute(text(statement + " AND s.id = ANY(:ids)"), {"ids": speaker_ids})


def _match(db, a, b, confidence: float) -> None:
    from app.models.media import SpeakerMatch

    low, high = sorted((int(a.id), int(b.id)))
    db.add(SpeakerMatch(speaker1_id=low, speaker2_id=high, confidence=confidence))
    db.flush()


def _named(db, user, name: str):
    """A speaker in another file that a human (or an auto-accept) named."""
    return make_speaker(db, user, make_file(db, user), display_name=name, verified=True)


def _legacy(db, user, suggested: str, confidence: float = _COSINE, **kw):
    """A pre-v091 row: a confident suggestion with no recorded source."""
    return make_speaker(
        db, user, make_file(db, user), suggested_name=suggested, confidence=confidence, **kw
    )


def test_v439_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    heads = set(scripts.get_heads())

    assert rev.down_revision == "v438_add_speaker_correction_carryover"
    assert len(heads) == 1, "two heads mean two branches both claimed a revision number"
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_the_revision_is_vendor_neutral():
    source = _REVISION_PATH.read_text(encoding="utf-8").lower()
    assert "clerk" not in source and "stripe" not in source


def test_only_provably_voiceprint_rows_are_stamped(db_session, normal_user, admin_user):
    module = _revision_module()
    db, user = db_session, normal_user
    try:
        dana = make_profile(db, user, name="Dana Okafor")
        profile_linked = _legacy(db, user, "Dana Okafor", profile_id=dana.id)
        linked_other_name = _legacy(db, user, "Erin Vasquez", profile_id=dana.id)
        already_llm = _legacy(
            db, user, "Dana Okafor", profile_id=dana.id, suggestion_source="llm_analysis"
        )

        frank = _named(db, user, "Frank Lindqvist")
        voice_matched = _legacy(db, user, "Frank Lindqvist")
        _match(db, frank, voice_matched, _COSINE)

        # The LLM overwrote the confidence later: the stored match no longer explains it.
        relabelled = _legacy(db, user, "Frank Lindqvist", confidence=0.9)
        _match(db, frank, relabelled, _COSINE)

        george = _named(db, user, "George Tanaka")
        george.display_name = "George T."  # partner renamed since the match
        partner_renamed = _legacy(db, user, "George Tanaka")
        _match(db, george, partner_renamed, _COSINE)

        stranger = _named(db, admin_user, "Hana Mori")
        other_owner = _legacy(db, user, "Hana Mori")
        _match(db, stranger, other_owner, _COSINE)

        llm_guess = _legacy(db, user, "Ivan Petrov", confidence=0.9)
        verified_unlinked = _legacy(db, user, "Jun Park", confidence=0.9, verified=True)
        db.flush()

        rows = [
            profile_linked,
            linked_other_name,
            already_llm,
            voice_matched,
            relabelled,
            partner_renamed,
            other_owner,
            llm_guess,
            verified_unlinked,
        ]
        ids = [int(r.id) for r in rows]
        _stamp(db, module, ids)
        _stamp(db, module, ids)  # idempotent: nothing left to match
        for row in rows:
            db.refresh(row)

        assert profile_linked.suggestion_source == "profile_match"
        assert voice_matched.suggestion_source == "voice_match"
        # The label the user had before the upgrade comes back.
        assert canonical_speaker_label_for_row(profile_linked) == "Dana Okafor"
        assert canonical_speaker_label_for_row(voice_matched) == "Frank Lindqvist"

        # Fail closed: nothing proves these came from a voiceprint.
        for row in (
            linked_other_name,
            relabelled,
            partner_renamed,
            other_owner,
            llm_guess,
            verified_unlinked,
        ):
            assert row.suggestion_source is None, row.suggested_name
            assert canonical_speaker_label_for_row(row) == "SPEAKER_00"
        assert already_llm.suggestion_source == "llm_analysis"
        # Only the source is written.
        assert profile_linked.suggested_name == "Dana Okafor"
        assert profile_linked.confidence == pytest.approx(_COSINE)
        assert profile_linked.display_name is None
    finally:
        db.rollback()


def test_a_row_proving_both_is_recorded_as_a_profile_match(db_session, normal_user):
    """The live matcher records ``profile_match`` whenever the match names a profile."""
    module = _revision_module()
    db, user = db_session, normal_user
    try:
        kim = make_profile(db, user, name="Kim Adeyemi")
        partner = _named(db, user, "Kim Adeyemi")
        both = _legacy(db, user, "Kim Adeyemi", profile_id=kim.id)
        _match(db, partner, both, _COSINE)

        _stamp(db, module, [int(both.id)])
        db.refresh(both)
        assert both.suggestion_source == "profile_match"
    finally:
        db.rollback()


def test_the_downgrade_is_a_no_op():
    """Nothing records which rows were stamped: the body is its docstring alone."""
    import ast
    import inspect as pyinspect
    import textwrap

    tree = ast.parse(textwrap.dedent(pyinspect.getsource(_revision_module().downgrade)))
    func = tree.body[0]
    assert isinstance(func, ast.FunctionDef)
    assert len(func.body) == 1
    assert isinstance(func.body[0], ast.Expr)
    assert isinstance(func.body[0].value, ast.Constant)


def test_an_unstamped_provable_row_makes_detection_stamp_v438(db_session, normal_user):
    from app.db.migrations import _detect_schema_version

    db, user = db_session, normal_user
    try:
        lee = make_profile(db, user, name="Lee Moreau")
        _legacy(db, user, "Lee Moreau", profile_id=lee.id)
        conn = db.connection()
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        db.rollback()

    assert detected == "v438_add_speaker_correction_carryover"


def test_with_nothing_left_to_stamp_detection_stamps_v439_or_later(db_session):
    """The whole table is stamped inside the savepoint (rolled back), so the result does not
    depend on what the deployment's own speakers hold."""
    from tests.unit._migration_detection import assert_detected_at_or_after

    module = _revision_module()
    conn = db_session.connection()
    try:
        for statement in module.STAMP_STATEMENTS:
            conn.execute(text(statement))
        assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)
    finally:
        db_session.rollback()
