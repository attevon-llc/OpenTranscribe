"""The Markdown transcript export (issue #564): golden bytes, YAML safety, escaping.

The expected document below was written by hand BEFORE the builder existed, so it pins the
format rather than echoing whatever the implementation happened to produce. The front matter
is read by Obsidian-style vaults, so every scalar must survive ``yaml.safe_load`` whatever the
title contains; the body is escaped so transcript text can never become Markdown structure.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC
from datetime import datetime

import pytest
import yaml

from app.models.media import Comment
from app.models.media import Speaker
from app.models.media import TranscriptSegment
from app.models.user import User
from app.services.transcript_export_markdown import MarkdownMeta
from app.services.transcript_export_service import build_export_content

pytestmark = pytest.mark.unit

EXPORTED_AT = datetime(2026, 10, 25, 14, 2, 11, tzinfo=UTC)

GOLDEN = """\
---
title: "Q4 Budget Review"
date: "2024-03-15"
date_source: "filename"
duration: "01:12:30"
participants: ["Sarah Chen", "SPEAKER_01"]
language: "en"
opentranscribe_uuid: "0192f3a1-0000-7000-8000-000000000001"
exported_at: "2026-10-25T14:02:11Z"
redacted: false
---

# Q4 Budget Review

**Sarah Chen** [00:00:15]
I've reviewed the numbers. Costs are \\*up\\* 3%.

> **USER COMMENT** — Ana [00:01:01]: check slide 4

**SPEAKER\\_01** [00:01:02]
\\# not a heading
"""


def _segments_and_speakers():
    sarah = Speaker(id=1, name="SPEAKER_00", display_name="Sarah Chen")
    other = Speaker(id=2, name="SPEAKER_01")
    segments = [
        TranscriptSegment(
            start_time=15.0, end_time=20.0, text="I've reviewed the numbers.", speaker_id=1
        ),
        TranscriptSegment(start_time=20.0, end_time=25.0, text="Costs are *up* 3%.", speaker_id=1),
        TranscriptSegment(start_time=62.0, end_time=70.0, text="# not a heading", speaker_id=2),
    ]
    return segments, [sarah, other]


def _comment(timestamp: float, text: str, user: str) -> Comment:
    comment = Comment(timestamp=timestamp, text=text)
    comment.user = User(full_name=user)
    return comment


def _build(*, meta: MarkdownMeta | None, comments=None, **overrides) -> str:
    segments, speakers = _segments_and_speakers()
    return build_export_content(
        export_format="md",
        segments=segments,
        speakers=speakers,
        comments=comments or [],
        include_comments=bool(comments),
        markdown_meta=meta,
        **overrides,
    )


_BASE_META = MarkdownMeta(
    title="Q4 Budget Review",
    recorded_date="2024-03-15",
    date_source="filename",
    duration_seconds=4350.0,
    language="en",
    file_uuid="0192f3a1-0000-7000-8000-000000000001",
    exported_at=EXPORTED_AT,
    redacted=False,
)


def _meta(**overrides) -> MarkdownMeta:
    return replace(_BASE_META, **overrides)


def _front_matter(document: str) -> dict:
    assert document.startswith("---\n")
    block = document.split("\n---\n", 1)[0][4:]
    loaded = yaml.safe_load(block)
    assert isinstance(loaded, dict)
    return loaded


def test_the_document_matches_the_hand_written_golden() -> None:
    document = _build(meta=_meta(), comments=[_comment(61.0, "check slide 4", "Ana")])
    assert document == GOLDEN


def test_the_front_matter_round_trips_through_a_yaml_parser() -> None:
    parsed = _front_matter(_build(meta=_meta()))
    assert parsed == {
        "title": "Q4 Budget Review",
        "date": "2024-03-15",
        "date_source": "filename",
        "duration": "01:12:30",
        "participants": ["Sarah Chen", "SPEAKER_01"],
        "language": "en",
        "opentranscribe_uuid": "0192f3a1-0000-7000-8000-000000000001",
        "exported_at": "2026-10-25T14:02:11Z",
        "redacted": False,
    }


@pytest.mark.parametrize(
    "title",
    [
        'Quarterly: "Budget" review',
        "# not a comment",
        "- leading dash",
        "two\nlines: still one scalar",
        "tab\there",
        "back\\slash {braces} [brackets] & *star",
        "null",
        "2024-03-15",
        "line sep \u2028 para \u2029 nel \x85 del \x7f",
        "اجتماع الميزانية 会议 émoji 🎉",
        "'single' and `tick`",
    ],
)
def test_a_hostile_title_survives_the_yaml_round_trip_exactly(title: str) -> None:
    document = _build(meta=_meta(title=title))
    assert _front_matter(document)["title"] == title


def test_a_null_recorded_date_omits_both_date_keys() -> None:
    parsed = _front_matter(_build(meta=_meta(recorded_date=None, date_source="filename")))
    assert "date" not in parsed
    assert "date_source" not in parsed


def test_absent_optional_metadata_is_omitted_not_written_as_null() -> None:
    parsed = _front_matter(
        _build(
            meta=_meta(duration_seconds=None, language=None, file_uuid=None, exported_at=None),
        )
    )
    assert set(parsed) == {"title", "date", "date_source", "participants", "redacted"}


def test_redacted_true_is_recorded() -> None:
    assert _front_matter(_build(meta=_meta(redacted=True)))["redacted"] is True


def test_without_metadata_the_title_falls_back_to_the_filename() -> None:
    document = _build(meta=None, filename="meeting.wav")
    assert _front_matter(document)["title"] == "meeting.wav"
    assert "\n# meeting.wav\n" in document


def test_timestamps_and_speakers_can_be_switched_off() -> None:
    document = _build(meta=_meta(), include_timestamps=False, include_speakers=False)
    body = document.split("\n---\n", 1)[1]
    assert "[00:" not in body
    assert "Sarah" not in body
    assert "I've reviewed the numbers. Costs are \\*up\\* 3%." in body
    assert "participants" not in _front_matter(document)


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("a * b _ c ` d [e] <f> \\g", "a \\* b \\_ c \\` d \\[e\\] \\<f\\> \\\\g"),
        ("# heading", "\\# heading"),
        ("- item", "\\- item"),
        ("+ item", "\\+ item"),
        ("| cell |", "\\| cell |"),
        ("1. first", "1\\. first"),
        ("12) twelfth", "12\\) twelfth"),
        ("> quote", "\\> quote"),
        ("plain 3% text", "plain 3% text"),
    ],
)
def test_transcript_text_cannot_become_markdown_structure(raw: str, escaped: str) -> None:
    segments = [TranscriptSegment(start_time=0.0, end_time=1.0, text=raw, speaker_id=None)]
    document = build_export_content(
        export_format="md",
        segments=segments,
        speakers=[],
        comments=[],
        include_comments=False,
        include_speakers=False,
        include_timestamps=False,
        markdown_meta=_meta(),
    )
    assert document.rstrip("\n").endswith("\n" + escaped)


def test_a_multi_line_comment_stays_inside_its_blockquote() -> None:
    document = _build(meta=_meta(), comments=[_comment(61.0, "line one\n# line two", "Ana")])
    assert "> **USER COMMENT** — Ana [00:01:01]: line one\n> \\# line two\n" in document
