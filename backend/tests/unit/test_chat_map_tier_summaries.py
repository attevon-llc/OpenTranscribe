"""The #464 map-tier summary/digest tiering, and its staleness fingerprint.

``chat/mapreduce.scope_digest_hits`` decides, per file, whether the bounded-scope
map represents a recording by its (cheap, always-fresh) extractive digest or by
its (richer, possibly STALE) LLM summary. The property every test here defends
is the same one the module docstring states: the flag can only ever ADD a hit
shape, never remove the digest fallback's coverage guarantee — an absent,
unconfigured, failed, or stale summary must fall back to the digest exactly as
if the flag were off.

Two more properties get their own tests because getting either wrong is a
silent-wrong-answer or a silent-empty-answer, not a crash:

* The staleness comparison must self-heal rather than trust a summary on
  faith — ``tasks/summarization.fingerprint_transcript_segments`` and
  ``ingest_artifacts.service.source_fingerprint`` must compute the IDENTICAL
  hash over the identical segment shape, or the comparison is meaningless.
* A summary hit's out-of-range ``digest_section`` must survive the REAL
  ``redactor.mask_digests`` pipeline without either matching a real section
  (substituting the WRONG file's masked text) or failing closed to an empty
  answer (the two hazards the module docstring names explicitly).
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.services.chat.mapreduce import build_file_summaries
from app.services.chat.mapreduce import build_overview
from app.services.chat.mapreduce import scope_digest_hits
from app.services.chat.redactor import mask_digests
from app.services.search.chunk_retrieval import ChunkHit

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# scope_digest_hits tiering — mocked db, same shape as test_chat_mapreduce.py
# --------------------------------------------------------------------------- #


def _scope_db(rows):
    """A session whose file_facts (outer) join returns ``rows``.

    Two chained ``.filter()`` calls, matching the real query
    (``uuid.in_(...)`` then ``is_quarantined.is_(False)``) exactly as
    ``test_chat_mapreduce.py``'s own helper does.
    """
    db = MagicMock()
    query = db.query.return_value.outerjoin.return_value.filter.return_value
    query.filter.return_value.all.return_value = rows
    return db


def _digest_payload(sections: int) -> dict:
    return {
        "sections": [
            {"index": i, "text": f"Section {i}.", "start_time": i * 60.0} for i in range(sections)
        ]
    }


def _fresh_row(
    file_id: int,
    uuid: str,
    title: str,
    *,
    fingerprint: str = "fp-1",
    brief: str = "A fuller paragraph summarising the recording.",
    bluf: str = "One-line BLUF.",
    sections: int = 3,
):
    """A file whose stored summary's fingerprint MATCHES its current digest's."""
    summary_data = {
        "bluf": bluf,
        "brief_summary": brief,
        "metadata": {"source_fingerprint": fingerprint},
    }
    return (file_id, uuid, title, _digest_payload(sections), fingerprint, "completed", summary_data)


def _stale_row(
    file_id: int,
    uuid: str,
    title: str,
    *,
    digest_fingerprint: str = "fp-current",
    stored_fingerprint: str = "fp-old",
    sections: int = 3,
):
    """A file whose summary was generated against an EARLIER transcript state."""
    summary_data = {
        "brief_summary": "Stale prose describing an earlier version of this recording.",
        "metadata": {"source_fingerprint": stored_fingerprint},
    }
    return (
        file_id,
        uuid,
        title,
        _digest_payload(sections),
        digest_fingerprint,
        "completed",
        summary_data,
    )


def _absent_summary_row(
    file_id: int, uuid: str, title: str, *, fingerprint: str = "fp-1", sections: int = 2
):
    """A file with a current digest but no summary at all (never generated)."""
    return (file_id, uuid, title, _digest_payload(sections), fingerprint, None, None)


# ------------------------------------------------------- flag-off byte-identical


def test_flag_off_is_byte_identical_to_pre_464_behaviour():
    """Omitting ``use_summaries`` and passing it explicitly False must be the
    SAME output — and both must ignore a file's summary entirely, even a
    fresh-looking one, because that is what "off" has to mean."""
    row = _fresh_row(1, "uuid-1", "Recording 1", sections=3)
    legacy_shape = row[:4]  # (file_id, uuid, title, digest) — the pre-#464 query

    hits_default = scope_digest_hits(_scope_db([legacy_shape]), ["uuid-1"])
    hits_explicit_false = scope_digest_hits(
        _scope_db([legacy_shape]), ["uuid-1"], use_summaries=False
    )

    for hits in (hits_default, hits_explicit_false):
        assert len(hits) == 1
        assert hits[0].digest_section == 0
        assert hits[0].content == "Section 0."
        assert hits[0].chunk_index == -1
        assert hits.coverage == {"files_without_artifacts": 0}, (
            "flag-off coverage must carry no summary_hits key at all — "
            "the shape, not just the count, must be unchanged"
        )


def test_an_empty_scope_still_short_circuits_with_the_flag_on():
    db = MagicMock()
    hits = scope_digest_hits(db, [], use_summaries=True)
    assert list(hits) == []
    assert db.query.called is False


# ------------------------------------------------------------------ tiering


def test_mixed_tiering_fresh_absent_and_stale_resolve_correctly():
    """The scope: one fresh, one never-summarised, one stale. All three must
    be COVERED — the digest fallback's coverage guarantee is what the flag
    must never weaken — and only the fresh one gets a summary hit."""
    rows = [
        _fresh_row(1, "uuid-fresh", "Fresh", fingerprint="fp-1", sections=3),
        _absent_summary_row(2, "uuid-absent", "Absent", fingerprint="fp-2", sections=2),
        _stale_row(
            3,
            "uuid-stale",
            "Stale",
            digest_fingerprint="fp-3-current",
            stored_fingerprint="fp-3-old",
            sections=4,
        ),
    ]
    hits = scope_digest_hits(
        _scope_db(rows), ["uuid-fresh", "uuid-absent", "uuid-stale"], use_summaries=True
    )

    by_file: dict[str, list[ChunkHit]] = {}
    for hit in hits:
        by_file.setdefault(hit.file_uuid, []).append(hit)

    assert set(by_file) == {"uuid-fresh", "uuid-absent", "uuid-stale"}, "every file must be covered"

    fresh = by_file["uuid-fresh"]
    assert len(fresh) == 1
    assert fresh[0].digest_section == 3, "one past the file's 3 real sections (0, 1, 2)"
    assert fresh[0].content == "A fuller paragraph summarising the recording."
    assert fresh[0].chunk_index == -1
    assert fresh[0].start_time == 0.0
    assert fresh[0].end_time is None
    assert fresh[0].title == "Fresh"

    absent = by_file["uuid-absent"]
    assert len(absent) == 1
    assert absent[0].digest_section == 0, "no summary at all — the ordinary digest hit"
    assert absent[0].content == "Section 0."

    stale = by_file["uuid-stale"]
    assert len(stale) == 1
    assert stale[0].digest_section == 0, "fingerprint mismatch — self-heals to the digest"
    assert stale[0].content == "Section 0."

    assert hits.coverage["summary_hits"] == 1
    assert hits.coverage["files_without_artifacts"] == 0


def test_brief_summary_is_preferred_over_bluf_when_both_exist():
    row = _fresh_row(1, "uuid-1", "R", bluf="Short.", brief="Longer paragraph.")
    hits = scope_digest_hits(_scope_db([row]), ["uuid-1"], use_summaries=True)
    assert hits[0].content == "Longer paragraph."


def test_bluf_is_used_when_brief_summary_is_absent():
    row = _fresh_row(1, "uuid-1", "R", bluf="Only a BLUF here.", brief="")
    hits = scope_digest_hits(_scope_db([row]), ["uuid-1"], use_summaries=True)
    assert hits[0].content == "Only a BLUF here."


def test_a_file_with_no_digest_cannot_be_verified_fresh_even_with_a_summary():
    """Freshness compares against ``file_facts.source_fingerprint``. A file
    with NO ``file_facts`` row at all has nothing to compare against, so it
    is counted as ``files_without_artifacts`` exactly as before #464 —
    never a summary hit, however complete ``summary_data`` looks."""
    row = (
        1,
        "uuid-1",
        "Recording 1",
        None,  # no digest at all
        None,  # no source_fingerprint to compare against
        "completed",
        {
            "brief_summary": "Looks complete but cannot be verified as current.",
            "metadata": {"source_fingerprint": "fp-1"},
        },
    )
    hits = scope_digest_hits(_scope_db([row]), ["uuid-1"], use_summaries=True)
    assert list(hits) == []
    assert hits.coverage["files_without_artifacts"] == 1
    assert hits.coverage["summary_hits"] == 0


def test_an_empty_summary_shape_falls_back_to_the_digest():
    """A fresh fingerprint whose summary carries no usable text acts like an
    absent summary — the digest still covers the file rather than the map
    contributing an empty entry for it."""
    row = (
        1,
        "uuid-1",
        "Recording 1",
        _digest_payload(2),
        "fp-1",
        "completed",
        {"bluf": "", "brief_summary": "   ", "metadata": {"source_fingerprint": "fp-1"}},
    )
    hits = scope_digest_hits(_scope_db([row]), ["uuid-1"], use_summaries=True)
    assert len(hits) == 1
    assert hits[0].digest_section == 0
    assert hits[0].content == "Section 0."
    assert hits.coverage["summary_hits"] == 0


@pytest.mark.parametrize("status", ["processing", "failed", "not_configured", "disabled"])
def test_every_non_completed_status_falls_back_to_the_digest(status):
    row = (
        1,
        "uuid-1",
        "Recording 1",
        _digest_payload(1),
        "fp-1",
        status,
        {"brief_summary": "text", "metadata": {"source_fingerprint": "fp-1"}},
    )
    hits = scope_digest_hits(_scope_db([row]), ["uuid-1"], use_summaries=True)
    assert hits[0].digest_section == 0
    assert hits.coverage["summary_hits"] == 0


# --------------------------------------------------------------------------- #
# D6: the no-LLM deployment still gets a complete map-tier overview
# --------------------------------------------------------------------------- #


def test_d6_the_map_tier_composes_without_an_llm():
    """#403 D6. The map tier must not silently require a provider it was
    built specifically to work without — with or without the #464 tiering."""
    rows = [_fresh_row(1, "uuid-1", "Recording 1", sections=3)]
    hits = scope_digest_hits(_scope_db(rows), ["uuid-1"], use_summaries=True)
    summaries = build_file_summaries(None, hits, masked_text={id(h): "masked prose" for h in hits})

    overview = build_overview("summarise this collection", summaries, llm=None, use_llm=True)

    assert overview.reducer == "code"
    assert overview.llm_calls == 0
    assert "recordings: 1" in overview.block


