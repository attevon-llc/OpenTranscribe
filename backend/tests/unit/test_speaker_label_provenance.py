"""A speaker's canonical label never comes from an LLM suggestion (owner decision 2026-10-12).

The label that search, chat, exports and the OpenSearch documents show as a speaker's name
comes ONLY from (a) a name a human typed or verified (``Speaker.display_name``), or (b) a
voiceprint match -- ``suggestion_source`` ``voice_match`` / ``profile_match`` -- at a cosine
similarity of at least 0.75 (``CLUSTER_ASSIGNMENT_THRESHOLD``). An LLM suggestion
(``llm_analysis``) is a suggestion the human must verify: the suggestion endpoints still
return it with its confidence, but it must not reach the label at any consumer.

Before this rule ``canonical_speaker_label`` promoted ANY ``suggested_name`` at confidence
>= 0.75, so an LLM guess became the speaker's name in the chunk index, digests, facts and
exports without a human looking at it (issue #605 wired that promotion in on purpose).

Each plane is driven through its own real helper -- not a second call to
``canonical_speaker_label`` -- so a plane that forgot to pass the provenance would fail here.
"""

from __future__ import annotations

import ast
import uuid as uuid_pkg
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.utils.speaker_labels import EMBEDDING_SUGGESTION_SOURCES
from app.utils.speaker_labels import UNKNOWN_SPEAKER_LABEL
from app.utils.speaker_labels import canonical_speaker_label
from app.utils.speaker_labels import canonical_speaker_label_for_row

pytestmark = pytest.mark.unit

_PLACEHOLDER = "SPEAKER_00"


