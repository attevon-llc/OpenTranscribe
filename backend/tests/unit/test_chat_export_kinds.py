"""Markdown export goes kind-aware (issue #464 amendment b).

Before this, `_render_markdown` rendered EVERY citation identically — a
speaker quote at a timestamp with a `/files/{uuid}?t=N` link — which is wrong
for anything that isn't a transcript chunk: a summary citation is
machine-generated prose about the recording, not a quote from it.
`_render_citation` is unit-tested directly here — no database, no live
conversation — because it is pure string formatting over a citation dict.
"""

from __future__ import annotations

import pytest

from app.api.endpoints.chat.export import _render_citation

pytestmark = pytest.mark.unit


def _chunk_citation(**overrides) -> dict:
    base = {
        "id": 1,
        "kind": "chunk",
        "file_uuid": "aaaaaaaa-0000-0000-0000-000000000000",
        "title": "Weekly sync",
        "chunk_index": 3,
        "start_time": 125.5,
        "end_time": 160.0,
        "speaker": "Dana Whitfield",
        "snippet": "We agreed the budget.",
    }
    return {**base, **overrides}


# --------------------------------------------------------------------------- #
# chunk / digest — unchanged behaviour, pinned so the new branches can't
# silently change what already worked
# --------------------------------------------------------------------------- #


def test_a_chunk_citation_renders_a_timestamped_quote_unchanged():
    lines = _render_citation(_chunk_citation())
    assert lines[0] == "- `[1]` **Weekly sync** — Dana Whitfield at 2:05"
    assert lines[1] == "  /files/aaaaaaaa-0000-0000-0000-000000000000?t=125"
    assert lines[2] == "  > We agreed the budget."


def test_a_legacy_citation_with_no_kind_is_treated_as_a_chunk():
    raw = _chunk_citation()
    del raw["kind"]
    lines = _render_citation(raw)
    assert "Dana Whitfield at" in lines[0]


def test_a_digest_citation_is_unchanged_still_timestamped_no_speaker():
    lines = _render_citation(
        _chunk_citation(kind="digest", speaker=None, digest_section=2, start_time=300.0)
    )
    assert lines[0] == "- `[1]` **Weekly sync** — Unknown speaker at 5:00"
    assert "?t=300" in lines[1]


# --------------------------------------------------------------------------- #
# summary (#464) — labelled, no timestamp, links to the summary view
# --------------------------------------------------------------------------- #


def test_a_summary_citation_is_labelled_never_a_timestamped_quote():
    lines = _render_citation(
        _chunk_citation(
            kind="summary",
            speaker=None,
            digest_section=3,
            start_time=0.0,
            snippet="The team is on track for the migration deadline.",
        )
    )
    assert lines[0] == "- `[1]` **Weekly sync** — AI-generated summary"
    assert "at " not in lines[0], "a summary has no timestamp to report"
    assert lines[1] == "  /files/aaaaaaaa-0000-0000-0000-000000000000?view=summary&section=3"
    # Italicized, not blockquoted — a blockquote reads as "these were the
    # words", which is exactly what a summary is not.
    assert lines[2] == "  *The team is on track for the migration deadline.*"
    assert not any(line.startswith("  > ") for line in lines)


def test_a_summary_citation_link_omits_section_when_absent():
    lines = _render_citation(_chunk_citation(kind="summary", digest_section=None, speaker=None))
    assert lines[1] == "  /files/aaaaaaaa-0000-0000-0000-000000000000?view=summary"
    assert "section=" not in lines[1]


def test_a_summary_citation_never_leaks_a_timestamp_query_param():
    lines = _render_citation(
        _chunk_citation(kind="summary", speaker=None, start_time=999.0, digest_section=1)
    )
    joined = "\n".join(lines)
    assert "t=999" not in joined
    assert "t=0" not in joined


def test_no_snippet_line_when_the_citation_has_none():
    lines = _render_citation(_chunk_citation(snippet=""))
    assert len(lines) == 2


def test_no_snippet_line_for_a_snippet_less_summary():
    lines = _render_citation(_chunk_citation(kind="summary", speaker=None, snippet=""))
    assert len(lines) == 2