# --------------------------------------------------------------------------- #
# Staleness fingerprint parity (tasks/summarization.py <-> ingest_artifacts)
# --------------------------------------------------------------------------- #


class _StubSpeaker:
    def __init__(self, name: str):
        self.name = name
        self.display_name = name
        self.suggested_name = None
        self.confidence = None


class _StubSegment:
    def __init__(self, seg_id: int, text: str, start: float, end: float, speaker_name: str | None):
        self.id = seg_id
        self.text = text
        self.start_time = start
        self.end_time = end
        self.speaker = _StubSpeaker(speaker_name) if speaker_name else None


def _stub_segments() -> list[_StubSegment]:
    return [
        _StubSegment(1, "Hello there.", 0.0, 2.0, "Dana"),
        _StubSegment(2, "General Kenobi.", 2.0, 4.5, "Bo"),
        _StubSegment(3, "You are a bold one.", 4.5, 6.0, "Dana"),
    ]


def test_summarization_fingerprint_matches_ingest_artifacts_for_identical_segments():
    """The staleness comparison is only meaningful if BOTH sides hash the
    identical shape. This proves ``fingerprint_transcript_segments`` (tasks/
    summarization.py) and ``ingest_artifacts.service.source_fingerprint`` are
    interchangeable when fed the same segments — not merely that each
    produces SOME deterministic string."""
    from app.services.ingest_artifacts.service import source_fingerprint
    from app.tasks.summarization import fingerprint_transcript_segments

    segments = _stub_segments()
    # Every stub segment in this fixture is built with a speaker_name (see
    # `_stub_segments`), so this holds by construction — asserting it, rather
    # than assuming it, is what lets `s.speaker.display_name` below be checked
    # instead of merely hoped for.
    assert all(s.speaker is not None for s in segments)

    from_task = fingerprint_transcript_segments(segments)
    from_ingest = source_fingerprint(
        [
            {
                "id": s.id,
                "text": s.text,
                "start_time": s.start_time,
                "end_time": s.end_time,
                "speaker": s.speaker.display_name,
            }
            for s in segments
            if s.speaker is not None
        ]
    )

    assert from_task == from_ingest


