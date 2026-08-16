"""The three deterministic sources for "when was this recorded". No LLM, no model.

Container metadata, the filename, and the words themselves. All three are pure functions
over data the caller already has, so they are testable without a database and cost nothing
on an ``LLM_PROVIDER``-empty deployment (#403 **D6**). The fourth source — LLM extraction
from the digest — is deliberately absent; :class:`~app.core.enums.RecordedDateSource.LLM`
exists so adding it later needs no schema change.

**Every extractor declines rather than guesses, and the bar is high on purpose.** A wrong
date is worse than no date here: it makes the product answer "3 meetings in March" with
confidence when the truth is 5, and gives the user nothing to check. So an ambiguous
``03/04/2024`` returns ``None`` rather than picking a locale, and a transcript saying "the
fifteenth" with no month or year returns ``None`` rather than inventing the rest from the
upload date. Both refusals are tested, because a refusal is the behaviour, not a gap.
"""

from __future__ import annotations

import calendar
import datetime as dt
import re

from app.core.enums import RecordedDateSource

from .recorded_date import DateCandidate

#: Dates outside this band are rejected wholesale. Both ends catch real junk rather than
#: hypothetical junk: ``1970-01-01`` is the epoch default a great many encoders write into
#: an unset field, and a *future* date is either a mis-parsed number (a filename's
#: ``2039`` is a resolution or a bitrate far more often than a year) or a wrong clock.
_EARLIEST_YEAR = 1970
#: Years ahead of the reference date to still accept. One, not zero: a recording made in a
#: timezone ahead of UTC on New Year's Eve is legitimately "next year" by our clock.
_FUTURE_YEAR_SLACK = 1

_MONTHS = {name.lower(): number for number, name in enumerate(calendar.month_name) if name} | {
    name.lower(): number for number, name in enumerate(calendar.month_abbr) if name
}
#: ``sept`` is the one common abbreviation ``calendar.month_abbr`` does not carry.
_MONTHS["sept"] = 9

_MONTH_ALTERNATION = "|".join(sorted(_MONTHS, key=len, reverse=True))

#: Confidences are **ordinal, not probabilities anybody measured.** They order forms
#: *within* one source and are shown to the user as a hint; they never override
#: :data:`~app.core.enums.PRECEDENCE`, which is what decides between sources. Saying so
#: here because a bare 0.75 in a codebase invites being read as a calibrated number.
_CONF_CONTAINER = 0.9
_CONF_FILENAME_ISO = 0.85
_CONF_FILENAME_MONTH_NAME = 0.75
_CONF_FILENAME_NUMERIC = 0.6
_CONF_TRANSCRIPT_SPOKEN = 0.7

#: How far into a transcript a stated date is still plausibly *this meeting's* date.
#: Meetings announce themselves in the opening exchange; a date said in minute forty is a
#: deadline, a birthday or a historical reference far more often than the date of the
#: recording. Bounded by turns rather than by seconds so it behaves the same on a
#: five-minute standup and a three-hour board meeting.
OPENING_SEGMENTS = 12

#: The deictic anchors that make a spoken date refer to *now*. Without one of these,
#: "March the fifteenth" is being talked about, not being dated — "the deadline is March
#: the fifteenth" is the failure case, and it is the common one.
_ANCHOR = re.compile(
    r"\b("
    r"today\s+is|it\s+is|it's|its|todays?\s+date|the\s+date\s+is|"
    r"this\s+is\s+the|we\s+are\s+on|recorded\s+on|meeting\s+of|meeting\s+on|"
    r"session\s+of|session\s+on|call\s+of|call\s+on"
    r")\b",
    re.IGNORECASE,
)

_ORDINAL = r"(?:st|nd|rd|th)?"

