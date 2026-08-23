"""The Citation schema widening (issue #464 amendment a) — and the bug it fixes.

**The bug, reproduced first.** Pydantic v2 silently drops a dict key that is not
a declared model field (no ``extra="forbid"`` here, matching this repo's
no-aliases-anywhere convention). ``chat/citations.py.build_citation`` already put
``kind`` and ``digest_section`` into the citation dict at STREAM time
(``KIND_CHUNK``/``KIND_DIGEST``, ``chunk.source.digest_section``) — but until
this widening, neither field existed on ``schemas.chat.Citation``, and
``ChatMessageOut.citations: list[Citation]`` validates straight from the
persisted ``chat_message.citations`` JSONB column every time a conversation is
reloaded (``models/chat.py``). So a digest citation rendered correctly labelled
DURING the stream and silently lost that label the moment the SAME message was
read back after a page refresh — with no error anywhere, because Pydantic
dropping an unknown key is not a validation failure.

The fix is to widen the schema to the FULL union up front — chunk, digest,
summary (#464), and the document-plane fields (``page``/``section_path``/
``char_start``/``char_end``) a later lane is expected to add citations for —
so growing the union again never has to re-migrate an already-persisted
message a second time.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.schemas.chat import ChatMessageOut
from app.schemas.chat import Citation

pytestmark = pytest.mark.unit


def _message_row(citations: list[dict]) -> SimpleNamespace:
    """A stand-in for the persisted ``ChatMessage`` ORM row.

    Only the fields ``ChatMessageOut`` actually reads — ``from_attributes=True``
    reads by attribute, so a bare ``SimpleNamespace`` is a faithful stand-in for
    the ORM row without a database.
    """
    return SimpleNamespace(
        uuid="11111111-1111-1111-1111-111111111111",
        role="assistant",
        content="Here is the answer. [1]",
        reasoning_content=None,
        citations=citations,
        msg_metadata=None,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        tokens_estimated=False,
        provider="openai",
        model="gpt-4",
        status="complete",
        error=None,
        created_at=None,
    )


# --------------------------------------------------------------------------- #
# The bug: pinned as a must-fire — these fields WOULD vanish without the widening
# --------------------------------------------------------------------------- #


def test_kind_and_digest_section_survive_a_reload_for_a_digest_citation():
    """MUST-FIRE reproduction: before the widening, both fields were silently
    dropped by `Citation`'s validation and this assertion would fail."""
    raw = {
        "id": 1,
        "kind": "digest",
        "file_uuid": "aaaaaaaa-0000-0000-0000-000000000000",
        "title": "Weekly sync",
        "chunk_index": -1,
        "digest_section": 2,
        "start_time": 125.5,
        "end_time": None,
        "speaker": None,
        "snippet": "We agreed the budget.",
    }
    reloaded = ChatMessageOut.model_validate(_message_row([raw]))

    assert reloaded.citations is not None
    citation = reloaded.citations[0]
    assert citation.kind == "digest"
    assert citation.digest_section == 2


def test_a_summary_citation_survives_a_reload_labelled_as_a_summary():
    """The #464 kind itself: a labelled interpretation must still read as one
    after a reload, not silently fall back to being rendered as a quote."""
    raw = {
        "id": 1,
        "kind": "summary",
        "file_uuid": "bbbbbbbb-0000-0000-0000-000000000000",
        "title": "Quarterly review",
        "chunk_index": -1,
        "digest_section": 4,  # the sentinel index scope_digest_hits uses
        "start_time": 0.0,
        "end_time": None,
        "speaker": None,
        "snippet": "The team is on track for the migration deadline.",
    }
    reloaded = ChatMessageOut.model_validate(_message_row([raw]))

    citation = reloaded.citations[0]
    assert citation.kind == "summary"
    assert citation.digest_section == 4
    assert citation.speaker is None, "a summary is never one person's words"