def _row(**kwargs):
    base = dict(
        name=_PLACEHOLDER,
        display_name=None,
        suggested_name=None,
        confidence=None,
        suggestion_source=None,
        verified=False,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def _llm(confidence=0.95, **kwargs):
    return _row(
        suggested_name="Joe Rogan",
        confidence=confidence,
        suggestion_source="llm_analysis",
        **kwargs,
    )


# --------------------------------------------------------------------------- the rule


def test_a_confident_llm_suggestion_does_not_become_the_label():
    assert canonical_speaker_label_for_row(_llm(0.95)) == _PLACEHOLDER


def test_a_confident_llm_suggestion_on_an_undiarized_speaker_stays_unknown():
    assert canonical_speaker_label_for_row(_llm(0.95, name=None)) == UNKNOWN_SPEAKER_LABEL


def test_once_a_human_verifies_the_suggestion_the_label_is_the_verified_name():
    speaker = _llm(0.95)
    assert canonical_speaker_label_for_row(speaker) == _PLACEHOLDER
    speaker.display_name = speaker.suggested_name
    speaker.verified = True
    assert canonical_speaker_label_for_row(speaker) == "Joe Rogan"


@pytest.mark.parametrize("source", ["voice_match", "profile_match"])
def test_a_voiceprint_match_at_the_assignment_threshold_is_the_label(source):
    speaker = _row(suggested_name="Priya Patel", confidence=0.75, suggestion_source=source)
    assert canonical_speaker_label_for_row(speaker) == "Priya Patel"


@pytest.mark.parametrize("source", ["voice_match", "profile_match"])
def test_a_voiceprint_match_just_under_the_threshold_is_not(source):
    speaker = _row(suggested_name="Priya Patel", confidence=0.74, suggestion_source=source)
    assert canonical_speaker_label_for_row(speaker) == _PLACEHOLDER


@pytest.mark.parametrize("source", ["llm_analysis", "voice_match", "profile_match"])
def test_a_typed_name_beats_every_suggestion(source):
    speaker = _row(
        display_name="Alice",
        suggested_name="Bob",
        confidence=0.99,
        suggestion_source=source,
        verified=True,
    )
    assert canonical_speaker_label_for_row(speaker) == "Alice"


@pytest.mark.parametrize("source", [None, "", "metadata_hint", "user_skipped", "anything_new"])
def test_a_suggestion_of_unknown_or_non_voiceprint_provenance_fails_closed(source):
    """Allow-list, not deny-list: a suggestion that cannot prove it came from a voiceprint
    is a suggestion. Legacy rows with no source are the cost of that, deliberately."""
    speaker = _row(suggested_name="Bob", confidence=0.99, suggestion_source=source)
    assert canonical_speaker_label_for_row(speaker) == _PLACEHOLDER


def test_omitting_the_source_argument_fails_closed():
    assert canonical_speaker_label(_PLACEHOLDER, suggested_name="Bob", confidence=0.99) == (
        _PLACEHOLDER
    )


def test_the_allow_list_is_exactly_the_two_voiceprint_sources():
    assert {"voice_match", "profile_match"} == EMBEDDING_SUGGESTION_SOURCES


# ------------------------------------------- every producer of a source is classified

_APP = Path(__file__).resolve().parents[2] / "app"
#: Sources written for a suggestion that is NOT a voiceprint match, with the reason each
#: stays out of the allow-list.
_NON_CANONICAL_SOURCES = {
    "llm_analysis": "an LLM guess from transcript content",
    "user_skipped": "a marker that the user skipped a speaker; carries no name",
}


def _written_source_literals() -> set[str]:
    found: set[str] = set()
    for path in _APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            values: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                if any(
                    isinstance(t, ast.Attribute) and t.attr == "suggestion_source"
                    for t in node.targets
                ):
                    values.append(node.value)
            elif isinstance(node, ast.Call):
                values.extend(kw.value for kw in node.keywords if kw.arg == "suggestion_source")
            for value in values:
                found.update(_possible_literals(value))
    return found


def _possible_literals(expr: ast.expr) -> set[str]:
    """The string literals an expression can evaluate to (a conditional yields either arm)."""
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return {expr.value}
    if isinstance(expr, ast.IfExp):
        return _possible_literals(expr.body) | _possible_literals(expr.orelse)
    return set()


def test_every_source_the_app_writes_is_classified():
    """A new suggestion producer is non-canonical until someone decides otherwise -- and
    that decision has to be written into one of the two sets, not made by accident."""
    written = _written_source_literals()
    # The detector must see the producers we know exist, or "nothing unclassified" is vacuous.
    assert {"llm_analysis", "voice_match", "profile_match"} <= written
    unclassified = written - EMBEDDING_SUGGESTION_SOURCES - set(_NON_CANONICAL_SOURCES)
    assert not unclassified, f"classify these suggestion_source values: {sorted(unclassified)}"


# ---------------------------------------------------- every consumer applies the same rule


def test_every_writer_and_reader_plane_agrees_on_an_llm_speaker():
    from app.api.endpoints.files.crud import _resolve_segment_speaker_name
    from app.services.formatting_service import FormattingService
    from app.services.speaker_status_service import SpeakerStatusService
    from app.tasks.reindex_task import _resolve_reindex_speaker_name
    from app.tasks.search_indexing_task import resolve_chunk_speaker_name
    from app.utils.transcript_builders import get_speaker_name

    speaker = _llm(0.95)
    assert resolve_chunk_speaker_name(speaker, voiceprint_names=True) == _PLACEHOLDER
    assert _resolve_reindex_speaker_name(speaker, voiceprint_names=True) == _PLACEHOLDER
    assert get_speaker_name(SimpleNamespace(speaker=speaker)) == _PLACEHOLDER
    assert SpeakerStatusService._resolve_display_name(speaker) == _PLACEHOLDER
    assert _resolve_segment_speaker_name(speaker) == _PLACEHOLDER
    assert FormattingService.format_speaker_name(speaker) == _PLACEHOLDER


def test_every_plane_names_a_voiceprint_matched_speaker():
    """The control: the same planes DO use a voiceprint match, so the test above is not
    passing because a plane ignores suggestions altogether."""
    from app.api.endpoints.files.crud import _resolve_segment_speaker_name
    from app.services.formatting_service import FormattingService
    from app.services.speaker_status_service import SpeakerStatusService
    from app.tasks.reindex_task import _resolve_reindex_speaker_name
    from app.tasks.search_indexing_task import resolve_chunk_speaker_name
    from app.utils.transcript_builders import get_speaker_name

    speaker = _row(suggested_name="Priya Patel", confidence=0.8, suggestion_source="voice_match")
    assert resolve_chunk_speaker_name(speaker, voiceprint_names=True) == "Priya Patel"
    assert _resolve_reindex_speaker_name(speaker, voiceprint_names=True) == "Priya Patel"
    assert get_speaker_name(SimpleNamespace(speaker=speaker)) == "Priya Patel"
    assert SpeakerStatusService._resolve_display_name(speaker) == "Priya Patel"
    assert _resolve_segment_speaker_name(speaker) == "Priya Patel"
    assert FormattingService.format_speaker_name(speaker) == "Priya Patel"


def test_the_segment_formatter_applies_the_rule_when_no_status_was_stamped(db_session, normal_user):
    """``format_transcript_segment`` falls back to resolving the speaker itself when the
    persisted ``resolved_display_name`` is empty -- it must carry the provenance too."""
    from app.models.media import TranscriptSegment
    from app.services.formatting_service import FormattingService

    def resolve(source):
        media_file, speaker = _file_and_speakers(
            db_session,
            normal_user,
            suggested_name="Joe Rogan",
            confidence=0.95,
            suggestion_source=source,
        )
        segment = TranscriptSegment(
            uuid=uuid_pkg.uuid4(),
            media_file_id=media_file.id,
            speaker_id=speaker.id,
            start_time=0.0,
            end_time=1.0,
            text="hello",
        )
        db_session.add(segment)
        db_session.flush()
        db_session.refresh(segment)
        # The schema reads only relationships that are already loaded; the segments endpoint
        # joinedloads the speaker, so load it here the same way.
        assert segment.speaker is not None
        return FormattingService.format_transcript_segment(segment).resolved_speaker_name

    assert resolve("llm_analysis") == _PLACEHOLDER
    assert resolve("voice_match") == "Joe Rogan"


# ------------------------------------------------------- the rows the DB-backed paths read


def _file_and_speakers(db, user, **speaker_kwargs):
    from tests.fixtures.speaker_cleanup_seams import make_file
    from tests.fixtures.speaker_cleanup_seams import make_speaker

    media_file = make_file(db, user)
    return media_file, make_speaker(db, user, media_file, **speaker_kwargs)


def test_the_reindex_writer_reads_the_provenance_column(db_session, normal_user):
    """``reindex_task`` batch-fetches speaker COLUMNS, so a column left out of that select
    reaches the resolver as 'unknown source'. Drive the real batch fetch: an LLM speaker
    stays on the placeholder and a voiceprint speaker is named."""
    from app.models.media import TranscriptSegment
    from app.tasks import reindex_task
    from tests.fixtures.speaker_cleanup_seams import make_speaker

    media_file, llm_speaker = _file_and_speakers(
        db_session,
        normal_user,
        name="SPEAKER_00",
        suggested_name="Joe Rogan",
        confidence=0.95,
        suggestion_source="llm_analysis",
    )
    voice_speaker = make_speaker(
        db_session,
        normal_user,
        media_file,
        name="SPEAKER_01",
        suggested_name="Priya Patel",
        confidence=0.9,
        suggestion_source="voice_match",
    )
    for index, speaker in enumerate((llm_speaker, voice_speaker)):
        db_session.add(
            TranscriptSegment(
                uuid=uuid_pkg.uuid4(),
                media_file_id=media_file.id,
                speaker_id=speaker.id,
                start_time=float(index),
                end_time=float(index) + 1.0,
                text=f"segment {index}",
            )
        )
    db_session.commit()

    metadata = reindex_task._extract_file_metadata(db_session, media_file)
    assert metadata is not None
    by_text = {seg["text"]: seg["speaker"] for seg in metadata["segments"]}
    assert by_text == {"segment 0": "SPEAKER_00", "segment 1": "Priya Patel"}


def test_the_rename_propagation_task_resolves_the_label_with_the_provenance(
    db_session, normal_user, monkeypatch
):
    """``_current_speaker_name`` re-reads columns in its own session; the rule has to survive
    that read, or the update_by_query it narrows would target a label nothing was indexed with."""
    from contextlib import contextmanager

    from app.tasks import rename_propagation_task as rpt

    _, llm_speaker = _file_and_speakers(
        db_session,
        normal_user,
        suggested_name="Joe Rogan",
        confidence=0.95,
        suggestion_source="llm_analysis",
    )
    _, voice_speaker = _file_and_speakers(
        db_session,
        normal_user,
        suggested_name="Priya Patel",
        confidence=0.9,
        suggestion_source="voice_match",
    )

    @contextmanager
    def same_session():
        yield db_session

    monkeypatch.setattr("app.db.session_utils.session_scope", same_session)
    assert rpt._current_speaker_name(int(llm_speaker.id)) == "SPEAKER_00"
    assert rpt._current_speaker_name(int(voice_speaker.id)) == "Priya Patel"


def test_chat_speaker_resolution_cannot_find_a_speaker_through_an_llm_suggestion(
    db_session, normal_user
):
    """'Who said ...' resolves a mentioned name against speaker labels. A speaker the LLM
    only guessed at must not answer to the guess, while a voiceprint match does."""
    from app.services.chat.speaker_resolver import build_candidate_roster

    _file_and_speakers(
        db_session,
        normal_user,
        name="SPEAKER_00",
        suggested_name="Zebedee Quillfeather",
        confidence=0.95,
        suggestion_source="llm_analysis",
    )
    _file_and_speakers(
        db_session,
        normal_user,
        name="SPEAKER_00",
        suggested_name="Ophelia Marchbanks",
        confidence=0.9,
        suggestion_source="voice_match",
    )

    roster = build_candidate_roster(
        db_session, normal_user.id, ["Zebedee Quillfeather", "Ophelia Marchbanks"]
    )
    names = {entry.name for entry in roster.entries}
    assert "Ophelia Marchbanks" in names
    assert "Zebedee Quillfeather" not in names
    assert "SPEAKER_00" not in names or all(name != "Zebedee Quillfeather" for name in names), (
        "the LLM-suggested speaker must not surface under its guessed name"
    )


def test_the_speaker_api_still_returns_the_llm_suggestion_but_not_as_the_name(
    db_session, normal_user
):
    """The suggestion stays visible (with its confidence and source) so the human can verify
    it; ``resolved_display_name`` -- what the SPA's name slot may render -- stays the placeholder."""
    from app.api.endpoints.speakers import _process_single_speaker_with_suggestions

    _, speaker = _file_and_speakers(
        db_session,
        normal_user,
        suggested_name="Joe Rogan",
        confidence=0.95,
        suggestion_source="llm_analysis",
    )
    payload = _process_single_speaker_with_suggestions(speaker, normal_user, 0, [])
    assert payload["suggested_name"] == "Joe Rogan"
    assert payload["suggestion_source"] == "llm_analysis"
    assert payload["confidence"] == pytest.approx(0.95)
    assert payload["has_llm_suggestion"] is True
    assert payload["display_name"] == ""
    assert speaker.resolved_display_name == "SPEAKER_00"


def test_the_speaker_api_does_not_claim_a_voice_match_for_a_suggestion_of_unknown_source(
    db_session, normal_user
):
    """A legacy suggestion v439 could not attribute stays unattributed on the wire: badging it
    ``voice_match`` claims the voiceprint evidence the canonical label itself refuses."""
    from app.api.endpoints.speakers import _process_single_speaker_with_suggestions

    _, speaker = _file_and_speakers(
        db_session, normal_user, suggested_name="Joe Rogan", confidence=0.95
    )
    payload = _process_single_speaker_with_suggestions(speaker, normal_user, 0, [])
    assert payload["suggested_name"] == "Joe Rogan"
    assert payload["suggestion_source"] is None
    assert speaker.resolved_display_name == "SPEAKER_00"


def test_the_suggestion_service_still_offers_the_llm_suggestion(db_session, normal_user):
    from app.services.smart_speaker_suggestion_service import SmartSpeakerSuggestionService

    _, speaker = _file_and_speakers(
        db_session,
        normal_user,
        suggested_name="Joe Rogan",
        confidence=0.95,
        suggestion_source="llm_analysis",
    )
    offered = SmartSpeakerSuggestionService.consolidate_suggestions_batch(
        speakers=[speaker],
        user_id=int(normal_user.id),
        file_org_map={int(speaker.media_file_id): None},
        voiceprints_off_file_ids=set(),
    )[int(speaker.id)]
    llm = [s for s in offered if s.suggestion_type == "llm_analysis"]
    assert [(s.name, round(s.confidence, 2)) for s in llm] == [("Joe Rogan", 0.95)]


# ------------------------------------------------------------- the LLM task's own writes


def test_the_llm_task_stores_a_suggestion_without_moving_the_label(
    db_session, normal_user, monkeypatch
):
    from contextlib import contextmanager

    from app.tasks import speaker_identification_task as sit

    media_file, speaker = _file_and_speakers(db_session, normal_user, name="SPEAKER_00")

    @contextmanager
    def same_session():
        yield db_session

    monkeypatch.setattr(sit, "session_scope", same_session)
    dispatched: list[tuple] = []
    monkeypatch.setattr(
        "app.services.speaker_rename_tracker.SpeakerRenameTracker.flush",
        lambda self, db: dispatched.append(tuple(self.__dict__)),
    )

    sit._store_speaker_predictions(
        int(media_file.id),
        {
            "speaker_predictions": [
                {"speaker_label": "SPEAKER_00", "predicted_name": "Joe Rogan", "confidence": 0.95}
            ]
        },
    )
    db_session.refresh(speaker)
    assert (speaker.suggested_name, speaker.suggestion_source) == ("Joe Rogan", "llm_analysis")
    assert canonical_speaker_label_for_row(speaker) == "SPEAKER_00"


def test_the_llm_task_does_not_overwrite_a_voiceprint_suggestion(
    db_session, normal_user, monkeypatch
):
    """Replacing a voice match with a guess would take away the label the voice earned."""
    from contextlib import contextmanager

    from app.tasks import speaker_identification_task as sit

    media_file, speaker = _file_and_speakers(
        db_session,
        normal_user,
        name="SPEAKER_00",
        suggested_name="Priya Patel",
        confidence=0.9,
        suggestion_source="voice_match",
    )

    @contextmanager
    def same_session():
        yield db_session

    monkeypatch.setattr(sit, "session_scope", same_session)
    monkeypatch.setattr(
        "app.services.speaker_rename_tracker.SpeakerRenameTracker.flush", lambda self, db: None
    )
    sit._store_speaker_predictions(
        int(media_file.id),
        {
            "speaker_predictions": [
                {"speaker_label": "SPEAKER_00", "predicted_name": "Joe Rogan", "confidence": 0.99}
            ]
        },
    )
    db_session.refresh(speaker)
    assert (speaker.suggested_name, speaker.suggestion_source) == ("Priya Patel", "voice_match")
    assert canonical_speaker_label_for_row(speaker) == "Priya Patel"


# --------------------------------------------------------------------------- the backfill


@pytest.fixture
def backfill_world(db_session, normal_user, speaker_os_seams, monkeypatch):
    """A cursor parked past every pre-existing file, so only this test's files are eligible."""
    from app.models.media import MediaFile
    from app.services import speaker_label_backfill as backfill
    from app.services.system_settings_service import set_setting

    facts: list[int] = []
    monkeypatch.setattr("app.tasks.ingest_artifacts_task.dispatch_file_facts", facts.append)
    top = db_session.query(MediaFile.id).order_by(MediaFile.id.desc()).limit(1).scalar() or 0
    set_setting(db_session, backfill.COMPLETE_FLAG_KEY, "false")
    set_setting(db_session, backfill.CURSOR_KEY, str(top))
    return SimpleNamespace(backfill=backfill, facts=facts, seams=speaker_os_seams)


def _with_segment(db, user, **speaker_kwargs):
    from app.models.media import TranscriptSegment

    media_file, speaker = _file_and_speakers(db, user, **speaker_kwargs)
    db.add(
        TranscriptSegment(
            uuid=uuid_pkg.uuid4(),
            media_file_id=media_file.id,
            speaker_id=speaker.id,
            start_time=0.0,
            end_time=1.0,
            text="hello",
        )
    )
    db.commit()
    return media_file, speaker


def test_the_backfill_reindexes_exactly_the_files_whose_label_came_from_an_llm_guess(
    db_session, normal_user, backfill_world
):
    llm_file, llm_speaker = _with_segment(
        db_session,
        normal_user,
        suggested_name="Joe Rogan",
        confidence=0.95,
        suggestion_source="llm_analysis",
        resolved_display_name="Joe Rogan",  # what the old rule persisted
    )
    legacy_file, _ = _with_segment(
        db_session, normal_user, suggested_name="Old Row", confidence=0.9, suggestion_source=None
    )
    voice_file, _ = _with_segment(
        db_session,
        normal_user,
        suggested_name="Priya Patel",
        confidence=0.9,
        suggestion_source="voice_match",
    )
    typed_file, _ = _with_segment(
        db_session,
        normal_user,
        display_name="Alice",
        suggested_name="Bob",
        confidence=0.9,
        suggestion_source="llm_analysis",
    )
    weak_file, _ = _with_segment(
        db_session,
        normal_user,
        suggested_name="Weak Guess",
        confidence=0.6,
        suggestion_source="llm_analysis",
    )

    stats: dict = {}
    backfill_world.backfill.backfill_llm_suggested_labels(db_session, stats)

    assert sorted(backfill_world.seams["reindexed"]) == sorted([llm_file.id, legacy_file.id])
    assert sorted(backfill_world.facts) == sorted([llm_file.id, legacy_file.id])
    assert stats["speaker_label_backfill"] == 2
    for untouched in (voice_file, typed_file, weak_file):
        assert untouched.id not in backfill_world.seams["reindexed"]
    db_session.refresh(llm_speaker)
    assert llm_speaker.resolved_display_name == "SPEAKER_00"


def test_the_backfill_finishes_and_then_does_nothing(db_session, normal_user, backfill_world):
    _with_segment(
        db_session,
        normal_user,
        suggested_name="Joe Rogan",
        confidence=0.95,
        suggestion_source="llm_analysis",
    )
    first: dict = {}
    backfill_world.backfill.backfill_llm_suggested_labels(db_session, first)
    assert first["speaker_label_backfill"] == 1

    second: dict = {}
    backfill_world.backfill.backfill_llm_suggested_labels(db_session, second)
    assert second["speaker_label_backfill"] == "complete"
    queued_after_second = list(backfill_world.seams["reindexed"])

    third: dict = {}
    backfill_world.backfill.backfill_llm_suggested_labels(db_session, third)
    assert third["speaker_label_backfill"] == "complete"
    assert backfill_world.seams["reindexed"] == queued_after_second


def test_the_backfill_retries_a_file_it_could_not_queue(db_session, normal_user, backfill_world):
    from app.services.search.reindex_dispatch import ReindexDispatch

    backfill_world.seams["reindex_outcome"] = ReindexDispatch.FAILED
    file_a, _ = _with_segment(
        db_session,
        normal_user,
        suggested_name="Joe Rogan",
        confidence=0.95,
        suggestion_source="llm_analysis",
    )
    stats: dict = {}
    backfill_world.backfill.backfill_llm_suggested_labels(db_session, stats)
    assert stats["speaker_label_backfill"] == 0

    backfill_world.seams["reindex_outcome"] = ReindexDispatch.QUEUED
    backfill_world.seams["reindexed"].clear()
    again: dict = {}
    backfill_world.backfill.backfill_llm_suggested_labels(db_session, again)
    assert backfill_world.seams["reindexed"] == [file_a.id]


# ---------------------------------------------------- identity_source values (issue #1280)


def _identity_source_literals(source: str) -> set[str]:
    """String literals assigned to, passed as, or compared with ``identity_source``."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        values: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            if any(
                isinstance(t, ast.Attribute) and t.attr == "identity_source" for t in node.targets
            ):
                values.append(node.value)
        elif isinstance(node, ast.Call):
            values.extend(kw.value for kw in node.keywords if kw.arg == "identity_source")
            if isinstance(node.func, ast.Attribute) and node.func.attr == "in_":
                owner = node.func.value
                if isinstance(owner, ast.Attribute) and owner.attr == "identity_source":
                    for arg in node.args:
                        if isinstance(arg, ast.List | ast.Tuple | ast.Set):
                            values.extend(arg.elts)
        elif isinstance(node, ast.Compare):
            sides = [node.left, *node.comparators]
            if any(isinstance(s, ast.Attribute) and s.attr == "identity_source" for s in sides):
                values.extend(sides)
        for value in values:
            found.update(_possible_literals(value))
    return found


def test_the_identity_source_literal_scan_fires_and_stays_quiet():
    fires = (
        "def f(s):\n"
        "    s.identity_source = 'voiceprint'\n"
        "    if s.identity_source == 'bogus':\n"
        "        pass\n"
        "    q.filter(Speaker.identity_source.in_(['human', 'other']))\n"
        "    make(identity_source='third')\n"
    )
    quiet = "def f(s):\n    s.identity_source = HUMAN\n    s.other = 'voiceprint'\n"

    assert _identity_source_literals(fires) == {"voiceprint", "bogus", "human", "other", "third"}
    assert _identity_source_literals(quiet) == set()


def test_every_identity_source_the_app_writes_or_reads_is_a_known_value():
    from app.utils.speaker_identity_source import IDENTITY_SOURCES

    seen: set[str] = set()
    for path in _APP.rglob("*.py"):
        seen |= _identity_source_literals(path.read_text(encoding="utf-8"))

    assert seen <= IDENTITY_SOURCES, (
        f"unknown identity_source values: {sorted(seen - IDENTITY_SOURCES)}"
    )


def test_every_identity_source_value_is_classified_for_hiding():
    """A new value is a decision about what a switch hides, made here and not by accident."""
    from app.utils.speaker_identity_source import IDENTITY_SOURCES
    from app.utils.speaker_identity_source import hides_link
    from app.utils.speaker_identity_source import hides_name

    #: value -> (name hidden while off, link hidden while off)
    classified = {
        "voiceprint": (True, True),
        "voiceprint_link": (False, True),
        "human": (False, False),
    }

    assert set(classified) == set(IDENTITY_SOURCES), "classify the new identity_source value"
    for value, (name_hidden, link_hidden) in classified.items():
        assert hides_name(value, False) is name_hidden
        assert hides_link(value, False) is link_hidden
        assert not hides_name(value, True) and not hides_link(value, True)
    assert not hides_name(None, False) and not hides_link(None, False)  # unknown = a person's
