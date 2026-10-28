"""Read-only provenance of a speaker's identity and recommendation, for the API (issue #1280).

``Speaker.identity_source_at`` / ``_user_id`` / ``_detail`` and ``suggestion_detail`` record how
the CURRENT name, link and recommendation came to be. This module turns them into the
``identity`` and ``suggestion`` objects of the speaker schema, after the privacy redaction:

* While a file's voiceprints are off, the record of a MACHINE identity names the profile or
  speaker the voice matched, so it is withheld exactly like the name and link it describes
  (``identity`` is ``null``). A person's own record is never withheld.
* A voiceprint suggestion's block (``suggestion``) is withheld on the same terms, whoever
  verified the speaker: it is the evidence of a voice comparison. The check is made here too, so
  a caller that forgot the redaction step still cannot serve it.
* ``identity.by`` (the person who made or confirmed it) is shown only to the file's owner, to an
  administrator of the file's organization and to a platform administrator whose reach covers
  this file: any administrator in single-tenant mode, and in multi-tenant mode only under a
  support-access grant for the file's tenant (the use is recorded like any other platform
  access). Any other viewer of a shared file gets ``source`` and ``at``. When the person's
  account is being erased (an erasure request names it; the row can outlive it while a legal
  hold lasts), ``by`` is an ``erased`` marker with no uuid, name or email.

Nothing here writes. The rows get plain (non-mapped) attributes, which the schema reads, so no
change reaches the database.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any

from app.models.erasure import ErasureLedgerEntry
from app.models.media import MediaFile
from app.models.user import User
from app.services.platform_bypass import PlatformBypass
from app.services.privacy.redact_view import PrivacyView
from app.services.privacy.redact_view import hides_voiceprint_suggestion
from app.utils.speaker_identity_source import hides_provenance

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.api.deps_context import RequestContext
    from app.models.media import Speaker

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProvenanceViewer:
    """Who is looking, as far as ``identity.by`` is concerned.

    Attributes:
        user_id: The viewer's ``user.id``.
        admin_org_id: The organization the viewer administers on this request (``None`` when
            they administer none): an org admin sees ``by`` for that organization's files.
        bypass: The request's platform bypass (a platform administrator's reach).
    """

    user_id: int
    admin_org_id: int | None = None
    bypass: PlatformBypass = field(default_factory=PlatformBypass.none)

    @classmethod
    def from_context(cls, ctx: RequestContext) -> ProvenanceViewer:
        """The viewer of a request."""
        return cls(
            user_id=int(ctx.user.id),
            admin_org_id=ctx.org_id if ctx.is_org_admin else None,
            bypass=ctx.bypass,
        )


def _can_see_actor(viewer: ProvenanceViewer | None, file_row: tuple[Any, Any, Any]) -> bool:
    """May this viewer learn WHO made a speaker's identity in this file?

    Args:
        viewer: The viewer (``None`` = nobody).
        file_row: ``(owner_user_id, organization_id, file uuid)``.
    """
    if viewer is None:
        return False
    owner_id, org_id, file_uuid = file_row
    if owner_id is not None and int(owner_id) == viewer.user_id:
        return True
    if org_id is not None and viewer.admin_org_id == org_id:
        return True
    # A platform administrator is named the person only where the platform role reaches this
    # file. ``allows`` answers that against the file's own tenant and, in multi-tenant mode,
    # records the use under the support grant; reaching the file through an ordinary share
    # confers nothing.
    return viewer.bypass.allows(
        org_id=org_id,
        owner_id=owner_id,
        need="read",
        resource_type="media_file",
        resource_uuid=str(file_uuid),
    )


def _erased_user_ids(session: Session, users: dict[int, dict[str, Any]]) -> set[int]:
    """Which of these accounts an erasure request names (matched on id AND uuid)."""
    if not users:
        return set()
    rows = (
        session.query(ErasureLedgerEntry.subject_user_id, ErasureLedgerEntry.subject_user_uuid)
        .filter(
            ErasureLedgerEntry.subject_type == "user",
            ErasureLedgerEntry.subject_user_id.in_(users),
        )
        .all()
    )
    return {
        int(user_id)
        for user_id, user_uuid in rows
        if user_uuid is not None and str(user_uuid) == str(users[int(user_id)]["uuid"])
    }


def attach_speaker_provenance(
    db: Session,
    speakers: Iterable[Speaker],
    viewer: ProvenanceViewer | RequestContext | None,
    *,
    view: PrivacyView | None = None,
) -> None:
    """Set ``identity`` and ``suggestion`` on each row for the schema to serialize.

    Call it after ``redact_speaker_rows`` and before the rows are validated into response
    schemas. Fails closed: a file whose privacy scope cannot be read hides a machine identity.

    Args:
        db: The session the rows were loaded with.
        speakers: Loaded speaker rows (already redacted).
        viewer: The viewer (or the request context to derive it from); decides whether
            ``identity.by`` is shown.
        view: A :class:`PrivacyView` to share its memo with the redaction (one is made if
            omitted).
    """
    rows = list(speakers)
    if not rows:
        return
    if viewer is not None and not isinstance(viewer, ProvenanceViewer):
        viewer = ProvenanceViewer.from_context(viewer)
    view = view or PrivacyView(db)
    session = db
    file_ids = {int(s.media_file_id) for s in rows}
    view.preload_files(file_ids)
    files = {
        int(r[0]): (r[1], r[2], r[3])
        for r in session.query(
            MediaFile.id, MediaFile.user_id, MediaFile.organization_id, MediaFile.uuid
        )
        .filter(MediaFile.id.in_(file_ids))
        .all()
    }
    # Decided lazily, only for a file with a recorded person: asking the platform role
    # whether it reaches a file is itself an audited act.
    show_actor: dict[int, bool] = {}
    wanted_actors: set[int] = set()
    for speaker in rows:
        fid = int(speaker.media_file_id)
        if speaker.identity_source_user_id is None:
            continue
        if fid not in show_actor:
            show_actor[fid] = fid in files and _can_see_actor(viewer, files[fid])
        if show_actor[fid]:
            wanted_actors.add(int(speaker.identity_source_user_id))
    actors: dict[int, dict[str, Any]] = {}
    if wanted_actors:
        actors = {
            int(u.id): {"uuid": u.uuid, "name": u.full_name or u.email}
            for u in session.query(User.id, User.uuid, User.full_name, User.email)
            .filter(User.id.in_(wanted_actors))
            .all()
        }
        for erased in _erased_user_ids(session, actors):
            actors[erased] = {"uuid": None, "name": None, "erased": True}
    for speaker in rows:
        fid = int(speaker.media_file_id)
        enabled = view.voiceprints(fid).enabled
        identity = None
        source = speaker.identity_source
        if source and not hides_provenance(source, enabled):
            by = None
            if show_actor.get(fid) and speaker.identity_source_user_id is not None:
                by = actors.get(int(speaker.identity_source_user_id))
            identity = {
                "source": source,
                "at": speaker.identity_source_at,
                "by": by,
                "detail": speaker.identity_source_detail,
            }
        suggestion = None
        if speaker.suggested_name and not (hides_voiceprint_suggestion(speaker) and not enabled):
            suggestion = {
                "source": speaker.suggestion_source,
                "confidence": speaker.confidence,
                "detail": speaker.suggestion_detail,
            }
        speaker.identity = identity  # plain attributes: read by the schema, never flushed
        speaker.suggestion = suggestion


def provenance_for_dict(speaker: Speaker) -> dict[str, Any]:
    """``identity`` and ``suggestion`` of an attached row, as JSON-safe values.

    For the surfaces that hand-build a dict instead of validating the ORM row into the schema.
    A row :func:`attach_speaker_provenance` was not run on reads as unknown (``None``).
    """
    identity = getattr(speaker, "identity", None)
    if identity is not None:
        by = identity.get("by")
        identity = {
            **identity,
            "at": identity["at"].isoformat() if identity.get("at") else None,
            "by": (
                {
                    "uuid": str(by["uuid"]) if by["uuid"] is not None else None,
                    "name": by["name"],
                    "erased": bool(by.get("erased")),
                }
                if by
                else None
            ),
        }
    return {"identity": identity, "suggestion": getattr(speaker, "suggestion", None)}