def test_summarization_fingerprint_changes_when_a_segment_is_edited():
    """MUST-FIRE: a fingerprint that never changes can never mark a summary
    stale, which would defeat the whole staleness gate silently."""
    from app.tasks.summarization import fingerprint_transcript_segments

    original = fingerprint_transcript_segments(_stub_segments())

    edited = _stub_segments()
    edited[0].text = "Hello THERE, edited."
    changed = fingerprint_transcript_segments(edited)

    assert original != changed


def test_summarization_fingerprint_changes_when_a_speaker_is_renamed():
    """A speaker rename must invalidate a summary's freshness too — matching
    ``ingest_artifacts.service.source_fingerprint``'s own documented contract
    (a rename invalidates the file_facts row by the identical mechanism)."""
    from app.tasks.summarization import fingerprint_transcript_segments

    original = fingerprint_transcript_segments(_stub_segments())

    renamed = _stub_segments()
    speaker = renamed[0].speaker
    # `_stub_segments()`'s first segment is built with speaker_name="Dana" — this
    # holds by construction; asserting it is what makes the mutation below checked
    # rather than merely assumed not to be renaming a None.
    assert speaker is not None
    speaker.display_name = "Dana Whitfield"
    changed = fingerprint_transcript_segments(renamed)

    assert original != changed


