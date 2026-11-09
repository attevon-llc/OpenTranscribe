"""Query/filter/sort helpers for the speaker list endpoint."""

import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.models.speaker import Speaker
from app.models.speaker import SpeakerProfile
from app.models.user import User
from app.services.file_availability import file_is_available
from app.services.permission_service import file_ids_in_scope
from app.services.permission_service import org_scope_pred
from app.services.platform_bypass import PlatformBypass
from app.services.privacy.redact_view import PrivacyView
from app.services.privacy.redact_view import machine_identity_clauses
from app.services.privacy.redact_view import visible_display_name_expr
from app.services.privacy.redact_view import visible_profile_id_expr
from app.services.speaker_list_queries import accessible_file_scope
from app.services.speaker_list_queries import profile_id_for_uuid
from app.services.speaker_list_queries import speaker_picker_groups
from app.utils.speaker_name_rules import PLACEHOLDER_SPEAKER_NAME_PATTERN

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)

# --- Helper functions for list_speakers ---


def _sort_speakers(speakers: list[Speaker]) -> list[Speaker]:
    """Sort speakers by SPEAKER_XX numbering for consistent ordering."""

    def get_speaker_number(speaker: Speaker) -> int:
        # :data:`PLACEHOLDER_SPEAKER_NAME_PATTERN`, with the ordinal captured — this one
        # reads the number out rather than answering the yes/no question, so it cannot
        # reuse the constant directly. Keep the two in step.
        match = re.match(r"^SPEAKER_(\d+)$", str(speaker.name))
        return int(match.group(1)) if match else 999

    # Always sort by original speaker number first, regardless of verification status
    # This ensures SPEAKER_01, SPEAKER_02, SPEAKER_03... order is maintained
    speakers.sort(key=lambda s: get_speaker_number(s))
    return speakers


#: Default page size for the ``for_filter=True`` fast path, and its ceiling.
#: A picker doing type-to-search never needs more than a screenful before the
#: user narrows further with ``q`` — matches the roster decline threshold in
#: ``services/chat/speaker_resolver.py`` (a picker showing 500 unfiltered
#: names is already past the point of being useful).
SPEAKER_FILTER_DEFAULT_LIMIT = 200
SPEAKER_FILTER_MAX_LIMIT = 500


def _resolve_profile_filter_id(
    db: Session,
    current_user: User,
    profile_uuid: str | None,
    organization_id: OrgScope = UNSCOPED,
    *,
    bypass: PlatformBypass = PlatformBypass.none(),
) -> int | None:
    """Resolve a profile UUID query param to an internal id, or ``None``.

    Callers that cannot see every user's rows are scoped to their own profiles — a profile UUID does not
    belong to the sharing model the speaker/file axes use, so there is no
    "shared profile" case to admit here. An unresolvable UUID (not found, or
    belongs to someone else) returns ``None``, which the caller treats as "no
    such profile" and answers an empty list rather than a 404 — this is a
    filter parameter on a picker, not a resource fetch.
    """
    if not profile_uuid:
        return None
    tenant_pred = org_scope_pred(SpeakerProfile.organization_id, organization_id)
    return profile_id_for_uuid(
        db,
        profile_uuid,
        owner_user_id=None if bypass.sees_all_in_scope else current_user.id,
        tenant_pred=tenant_pred,
    )


