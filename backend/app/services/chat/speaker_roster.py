"""Candidate-scoped speaker roster for chat speaker-mention resolution (issue #524).

Builds the permission-scoped roster of accessible speakers that a question's
candidate name strings could plausibly match, one bounded query per candidate,
never the caller's whole speaker history. Matching lives in
:mod:`speaker_name_match`; this module only decides which rows are read.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import and_
from sqlalchemy import case
from sqlalchemy import func
from sqlalchemy import not_
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.models.media import MediaFile
from app.models.speaker import Speaker
from app.services.chat.speaker_name_text import _FUZZY_MAX_LENGTH_DELTA
from app.services.chat.speaker_name_text import _normalize
from app.services.privacy.redact_view import PrivacyView
from app.services.privacy.redact_view import hidden_suggestion_clause
from app.services.privacy.redact_view import machine_identity_clauses
from app.services.privacy.redact_view import visible_display_name_expr
from app.utils.speaker_identity_source import visible_profile_id
from app.utils.speaker_labels import EMBEDDING_SUGGESTION_SOURCES
from app.utils.speaker_labels import UNKNOWN_SPEAKER_LABELS
from app.utils.speaker_labels import canonical_speaker_label_for_row

logger = logging.getLogger(__name__)

#: Upper bound on how many candidate NAME STRINGS one question may attempt to
#: resolve (issue #524, design direction #2: cap the INPUT, not the corpus).
#: A real question names at most a handful of people; one naming forty is
#: pathological input, not evidence the CORPUS-size cap this replaced
#: (`ROSTER_DISTINCT_CAP`, removed) needed to be bigger. Candidates beyond
#: this are silently dropped from the lookup, the same way
#: :data:`MAX_RESOLUTION_ITEMS` already caps the OUTPUT lists below.
MAX_CANDIDATES_PER_QUESTION = 20

#: Row cap for ONE candidate's targeted lookup (:func:`build_candidate_roster`).
#: A single candidate string realistically matches a handful of roster
#: entries once scoped to accessible/in-scope files; a return this large means
#: the candidate itself is too short or generic to bound safely (a single CJK
#: character, a bare "Team"). Resolving from a truncated sample risks
#: reporting a UNIQUE match that a complete read would have shown as
#: ambiguous — the one failure mode this whole ladder exists to prevent — so
#: that candidate declines instead of truncating.
_CANDIDATE_ROW_CAP = 200


class SpeakerResolutionDeclineReason(StrEnum):
    """Why a turn's speaker-mention resolution produced no unique match.

    Issue #524's secondary defect: ``{'declined': True}`` alone carried no
    reason, so distinguishing "the question named nobody", "we could not
    safely search a candidate", and "every candidate that matched anything
    matched more than one entry" cost a source read. Values are a closed,
    short, NON-IDENTIFYING enum — never a speaker name — because this is
    persisted verbatim in ``msg_metadata.speaker_resolution``.
    """

    #: The question carried no name-like candidates at all — nothing to look up.
    NO_CANDIDATES = "no_candidates"
    #: A candidate's own bounded lookup could not be resolved safely (see
    #: `_CANDIDATE_ROW_CAP`) — the search space for that candidate was too
    #: large to trust a truncated read of it. `Roster.declined` is True.
    ROSTER_TOO_LARGE = "roster_too_large"
    #: One or more candidates matched something, but every match was a tie —
    #: ambiguity means no filter, ever, never a guess. (The tied names
    #: themselves are already reported in `SpeakerMentionResolution.ambiguous`;
    #: this is the short, aggregable category alongside that list.)
    AMBIGUOUS_TIE = "ambiguous_tie"
    #: Candidates existed and were searched; none matched the roster.
    NO_MATCH = "no_match"


@dataclass(frozen=True)
class RosterEntry:
    """One person a chat turn could plausibly be asked about."""

    name: str
    profile_id: int | None
    file_count: int


@dataclass(frozen=True)
class Roster:
    """A speaker roster scoped to what one resolution attempt actually needed.

    ``declined`` means a candidate's own bounded lookup could not be resolved
    safely (:data:`_CANDIDATE_ROW_CAP`, see :func:`build_candidate_roster`) —
    ``entries`` is empty in that case, and the caller should resolve no
    mentions at all rather than degrade to a partial or slow match.
    ``decline_reason`` names why, for :class:`SpeakerMentionResolution.as_meta`.
    """

    entries: tuple[RosterEntry, ...] = ()
    declined: bool = False
    decline_reason: SpeakerResolutionDeclineReason | None = None


def _group_speaker_rows(rows: Any, db: Session | PrivacyView) -> dict[str, dict[str, Any]]:
    """Group raw ``Speaker`` rows into canonical-label buckets.

    Canonical labels come from :func:`canonical_speaker_label`, the single
    home for speaker display-name resolution, so a roster built this way
    names people exactly the way the chunk index, the digest plane and every
    other reader do. Rows resolving to :data:`UNKNOWN_SPEAKER_LABELS` are
    excluded — "who said X" about an unlabeled diarization slot is not a
    mention anyone could type. Shared by :func:`build_candidate_roster` so
    every roster this module builds canonicalizes identically.

    A voiceprint-derived name is not a roster name while its file's voiceprints are off
    (#1280): the model would otherwise be handed, and could resolve a "who said" question to,
    a name the speaker API no longer shows. A profile link a voiceprint made is not exposed
    either: the roster entry's ``profile_id`` is the link the file's decision allows.
    ``db`` is the session (or a shared ``PrivacyView``) the policy is read through.
    """
    grouped: dict[str, dict[str, Any]] = {}
    rows = list(rows)
    privacy = db if isinstance(db, PrivacyView) else PrivacyView(db)
    privacy.preload_files(int(r.media_file_id) for r in rows)
    for row in rows:
        label = canonical_speaker_label_for_row(row, privacy)
        if label in UNKNOWN_SPEAKER_LABELS:
            continue
        bucket = grouped.setdefault(label, {"profile_id": None, "files": set()})
        bucket["files"].add(row.media_file_id)
        profile_id = visible_profile_id(
            row.profile_id,
            row.identity_source,
            privacy.voiceprints(int(row.media_file_id)).enabled,
        )
        if profile_id is not None and bucket["profile_id"] is None:
            bucket["profile_id"] = profile_id
    return grouped


def _escape_like(value: str) -> str:
    """Escape LIKE/ILIKE metacharacters so a candidate containing a literal
    ``%``/``_``/``\\`` is matched literally, never as a wildcard."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _candidate_column_clause(column: Any, cand_norm: str) -> Any:
    """One SQL predicate bounding whether *column* could plausibly match the
    already-normalized *cand_norm* under ANY rung of :func:`match_candidate`'s
    ladder.

    A deliberate OVER-approximation of the ladder, not a re-implementation of
    it in SQL — this only decides which rows are worth reading from Postgres.
    :func:`match_candidate` still runs the real ladder in Python against
    whatever rows this fetches, so a SQL false POSITIVE costs one extra row
    scored and rejected — never a wrong match. A SQL false NEGATIVE is what
    this function must avoid:

    - exact / unique token-subset / the no_space and Korean prefix rungs are
      all, after normalization, a CONTAINMENT check in one direction or the
      other ("Dana" inside "Dana Smith", or a short CJK/Korean roster name
      inside a longer scriptio-continua run) — covered by the ``ilike`` and
      ``strpos`` clauses below, one per direction.
    - the fuzzy (short-typo) rung is approximated, not covered exactly: a
      genuine edit-distance search needs either reading every row (what this
      function exists to avoid) or a trigram index this deployment does not
      have. A typo is a small edit and rarely changes the first two
      characters, so "same length class, same leading two characters" is
      used as a bounded stand-in — narrower than the real fuzzy rung (a typo
      IN the first two characters is missed), a documented trade against
      reading the caller's whole roster to catch it.
    """
    if not cand_norm:
        return None
    escaped = _escape_like(cand_norm)
    clauses = [
        column.ilike(f"%{escaped}%", escape="\\"),  # cand_norm found INSIDE column
        func.strpos(cand_norm, func.lower(column)) > 0,  # column found INSIDE cand_norm
    ]
    if len(cand_norm) >= 2:
        prefix = _escape_like(cand_norm[:2])
        clauses.append(
            and_(
                column.ilike(f"{prefix}%", escape="\\"),
                func.abs(func.length(column) - len(cand_norm)) <= _FUZZY_MAX_LENGTH_DELTA,
            )
        )
    return or_(*clauses)


def _candidate_filter(columns: tuple[Any, ...], cand_norm: str) -> Any:
    """OR *cand_norm*'s per-column clauses across every raw name column —
    ``canonical_speaker_label`` picks ONE of them per row, so a row is worth
    reading if ANY of its raw columns plausibly relates to the candidate."""
    clauses = [
        c for c in (_candidate_column_clause(col, cand_norm) for col in columns) if c is not None
    ]
    if not clauses:
        return None
    return or_(*clauses)


def _accessible_speaker_query(db: Session, accessible_sq: Any, file_uuids: list[str] | None) -> Any:
    """The base ``Speaker`` row query: accessible, non-quarantined files only, narrowed to
    *file_uuids* when the turn carries an explicit scope."""
    base_query = db.query(
        Speaker.name,
        Speaker.display_name,
        Speaker.suggested_name,
        Speaker.confidence,
        Speaker.suggestion_source,
        Speaker.verified,
        Speaker.identity_source,
        Speaker.profile_id,
        Speaker.media_file_id,
    ).join(MediaFile, MediaFile.id == Speaker.media_file_id)
    base_query = base_query.filter(
        Speaker.media_file_id.in_(select(accessible_sq)),
        MediaFile.is_quarantined.is_(False),
    )
    if file_uuids is not None:
        base_query = base_query.filter(MediaFile.uuid.in_(file_uuids))
    return base_query


def _candidate_columns(view: PrivacyView, accessible_files: Any) -> tuple[Any, ...]:
    """The raw name columns a candidate may match, with machine-made names gated by policy."""
    # While a file's voiceprints are off, the names a voiceprint made are not candidate
    # columns (#1280): the Python ladder below would drop them through the label anyway, but a
    # hidden row fetched here still counts against the row cap, and a cap that trips only for a
    # hidden name tells the asker that the name exists.
    name_hidden, _link_hidden = machine_identity_clauses(view, accessible_files)
    suggestion_hidden = hidden_suggestion_clause(view, accessible_files)

    # A suggested name is a candidate-bearing column only when a voiceprint match produced it
    # (and the file's policy still lets it be shown): an LLM suggestion is not the speaker's
    # name, so "who said X" must not find a speaker through it (the label it would then
    # resolve to is the placeholder).
    voiceprint_suggested_name = case(
        (
            and_(
                Speaker.suggestion_source.in_(sorted(EMBEDDING_SUGGESTION_SOURCES)),
                not_(suggestion_hidden),
            ),
            Speaker.suggested_name,
        ),
        else_=None,
    )
    return (Speaker.name, visible_display_name_expr(name_hidden), voiceprint_suggested_name)


def _row_key(row: Any) -> tuple[Any, ...]:
    """Identity of one fetched speaker row, so the per-candidate lookups can be deduplicated."""
    return (
        row.name,
        row.display_name,
        row.suggested_name,
        getattr(row, "suggestion_source", None),
        row.identity_source,
        row.profile_id,
        row.media_file_id,
    )


def build_candidate_roster(
    db: Session,
    user_id: int,
    candidates: list[str],
    *,
    organization_id: OrgScope = UNSCOPED,
    file_uuids: list[str] | None = None,
) -> Roster:
    """Build a roster scoped to the CANDIDATE STRINGS actually asked about,
    never the caller's whole speaker history (issue #524).

    Replaces the old ``build_roster``, which read every distinct speaker name
    the caller could access before looking at the question at all, and
    declined outright past ``ROSTER_DISTINCT_CAP`` (500) — even to answer
    about a real, heavily-discussed speaker — because the roster it built
    just to CHECK was too big to check safely. Measured live: an account
    owning 529 distinct speaker names got a clean decline on a question about
    "Marketing", a speaker with 862 chunks in scope. The caller does not need
    to know about every person in their library; it needs to know whether the
    handful of names *this question* mentions exist and are unambiguous.

    For each of up to :data:`MAX_CANDIDATES_PER_QUESTION` candidates, this
    issues one bounded, permission-scoped query (see
    :func:`_candidate_column_clause`) instead of materializing a roster
    upfront. Because the query already fetches every roster row that could
    plausibly match THIS candidate under the ladder, it also carries whatever
    it takes to resolve the AMBIGUITY rule correctly (design direction #4):
    a genuine tie for a candidate is, by construction, inside the fetched
    set for that candidate — there is no separate "build a bigger roster to
    check for ties" step needed.

    A candidate whose own targeted lookup still returns more than
    :data:`_CANDIDATE_ROW_CAP` rows declines the WHOLE call (``declined=True``,
    reason ``ROSTER_TOO_LARGE``) rather than resolving from a truncated,
    non-representative sample — a truncated read could report a false UNIQUE
    match where a complete one would have shown ambiguity, which is the one
    failure mode this ladder exists to prevent.

    ``file_uuids`` narrows the search to files already in scope for this turn
    (design direction #3) — when the caller passed an explicit scope, a
    speaker mention is resolved against THAT scope, never the whole library.
    ``None`` (the "all accessible" turn shape used throughout this package)
    searches every accessible file, exactly as an unscoped retrieval leg
    would.

    Permissions are unchanged from the roster this replaces: accessible files
    only — **never `Speaker.user_id`**, the file OWNER's id (the same class
    of bug #385 fixed for tags) — resolved through
    :meth:`PermissionService.get_accessible_file_ids_subquery`, the single
    sharing authority this package routes every other axis through, with
    quarantined files excluded explicitly (the accessible-files subquery
    alone does not filter them).

    Returns:
        A :class:`Roster`. Empty, non-declined when *candidates* is empty
        after normalization. ``declined=True`` only when a candidate's own
        bounded lookup could not be resolved safely.
    """
    from app.services.permission_service import PermissionService

    norm_candidates: list[str] = []
    seen_candidates: set[str] = set()
    for c in candidates:
        n = _normalize(c)
        if n and n not in seen_candidates:
            seen_candidates.add(n)
            norm_candidates.append(n)
    norm_candidates = norm_candidates[:MAX_CANDIDATES_PER_QUESTION]
    if not norm_candidates:
        return Roster()

    accessible_sq = PermissionService.get_accessible_file_ids_subquery(
        db, user_id, organization_id=organization_id
    )
    base_query = _accessible_speaker_query(db, accessible_sq, file_uuids)
    view = PrivacyView(db)
    columns = _candidate_columns(view, select(accessible_sq))
    all_rows: list[Any] = []
    seen_row_keys: set[tuple[Any, ...]] = set()
    for cand in norm_candidates:
        clause = _candidate_filter(columns, cand)
        if clause is None:
            continue
        rows = base_query.filter(clause).limit(_CANDIDATE_ROW_CAP + 1).all()
        if len(rows) > _CANDIDATE_ROW_CAP:
            logger.info(
                "Speaker candidate roster declined for user %s: a candidate lookup exceeded cap %d",
                user_id,
                _CANDIDATE_ROW_CAP,
            )
            return Roster(
                entries=(),
                declined=True,
                decline_reason=SpeakerResolutionDeclineReason.ROSTER_TOO_LARGE,
            )
        for row in rows:
            key = _row_key(row)
            if key not in seen_row_keys:
                seen_row_keys.add(key)
                all_rows.append(row)

    grouped = _group_speaker_rows(all_rows, view)
    entries = tuple(
        sorted(
            (
                RosterEntry(name=name, profile_id=data["profile_id"], file_count=len(data["files"]))
                for name, data in grouped.items()
            ),
            key=lambda e: (-e.file_count, e.name.lower()),
        )
    )
    return Roster(entries=entries)