# --------------------------------------------------------------------------- #
# The out-of-range digest_section survives the REAL redactor.mask_digests
# pipeline — the design claim in scope_digest_hits' docstring, proven not
# assumed.
# --------------------------------------------------------------------------- #


@contextmanager
def _one_session(db):
    yield db


def _factory(db):
    return lambda: _one_session(db)


def _summary_hit(
    section_count: int, *, content: str = "LLM-written prose about the whole file."
) -> ChunkHit:
    return ChunkHit(
        file_uuid="11111111-1111-1111-1111-111111111111",
        file_id=1,
        chunk_index=-1,
        content=content,
        title="Weekly sync",
        start_time=0.0,
        end_time=None,
        digest_section=section_count,  # one PAST the real sections — see mapreduce.py
    )


def _cfg(
    *, enabled=True, redact_before_llm=True, redact_before_llm_locked=False, categories=("pii",)
):
    return SimpleNamespace(
        enabled=enabled,
        redact_before_llm=redact_before_llm,
        redact_before_llm_locked=redact_before_llm_locked,
        enabled_categories=set(categories),
    )


def _facts_db_for_summary_hit(sections_count: int, *, status: str = "completed"):
    """A session whose scan/digest probes describe a file with EXACTLY
    ``sections_count`` real sections (0..sections_count-1) — so a summary
    hit's ``digest_section == sections_count`` is provably out of range."""
    from app.core import constants as C  # noqa: N812

    resolved_status = C.REDACTION_STATUS_DONE if status == "completed" else status
    digest_payload = {
        "sections": [
            {
                "index": i,
                "sentences": [
                    {
                        "text": f"Section {i}.",
                        "provenance": {"kind": "segment_ids", "segment_ids": [i + 1]},
                    }
                ],
            }
            for i in range(sections_count)
        ]
    }

    def _query(*targets):
        key = " ".join(str(t) for t in targets)
        result = MagicMock()
        if "redaction_status" in key:
            result.filter.return_value.first.return_value = SimpleNamespace(
                id=1, redaction_status=resolved_status, redaction_coverage=None, language="en"
            )
        elif "digest" in key:
            result.filter.return_value.first.return_value = (digest_payload,)
        else:
            result.filter.return_value.order_by.return_value.all.return_value = []
        return result

    db = MagicMock()
    db.query.side_effect = _query
    return db


