"""Centralized application enums.

Import enums from here instead of from model files to avoid circular
imports and to provide a single source of truth.
"""

import enum


# NOT StrEnum (UP042): str(FileStatus.X) == "FileStatus.X" is load-bearing — the
# status-detail API pins it as a characterization (test_files_management.py) and
# the redaction-guard / on-demand-analytics `str(status)` comparisons would
# silently change behavior. Convert deliberately in its own change, not a codemod.
class FileStatus(str, enum.Enum):  # noqa: UP042
    """Processing status for media files."""

    PENDING = "pending"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    ORPHANED = "orphaned"
    # Abuse / DMCA / safe-harbor takedown. Distinct from the processing lifecycle
    # above: a file in ANY prior state can be quarantined. The authoritative
    # takedown flag is the dedicated ``MediaFile.is_quarantined`` column (so a
    # completed file's processing status survives a takedown and is restored on
    # release); this enum value is the surfaced display status while held.
    QUARANTINED = "quarantined"


#: ``StrEnum``, unlike its neighbour ``FileStatus`` above. Nothing pins ``str(...)`` of
#: this one — it is new, and the values go to the database and the wire as ``.value`` —
#: so there is no reason to carry ``FileStatus``'s deliberate exception.
class RecordedDateSource(enum.StrEnum):
    """Where ``media_file.recorded_date`` came from. **Never absent when a date is set.**

    A derived date whose origin the user cannot see, or correct, is worse than no
    date — it answers "3 meetings in March" with confidence when the truth is 5 and
    offers no way to find out. So the source travels with the value everywhere: in
    the column (``ck_media_file_recorded_date_provenance`` makes a bare date
    unrepresentable), on the wire, in the UI, and in the chat coverage block.

    Members are declared in the order the sources were *specified* (container →
    filename → transcript → llm, then the two that are not derivations). Which one
    **wins** is :data:`PRECEDENCE`, stated separately and on purpose: precedence is a
    policy that has to be explicit, recorded and surfaceable, not an artefact of the
    order somebody happened to write the members in.
    """

    #: The container said so — ffprobe/exiftool ``creation_time``. Cheapest and, for
    #: a file that has not been re-encoded, the most reliable. For an ingest path
    #: with no media (the eval-corpus injector) the source record plays this role.
    CONTAINER = "container"
    #: Parsed out of the filename. How most archives actually encode the date.
    FILENAME = "filename"
    #: The meeting stated its own date out loud. The source unique to this product —
    #: we have the words — and deterministic, so it costs no LLM and works on an
    #: ``LLM_PROVIDER``-empty deployment (#403 D6).
    TRANSCRIPT = "transcript"
    #: LLM extraction from the digest. **Seam only — nothing produces this yet.**
    #: Reserved so the opt-in Stage 7 path does not need a schema change.
    LLM = "llm"
    #: A human typed it. Outranks every derived source and is never overwritten by a
    #: later re-derivation (``recorded_date_locked``).
    MANUAL = "manual"
    #: Every source was absent. Recorded rather than left NULL so "we looked and
    #: found nothing" is distinguishable from "we never looked", which is the
    #: difference between an honest gap and an unrun resolver.
    NONE = "none"


#: Precedence, strongest first — the policy, written down once.
#:
#: ``MANUAL`` first because a hand-entered date is the only value here nobody derived.
#: The rest follow the owner's specified ordering: the container is the cheapest and
#: most reliable signal, a filename is deterministic and is how archives encode a
#: date, the transcript is the source unique to us, and the LLM is the fallback for
#: when the first three are absent or disagree.
#:
#: **Disagreement is normal, not an error** — a recording made on the 14th about the
#: 15th's meeting is an ordinary thing. So losing candidates are kept
#: (``recorded_date_candidates``) and a conflict is surfaced to the user rather than
#: silently resolved by this tuple. Picking one and discarding the others is what
#: turns a helpful inference into an unfalsifiable claim.
#:
#: ``NONE`` is excluded: it is the absence of a candidate, not one that could win.
PRECEDENCE: tuple[RecordedDateSource, ...] = (
    RecordedDateSource.MANUAL,
    RecordedDateSource.CONTAINER,
    RecordedDateSource.FILENAME,
    RecordedDateSource.TRANSCRIPT,
    RecordedDateSource.LLM,
)
