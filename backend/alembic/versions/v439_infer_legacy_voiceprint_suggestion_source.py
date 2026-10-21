"""Record the voiceprint provenance of legacy speaker suggestions that can prove it.

Why
---
Owner decision 2026-10-12: a speaker's canonical label accepts a ``suggested_name`` only
when ``suggestion_source`` says a voiceprint matcher wrote it (``voice_match`` /
``profile_match``). Rows written before ``v091`` added the column carry no source at all,
and the label rule treats "no source" as unknown, i.e. never the name. Before the
column, the LLM speaker-ID task and the voiceprint matchers wrote ``suggested_name`` and
``confidence`` identically (same 0-1 scale, no marker), so for most such rows the source
genuinely cannot be recovered. A speaker whose name came from a voice match before the
upgrade would come back as ``SPEAKER_01``.

Two shapes ARE provable from Postgres, because only a non-LLM path can produce them. A
row is stamped only when its ``suggestion_source`` is NULL and it has a suggestion, and:

* ``profile_match`` -- it is linked to a speaker profile (``profile_id``) and the
  suggestion IS that profile's name. No LLM path, legacy or current, has ever written
  ``profile_id``; the link comes from a human or a voiceprint match.
* ``voice_match`` -- a ``speaker_match`` row (written only by embedding similarity) pairs
  it with another speaker of the same owner whose ``display_name`` IS the suggestion, at
  exactly the speaker's own ``confidence``. Both legacy voiceprint writers stored the
  suggestion, the speaker's confidence and that match row from one similarity value; an
  LLM confidence equal to a stored cosine score to the same name is not a coincidence
  that happens. A match row raised later, or a partner renamed since, no longer matches
  exactly and the row is left alone.

Left alone on purpose (fail closed): every other NULL-source row, including any with a
``verified`` flag but no profile link, and every row that already has a source
(``llm_analysis`` above all). Those stay suggestions a human verifies; the
``speaker_label_backfill`` arm of ``search_index_maintenance`` re-indexes them under the
diarizer label. Only ``suggestion_source`` is written: no name, confidence or link
changes, so the label of a stamped row is exactly what it was before the upgrade.

Idempotent: a stamped row has a source, so a second pass matches nothing. Downgrade is a
no-op: nothing records which rows were stamped, and the stamp states a fact.

Detection: data-only, so like ``v434`` the startup runner's arm keys on the ABSENCE of
rows this revision would stamp and reads the predicate from this file.

Affects deployments with speaker data from before the suggestion-source column.
Community edition: no difference.

Revision ID: v439_infer_legacy_voiceprint_suggestion_source
Revises: v438_add_speaker_correction_carryover
Create Date: 2026-10-14
"""

from alembic import op

revision = "v439_infer_legacy_voiceprint_suggestion_source"
down_revision = "v438_add_speaker_correction_carryover"
branch_labels = None
depends_on = None

_LEGACY_SUGGESTION = """
    s.suggestion_source IS NULL
    AND coalesce(s.suggested_name, '') <> ''
"""

#: Linked to a profile, and the suggestion is that profile's name. ``s`` is ``speaker``.
PROFILE_MATCH_WHERE = f"""
    {_LEGACY_SUGGESTION}
    AND s.profile_id IS NOT NULL
    AND EXISTS (
        SELECT 1 FROM speaker_profile p
         WHERE p.id = s.profile_id AND p.name = s.suggested_name
    )
"""

#: A stored voiceprint match to a same-owner speaker named the suggestion, at the
#: speaker's own confidence. ``s`` is ``speaker``.
VOICE_MATCH_WHERE = f"""
    {_LEGACY_SUGGESTION}
    AND s.confidence IS NOT NULL
    AND EXISTS (
        SELECT 1
          FROM speaker_match m
          JOIN speaker o
            ON o.id = CASE WHEN m.speaker1_id = s.id THEN m.speaker2_id ELSE m.speaker1_id END
         WHERE (m.speaker1_id = s.id OR m.speaker2_id = s.id)
           AND o.user_id = s.user_id
           AND o.display_name = s.suggested_name
           AND m.confidence = s.confidence
    )
"""

#: Profile first: a row satisfying both is a profile match, as the live matcher records it.
STAMP_STATEMENTS = (
    f"UPDATE speaker s SET suggestion_source = 'profile_match' WHERE {PROFILE_MATCH_WHERE}",
    f"UPDATE speaker s SET suggestion_source = 'voice_match' WHERE {VOICE_MATCH_WHERE}",
)

#: Whether any row is still unstamped: the startup runner's detection marker.
DETECT_SQL = (
    f"SELECT EXISTS(SELECT 1 FROM speaker s WHERE ({PROFILE_MATCH_WHERE}) OR ({VOICE_MATCH_WHERE}))"
)


def upgrade():
    for statement in STAMP_STATEMENTS:
        op.execute(statement)


def downgrade():
    """No-op (see the module docstring): nothing records which rows were stamped."""