def _get_unique_speakers_for_filter(
    db: Session,
    current_user: User,
    organization_id: OrgScope = UNSCOPED,
    *,
    q: str | None = None,
    limit: int = SPEAKER_FILTER_DEFAULT_LIMIT,
    profile_id: int | None = None,
    is_profile: bool | None = None,
    include_unnamed: bool = False,
    bypass: PlatformBypass = PlatformBypass.none(),
) -> list[dict[str, Any]]:
    """
    Get unique speakers by display name for filter use with media file counts.

    Includes speakers from both owned and shared files so that shared-collection
    speakers appear in the filter sidebar. ``organization_id`` tenant-gates the
    accessible-file resolution (default-deny across scopes).

    ⚠️ **Quarantined files are excluded explicitly.** The accessible-files
    subquery alone does not filter quarantine — it answers "can this user
    reach this file", and quarantine is a separate, independent gate every
    other read surface (gallery, detail, search, streaming) already applies.
    Before this fix, a quarantined shared file's speakers — and their file
    counts — surfaced in this picker for every user the file had been shared
    with, including after the takedown, because nothing here ever asked.

    Groups by display name in ONE query (``GROUP BY``, not a separate
    ``DISTINCT ON`` pass + a second count query): the media-file count is what
    ``limit``/ordering are computed against, so counting first and then
    picking a representative row for the winners is both cheaper and — unlike
    the old two-query shape, which capped the alphabetically-first N rows
    before sorting by count — actually returns the top-N by relevance.

    Args:
        q: Case-insensitive substring filter on display name (server-side
            type-to-search; the picker no longer filters a fully-fetched list
            client-side).
        limit: Page size, clamped to :data:`SPEAKER_FILTER_MAX_LIMIT`.
        profile_id: Restrict to speakers linked to this profile (internal id,
            already resolved by the caller).
        is_profile: ``True`` = only profile-linked speakers, ``False`` = only
            unlinked, ``None`` = no filter.
        include_unnamed: Also offer speakers **no human has named** — see the
            note below. Off by default, so every existing caller (the chat
            file-scope picker included) keeps a people-only roster.

    Returns:
        List of dicts with uuid, name, display_name, media_count, is_unnamed,
        and profile_id (the profile's UUID, or ``None``).

    Unnamed speakers (issue #743)
    ---------------------------
    Diarization writes ``display_name=None`` and ``name='SPEAKER_00'``, so on a
    library where nobody has renamed anyone this roster was **empty** and the
    gallery's speaker filter had nothing to show. Dropping the "must have a
    real display name" predicate is not the fix: this function groups across
    files, and ``SPEAKER_00`` in one recording is a different person from
    ``SPEAKER_00`` in another, so an unqualified entry would read as one person
    and return a nonsense file set.

    So they are opt-in and **flagged**. ``include_unnamed=True`` groups by the
    label the caller would actually filter on — ``COALESCE(NULLIF(display_name,
    ''), name)``, exactly what ``files/filtering.apply_speaker_filter`` matches
    — and marks any group whose every row is unlabeled with ``is_unnamed``.
    A caller must never render a flagged entry as a person; the filter sidebar
    collapses them into one "files with unlabeled speakers" facet, which is a
    true statement about a *file* rather than a false one about a person.
    Named entries always sort ahead of unlabeled ones.
    """
    from sqlalchemy import and_
    from sqlalchemy import func
    from sqlalchemy import not_

    # The files this caller may list speakers of: a caller who sees all in scope gets the
    # tenant's speakers, others see accessible files only.
    if bypass.sees_all_in_scope:
        scope_files = file_ids_in_scope(organization_id)
    else:
        scope_files = accessible_file_scope(db, current_user.id, organization_id)

    # A name or link a voiceprint made is not offered while that file's voiceprints are off
    # (issue #1280): the row is listed under its diarizer label like any unnamed speaker, and
    # reads as unlinked. A person's own name and link are never touched.
    name_hidden, link_hidden = machine_identity_clauses(PrivacyView(db), scope_files)
    visible_name = visible_display_name_expr(name_hidden)
    visible_profile = visible_profile_id_expr(link_hidden)

    # "A human gave this speaker a real name." A raw ``SPEAKER_nn`` written into
    # display_name (what an auto-label pass emits) is not one.
    has_human_name = and_(
        visible_name.isnot(None),
        visible_name != "",
        not_(visible_name.op("~")(PLACEHOLDER_SPEAKER_NAME_PATTERN)),
    )
    # The exact string this row is offered under, and sent back as ``?speaker=``.
    label_col = func.coalesce(func.nullif(visible_name, ""), Speaker.name)

    base_filter = [] if include_unnamed else [has_human_name]
    base_filter.append(Speaker.media_file_id.in_(scope_files))

    base_filter.append(file_is_available(Speaker.media_file_id))

    if q and q.strip():
        # Search the offered label, not display_name alone — otherwise typing
        # "SPEAKER_01" into the picker matches nothing it is showing.
        base_filter.append(label_col.ilike(f"%{q.strip()}%"))
    if profile_id is not None:
        base_filter.append(visible_profile == profile_id)
    if is_profile is True:
        base_filter.append(visible_profile.isnot(None))
    elif is_profile is False:
        base_filter.append(visible_profile.is_(None))

    bounded_limit = max(1, min(limit, SPEAKER_FILTER_MAX_LIMIT))
    # A group counts as unlabeled only when EVERY row in it is — a label shared
    # with a real name is a person's entry, not a review bucket.
    unnamed_col = func.bool_and(not_(has_human_name))
    groups = speaker_picker_groups(
        db,
        base_filter,
        label=label_col,
        is_unnamed=unnamed_col,
        visible_profile=visible_profile,
        limit=bounded_limit,
    )

    # An unlabeled group is projected with display_name=None so that the
    # caller's `display_name || name` fallback yields the grouping label
    # even when the label came from a display_name holding "SPEAKER_02".
    return [
        {
            "uuid": group.representative_uuid,
            "name": group.label if group.is_unnamed else group.representative_name,
            "display_name": None if group.is_unnamed else group.label,
            "media_count": group.media_count,
            "is_unnamed": group.is_unnamed,
            "profile_id": group.profile_uuid,
        }
        for group in groups
    ]


def _resolve_file_uuid_to_id(
    file_uuid: str | None,
    current_user: User,
    db: Session,
    organization_id: OrgScope = UNSCOPED,
    *,
    bypass: PlatformBypass = PlatformBypass.none(),
) -> int | None:
    """Convert file UUID to internal ID if provided (tenant-gated lookup)."""
    if not file_uuid:
        return None
    from app.services.uuid_helpers import get_file_by_uuid_with_permission

    media_file = get_file_by_uuid_with_permission(
        db,
        file_uuid,
        current_user.id,
        bypass=bypass,
        organization_id=organization_id,
    )
    return media_file.id