def test_a_summary_hits_out_of_range_section_declines_provenance_not_fails_closed():
    """MUST-FIRE-shaped: if `_digest_sentences` ever coincidentally matched a
    real section for a summary hit's sentinel index, this would substitute
    that section's text for the summary's own — a silent wrong-content bug,
    not a crash. Proven against the REAL `mask_digests`/`_gather_digest_plans`
    pipeline, not assumed from reading the source."""
    db = _facts_db_for_summary_hit(sections_count=3)
    hit = _summary_hit(section_count=3)

    with (
        patch("app.services.redaction.config.resolve_effective_config", return_value=_cfg()),
        patch("app.services.chat.redactor._mask_inline", return_value="[MASKED SUMMARY]") as inline,
    ):
        masked = mask_digests(_factory(db), [hit], user_id=1)

    inline.assert_called_once()
    # The RAW summary content — not a real section's text — is what reached
    # the fallback, proving no wrong-section substitution occurred.
    assert inline.call_args[0][0] == hit.content
    assert masked[0].content == "[MASKED SUMMARY]"
    assert masked[0].content != "", "declining provenance must not empty the tier"


def test_summary_hit_is_masked_for_a_remote_or_unconfigured_provider():
    db = _facts_db_for_summary_hit(sections_count=2)
    hit = _summary_hit(section_count=2)

    with (
        patch("app.services.redaction.config.resolve_effective_config", return_value=_cfg()),
        patch("app.services.chat.redactor._mask_inline", return_value="[MASKED]") as inline,
    ):
        masked = mask_digests(_factory(db), [hit], user_id=1, unmask_for_local=False)

    inline.assert_called_once()
    assert masked[0].content == "[MASKED]"
    assert masked[0].content != hit.content


def test_summary_hit_is_unmasked_for_a_local_provider():
    """Reuses the ALREADY-LANDED locality exemption
    (``redaction.llm_guard.is_local_provider`` / ``mask_digests``'s
    ``unmask_for_local``) — no second locality check is written for the map
    tier. The exemption applies at the SAME ``_gather`` gate every other
    masked call goes through, before any per-hit routing happens at all."""
    db = _facts_db_for_summary_hit(sections_count=2)
    hit = _summary_hit(section_count=2)

    with patch("app.services.redaction.config.resolve_effective_config", return_value=_cfg()):
        masked = mask_digests(_factory(db), [hit], user_id=1, unmask_for_local=True)

    assert masked[0].content == hit.content


def test_summary_hit_stays_masked_for_a_local_provider_under_the_admin_force_floor():
    """The admin force floor (``redact_before_llm_locked``) always wins the
    local exemption — matching every other masker call site."""
    db = _facts_db_for_summary_hit(sections_count=2)
    hit = _summary_hit(section_count=2)

    with (
        patch(
            "app.services.redaction.config.resolve_effective_config",
            return_value=_cfg(redact_before_llm_locked=True),
        ),
        patch("app.services.chat.redactor._mask_inline", return_value="[MASKED]") as inline,
    ):
        masked = mask_digests(_factory(db), [hit], user_id=1, unmask_for_local=True)

    inline.assert_called_once()
    assert masked[0].content == "[MASKED]"
