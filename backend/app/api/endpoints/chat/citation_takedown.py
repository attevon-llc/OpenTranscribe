"""Quarantine re-check for PERSISTED chat citations, at read time (issue #817).

``chat/messages.py::list_messages`` and ``chat/export.py::export_conversation`` both
replay a message's stored ``citations[].snippet`` verbatim — and a citation's snippet
is unredacted transcript text whenever ``chat/service.py`` decided the answering model
was local (``redaction.llm_guard.is_local_provider``). A conversation persists its
citations at answer time; nothing re-checks them against the file's *current*
quarantine state on a later read. So a file taken down AFTER a conversation cited it
stayed fully readable through that conversation's history and export forever — the
same class of gap #817 already closed for the speaker-cluster media-preview route and
the transcript-segment endpoint, one plane over.

**Decision (do not relitigate): drop the WHOLE citation entry, not just ``snippet``.**
``title`` and ``file_uuid`` are themselves part of the takedown subject — a citation
that kept its title and link but blanked its snippet would still name the taken-down
recording and point a reader at it. Dropping the entry leaves a dangling ``[n]`` marker
in the assistant's ``content``; that is an accepted, already-documented property of this
citation scheme (``chat/citations.py``'s ``extract_used_citations``, and the referenced
marker is never re-numbered here either — renumbering would require rewriting
``content``, which the next paragraph rules out).

**Decision: leave ``message.content`` alone.** It is the assistant's own
already-masked prose (masked at persist time by ``chat/output_redactor``), not
transcript text, and rewriting stored history to scrub a dangling citation marker is a
different, larger feature than "don't replay a taken-down file's transcript text" —
out of scope here.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.media import MediaFile


def _quarantined_uuids(db: Session, file_uuids: set[str]) -> set[str]:
    """The subset of ``file_uuids`` that are currently quarantined."""
    if not file_uuids:
        return set()
    rows = (
        db.query(MediaFile.uuid)
        .filter(MediaFile.uuid.in_(file_uuids), MediaFile.is_quarantined.is_(True))
        .all()
    )
    return {str(row[0]) for row in rows}


def drop_quarantined_citations(db: Session, citations: list[dict], *, is_admin: bool) -> list[dict]:
    """Drop citations naming a currently-quarantined file.

    Args:
        db: Database session.
        citations: A message's persisted citation payloads.
        is_admin: Admins keep full visibility for takedown review — the same
            bypass every other quarantine-aware read surface applies.

    Returns:
        ``citations`` unchanged when ``is_admin`` or empty; otherwise a new list
        with every citation whose ``file_uuid`` is quarantined removed.
    """
    if is_admin or not citations:
        return citations

    uuids = {c["file_uuid"] for c in citations if c.get("file_uuid")}
    quarantined = _quarantined_uuids(db, uuids)
    if not quarantined:
        return citations

    return [c for c in citations if str(c.get("file_uuid")) not in quarantined]


def drop_quarantined_citations_bulk(
    db: Session, list_of_citation_lists: list[list[dict]], *, is_admin: bool
) -> list[list[dict]]:
    """The multi-message sibling of :func:`drop_quarantined_citations`.

    Resolves the quarantined-uuid set with ONE query across every list rather
    than one query per message — the shape ``list_messages`` needs, replaying
    up to 500 messages per page.

    Args:
        db: Database session.
        list_of_citation_lists: One citations list per message, in the same
            order they must be returned in.
        is_admin: Admins keep full visibility for takedown review.

    Returns:
        A new list of citation lists, each filtered exactly as
        :func:`drop_quarantined_citations` would filter it alone.
    """
    if is_admin or not list_of_citation_lists:
        return list_of_citation_lists

    uuids: set[str] = set()
    for citations in list_of_citation_lists:
        uuids.update(c["file_uuid"] for c in citations if c.get("file_uuid"))

    quarantined = _quarantined_uuids(db, uuids)
    if not quarantined:
        return list_of_citation_lists

    return [
        [c for c in citations if str(c.get("file_uuid")) not in quarantined]
        for citations in list_of_citation_lists
    ]