#: ``2024-03-15`` / ``2024_03_15`` / ``2024.03.15``. Year first, so the roles of the other
#: two components are fixed by position and nothing has to be assumed.
_RE_ISO = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_.](\d{1,2})[-_.](\d{1,2})(?!\d)")
#: ``20240315``. Same reasoning; the length is what disambiguates it from a bare year.
_RE_COMPACT = re.compile(r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})(?!\d)")
#: ``Mar 15 2024`` / ``March 15, 2024`` / ``March-15-2024``.
_RE_MONTH_FIRST = re.compile(
    rf"\b({_MONTH_ALTERNATION})\b[\s._-]*(\d{{1,2}}){_ORDINAL}[\s,._-]+((?:19|20)\d{{2}})\b",
    re.IGNORECASE,
)
#: ``15 March 2024`` / ``15th of March, 2024``.
_RE_DAY_FIRST = re.compile(
    rf"\b(\d{{1,2}}){_ORDINAL}[\s._-]*(?:of[\s._-]+)?({_MONTH_ALTERNATION})\b"
    rf"[\s,._-]+((?:19|20)\d{{2}})\b",
    re.IGNORECASE,
)
#: ``15/03/2024`` or ``03/15/2024`` — year LAST, so the first two components are
#: **ambiguous** and are only resolved when one of them exceeds 12.
_RE_NUMERIC_YEAR_LAST = re.compile(r"(?<!\d)(\d{1,2})[-_./](\d{1,2})[-_./]((?:19|20)\d{2})(?!\d)")


def _plausible(year: int, month: int, day: int, today: dt.date) -> dt.datetime | None:
    """A real calendar date inside the accepted band, or ``None``.

    ``dt.date`` does the calendar validation for free, which matters more than it looks:
    ``2023-02-30`` and ``2024-13-05`` both match the regexes above and neither is a date.
    """
    if not (_EARLIEST_YEAR <= year <= today.year + _FUTURE_YEAR_SLACK):
        return None
    try:
        parsed = dt.date(year, month, day)
    except ValueError:
        return None
    if parsed > today + dt.timedelta(days=365 * _FUTURE_YEAR_SLACK):
        return None
    return dt.datetime(parsed.year, parsed.month, parsed.day, tzinfo=dt.UTC)


def _today(reference: dt.date | None) -> dt.date:
    """The plausibility clock, injectable so the tests are not time bombs."""
    return reference or dt.datetime.now(dt.UTC).date()


def from_container(
    creation_date: dt.datetime | None,
    *,
    reference: dt.date | None = None,
) -> DateCandidate | None:
    """The container's own claim — ffprobe/exiftool ``creation_time``.

    Takes the already-parsed ``media_file.creation_date`` rather than re-reading the raw
    metadata, because ingest has parsed it once already and a second parser is a second
    thing to keep in agreement.

    ⚠️ This is only trustworthy because ``creation_date`` no longer falls back to
    filesystem mtime and then to ``upload_time``. While it did, this function would have
    been laundering the upload date into a ``container`` provenance — asserting the file
    said something it never said.
    """
    if creation_date is None:
        return None
    stamped = creation_date if creation_date.tzinfo else creation_date.replace(tzinfo=dt.UTC)
    resolved = _plausible(stamped.year, stamped.month, stamped.day, _today(reference))
    if resolved is None:
        return None
    return DateCandidate(
        source=RecordedDateSource.CONTAINER,
        date=stamped,
        confidence=_CONF_CONTAINER,
        evidence=f"container metadata: {stamped.isoformat()}",
    )