# --------------------------------------------------------------------------- #
# Every kind round-trips, including forward-compat document-plane fields
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "kind,extra",
    [
        (None, {}),  # legacy: persisted before the `kind` field existed at all
        ("chunk", {"speaker": "Dana Whitfield"}),
        ("digest", {"digest_section": 0, "speaker": None}),
        ("summary", {"digest_section": 3, "speaker": None, "start_time": None}),
        # Forward-compat: a later lane's kind, exercised here so THAT lane's
        # persisted messages already round-trip against this schema.
        (
            "document",
            {
                "page": 5,
                "section_path": "3.2 Risk Register",
                "char_start": 1200,
                "char_end": 1450,
                "start_time": None,
            },
        ),
    ],
)
def test_every_kind_round_trips_through_persistence_and_reload(kind, extra):
    raw = {
        "id": 1,
        "file_uuid": "cccccccc-0000-0000-0000-000000000000",
        "title": "A recording",
        "chunk_index": 0,
        "start_time": 42.0,
        "end_time": 50.0,
        "speaker": None,
        "snippet": "some text",
        **extra,
    }
    if kind is not None:
        raw["kind"] = kind

    reloaded = ChatMessageOut.model_validate(_message_row([raw]))
    citation = reloaded.citations[0]

    assert citation.kind == (kind or "chunk"), "an absent kind must read as chunk (pre-#403 data)"
    for field, value in extra.items():
        assert getattr(citation, field) == value, field


def test_document_plane_fields_default_to_none_for_every_kind_this_lane_emits():
    """Nothing this lane ships ever sets page/section_path/char_start/char_end
    — pinned so a later lane's addition is the first thing to populate them."""
    for kind in (None, "chunk", "digest", "summary"):
        raw = {
            "id": 1,
            "file_uuid": "dddddddd-0000-0000-0000-000000000000",
            "title": "R",
            "chunk_index": 0,
            "start_time": 1.0,
            "end_time": 2.0,
            "speaker": None,
            "snippet": "s",
        }
        if kind is not None:
            raw["kind"] = kind
        citation = Citation(**raw)
        assert citation.page is None
        assert citation.section_path is None
        assert citation.char_start is None
        assert citation.char_end is None


def test_start_time_none_is_a_first_class_case_not_a_zero_sentinel():
    """A kind with no natural timestamp (a summary, or a future no-timestamp
    `recurrence` kind) must be representable as `None`, not coerced to `0` —
    rendering a missing timestamp as 0 would look like a working "jump to
    0:00" link that just happens to be wrong."""
    citation = Citation(id=1, file_uuid="x", kind="summary", start_time=None)
    assert citation.start_time is None


def test_multiple_citations_of_different_kinds_round_trip_independently_in_one_message():
    """A single answer can cite a chunk, a digest, and a summary together —
    each must keep its own shape through one persisted message."""
    raw = [
        {
            "id": 1,
            "kind": "chunk",
            "file_uuid": "u-1",
            "title": "R1",
            "chunk_index": 3,
            "start_time": 12.0,
            "end_time": 20.0,
            "speaker": "Dana",
            "snippet": "a",
        },
        {
            "id": 2,
            "kind": "digest",
            "file_uuid": "u-1",
            "title": "R1",
            "chunk_index": -1,
            "digest_section": 1,
            "start_time": 300.0,
            "end_time": None,
            "speaker": None,
            "snippet": "b",
        },
        {
            "id": 3,
            "kind": "summary",
            "file_uuid": "u-2",
            "title": "R2",
            "chunk_index": -1,
            "digest_section": 2,
            "start_time": 0.0,
            "end_time": None,
            "speaker": None,
            "snippet": "c",
        },
    ]
    reloaded = ChatMessageOut.model_validate(_message_row(raw))
    kinds = [c.kind for c in reloaded.citations]
    assert kinds == ["chunk", "digest", "summary"]