def from_filename(
    filename: str | None,
    *,
    reference: dt.date | None = None,
) -> DateCandidate | None:
    """A date encoded in the filename — how most archives actually carry one.

    Tried most-specific first. The year-first forms are unambiguous by position; the
    month-name forms are unambiguous by construction; ``15/03/2024`` is resolved **only**
    when one component exceeds 12 and therefore cannot be a month. A filename whose date
    is genuinely ``03/04/2024`` is refused, because choosing between 3 April and 4 March
    means choosing a locale this application was never told.
    """
    if not filename:
        return None
    today = _today(reference)
    stem = filename.rsplit("/", 1)[-1]

    for pattern, confidence in ((_RE_ISO, _CONF_FILENAME_ISO), (_RE_COMPACT, _CONF_FILENAME_ISO)):
        match = pattern.search(stem)
        if match:
            # Unpacked rather than splatted: `_plausible(*(...), today)` is valid
            # ONLY while every pattern here has exactly three groups, and nothing
            # enforced that. A four-group pattern added later would silently shift
            # `today` into the `day` position and mis-date every file it matched.
            year, month, day = match.groups()
            resolved = _plausible(int(year), int(month), int(day), today)
            if resolved is not None:
                return _filename_candidate(resolved, confidence, match.group(0))

    match = _RE_MONTH_FIRST.search(stem)
    if match:
        resolved = _plausible(
            int(match.group(3)), _MONTHS[match.group(1).lower()], int(match.group(2)), today
        )
        if resolved is not None:
            return _filename_candidate(resolved, _CONF_FILENAME_MONTH_NAME, match.group(0))

    match = _RE_DAY_FIRST.search(stem)
    if match:
        resolved = _plausible(
            int(match.group(3)), _MONTHS[match.group(2).lower()], int(match.group(1)), today
        )
        if resolved is not None:
            return _filename_candidate(resolved, _CONF_FILENAME_MONTH_NAME, match.group(0))

    match = _RE_NUMERIC_YEAR_LAST.search(stem)
    if match:
        first, second, year = (int(g) for g in match.groups())
        # Exactly one ordering is possible only when one component cannot be a month.
        # Both <= 12 is the ambiguous case and is refused, not guessed.
        if first > 12 and second <= 12:
            resolved = _plausible(year, second, first, today)
        elif second > 12 and first <= 12:
            resolved = _plausible(year, first, second, today)
        else:
            return None
        if resolved is not None:
            return _filename_candidate(resolved, _CONF_FILENAME_NUMERIC, match.group(0))
    return None


def _filename_candidate(resolved: dt.datetime, confidence: float, matched: str) -> DateCandidate:
    return DateCandidate(
        source=RecordedDateSource.FILENAME,
        date=resolved,
        confidence=confidence,
        evidence=f"filename: {matched!r}",
    )


def from_transcript(
    segments: list[dict],
    *,
    reference: dt.date | None = None,
    opening_segments: int = OPENING_SEGMENTS,
) -> DateCandidate | None:
    """The meeting stating its own date out loud — **the source unique to this product.**

    Two conditions, both required, and both there to keep a mention from becoming a claim:

    1. It is said in the **opening** ``opening_segments`` turns. A date in minute forty is
       overwhelmingly a deadline or a reference, not the date of the recording.
    2. A deictic anchor precedes it in the same turn — "today is", "this is the",
       "recorded on". Without one, "the deadline is March the fifteenth" reads exactly
       like a meeting dating itself, and it is the more common sentence of the two.

    A date must also be **complete**. "It's Tuesday the fifteenth" is anchored, in the
    opening, and still returns ``None``: recovering the month and year would mean assuming
    the recording happened near its upload, which is the assumption this whole change
    exists to stop making.

    Args:
        segments: Ordered segment dicts (``load_ordered_segments`` output shape); only
            ``text`` is read.
        reference: Plausibility clock; defaults to today.
        opening_segments: How many leading turns count as the opening.

    Returns:
        The first complete, anchored date in the opening, or ``None``.
    """
    today = _today(reference)
    for segment in segments[:opening_segments]:
        text = str(segment.get("text") or "")
        if not text or not _ANCHOR.search(text):
            continue
        for pattern, month_group, day_group, year_group in (
            (_RE_MONTH_FIRST, 1, 2, 3),
            (_RE_DAY_FIRST, 2, 1, 3),
            (_RE_ISO, None, None, None),
        ):
            match = pattern.search(text)
            if not match:
                continue
            if month_group is None:
                # ISO: three groups in year/month/day order. Unpacked rather than
                # splatted for the same reason as the filename path above.
                year, month, day = match.groups()
                resolved = _plausible(int(year), int(month), int(day), today)
            else:
                # The table's three indices are all-None (ISO, handled above) or
                # all-int; asserting it makes the invariant checkable rather than
                # implicit, and a partially-None row would otherwise crash here
                # with a `group(None)` TypeError at parse time.
                assert day_group is not None and year_group is not None, (
                    "pattern table row has a month group but not day/year"
                )
                resolved = _plausible(
                    int(match.group(year_group)),
                    _MONTHS[match.group(month_group).lower()],
                    int(match.group(day_group)),
                    today,
                )
            if resolved is not None:
                return DateCandidate(
                    source=RecordedDateSource.TRANSCRIPT,
                    date=resolved,
                    confidence=_CONF_TRANSCRIPT_SPOKEN,
                    evidence=f"spoken in the opening: {match.group(0)!r}",
                )
    return None
