"""Destroy every external copy an ACCOUNT owns when the account is deleted (issue #695).

The account cascade deletes ``media_file`` rows in bulk, which never reaches
``file_cleanup_service.purge_media_file``. :func:`load_account_purge_plans` reads the
object-storage and OpenSearch addresses as plain data BEFORE that delete;
:func:`purge_account_external_copies` destroys them AFTER the account's rows commit, with
no transaction open, reusing the per-file external destroy and the OpenSearch erasers.
"""

import logging
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from sqlalchemy.orm import Session

from app.models.media import MediaFile
from app.services.file_cleanup_service import _purge_external_copies
from app.services.opensearch_erasure import _erase_cluster_docs
from app.services.opensearch_erasure import _erase_profile_docs
from app.services.opensearch_erasure import _erase_speaker_docs

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccountPurgePlan:
    """Plain-data plan for destroying every external copy an ACCOUNT owns (issue #695).

    Deleting a user removes ``media_file`` rows via a bulk ``query(...).delete()``
    (``admin._delete_user_media_files``) that never loads instances and therefore never
    reaches ``purge_media_file`` — so the objects those rows named survived every
    account deletion forever. This is the account-scoped analogue of
    :func:`_load_purge_plan`: read everything the external destroy needs *before* the
    bulk delete runs, as plain data, then hand it to :func:`purge_account_external_copies`
    with no transaction open.

    Attributes:
        files: One ``_load_purge_plan``-shaped dict per media file the account owns.
        avatar_paths: Every non-null ``SpeakerProfile.avatar_path`` the account owns.
            ``purge_media_file`` deliberately never touches these — profiles are
            preserved when a single FILE is destroyed — but the account-delete path
            destroys the profiles themselves (``_delete_user_owned_records``), and
            nothing else has ever removed the avatar objects backing them.
        speaker_uuids: Every ``Speaker.uuid`` this account owns (issue #715). Read
            here, alongside ``avatar_paths``, so :func:`purge_account_external_copies`
            can remove the OpenSearch voiceprints **after** the account's rows commit,
            instead of ``admin._delete_user_speakers`` doing it — one OpenSearch round
            trip per speaker — from inside the caller's open transaction.
        profile_uuids: Every ``SpeakerProfile.uuid`` this account owns (issue #715),
            for the same reason: ``admin._delete_user_owned_records`` used to remove
            the profile embeddings itself, also from inside the open transaction.
        cluster_uuids: Every ``SpeakerCluster.uuid`` this account owns. The rows go by
            ``ON DELETE CASCADE`` off ``user``, which no code sees, so the clusters'
            centroid embeddings (``cluster_<uuid>`` documents in the speaker index — an
            average of the account's voiceprints, i.e. biometric) outlived every
            deleted account until they were read here.
        user_id: The account, so its Redis caches (file listings name every file)
            can be invalidated once the rows are gone.
    """

    files: list[dict[str, Any]] = field(default_factory=list)
    avatar_paths: list[str] = field(default_factory=list)
    speaker_uuids: list[str] = field(default_factory=list)
    profile_uuids: list[str] = field(default_factory=list)
    cluster_uuids: list[str] = field(default_factory=list)
    user_id: int | None = None


def load_account_purge_plans(db: Session, user_id: int) -> AccountPurgePlan:
    """Read the storage/OpenSearch addresses of everything an account owns.

    Two COLUMN-ONLY queries — **plain tuples, never ORM instances** — for the same
    reason :func:`_load_purge_plan` reads plain data: an escaping instance lazy-loads,
    which reopens a transaction in the middle of the caller's external-copy phase.

    ``speaker_uuids`` is left ``[]`` on every FILE plan here — not because a file's
    speakers should be skipped, but because they are not per-file work at all.
    :class:`AccountPurgePlan` carries the account's speaker and profile UUIDs at the
    top level instead (issue #715): both used to be swept by
    ``admin._delete_user_speakers`` / ``admin._delete_user_owned_records`` themselves,
    one OpenSearch round trip per speaker/profile, from inside the caller's open
    transaction. They are read here as plain column-only tuples, and
    :func:`purge_account_external_copies` removes the embeddings after the account's
    rows have committed, matching the phase split every other external-copy delete in
    this module already uses.

    Args:
        db: The caller's session, used only for the reads below.
        user_id: Internal id of the account about to be deleted.

    Returns:
        An :class:`AccountPurgePlan` naming every object this account's cascade must
        remove from object storage and OpenSearch.
    """
    from app.models.speaker import Speaker
    from app.models.speaker import SpeakerProfile

    file_rows = (
        db.query(
            MediaFile.id,
            MediaFile.uuid,
            MediaFile.filename,
            MediaFile.storage_path,
            MediaFile.thumbnail_path,
            MediaFile.playback_path,
        )
        .filter(MediaFile.user_id == user_id)
        .all()
    )
    files: list[dict[str, Any]] = [
        {
            "file_id": int(row.id),
            "file_uuid": str(row.uuid),
            "owner_id": int(user_id),
            "filename": str(row.filename) if row.filename else None,
            "storage_path": str(row.storage_path) if row.storage_path else None,
            "thumbnail_path": str(row.thumbnail_path) if row.thumbnail_path else None,
            "playback_path": str(row.playback_path) if row.playback_path else None,
            "speaker_uuids": [],
            "speaker_read_error": None,
        }
        for row in file_rows
    ]

    avatar_rows = (
        db.query(SpeakerProfile.avatar_path)
        .filter(SpeakerProfile.user_id == user_id, SpeakerProfile.avatar_path.isnot(None))
        .all()
    )
    avatar_paths = [str(row[0]) for row in avatar_rows if row[0]]

    speaker_rows = db.query(Speaker.uuid).filter(Speaker.user_id == user_id).all()
    speaker_uuids = [str(row[0]) for row in speaker_rows]

    profile_uuid_rows = (
        db.query(SpeakerProfile.uuid).filter(SpeakerProfile.user_id == user_id).all()
    )
    profile_uuids = [str(row[0]) for row in profile_uuid_rows]

    from app.models.speaker import SpeakerCluster

    cluster_rows = db.query(SpeakerCluster.uuid).filter(SpeakerCluster.user_id == user_id).all()
    cluster_uuids = [str(row[0]) for row in cluster_rows]

    return AccountPurgePlan(
        files=files,
        avatar_paths=avatar_paths,
        speaker_uuids=speaker_uuids,
        profile_uuids=profile_uuids,
        cluster_uuids=cluster_uuids,
        user_id=int(user_id),
    )


def purge_account_external_copies(plan: AccountPurgePlan) -> list[dict[str, Any]]:
    """Destroy every external copy an ACCOUNT owned. **Takes no Session, never raises.**

    Runs after the account's rows are already committed (see
    ``admin.delete_admin_user`` / ``users.delete_user``): the rows are gone, so a
    failure here is not retryable and must be visible, never swallowed. Reuses
    :func:`_purge_external_copies` per file rather than open-coding a second artifact
    list, which is what gives this call thumbnail + derived-cache (including listed
    masked variants) + OpenSearch erasure for free, in one audited implementation.
    Also sweeps ``plan.speaker_uuids`` / ``plan.profile_uuids`` (issue #715) — this is
    now the only place either embedding is removed; neither
    ``admin._delete_user_speakers`` nor ``admin._delete_user_owned_records`` touches
    OpenSearch any more.

    Args:
        plan: The plain-data plan from :func:`load_account_purge_plans`.

    Returns:
        One ``{"stage", "file_uuid"|"avatar_path", "error"}`` dict per object that may
        still exist. Empty means every object was confirmed gone.
    """
    residual: list[dict[str, Any]] = []

    for file_plan in plan.files:
        try:
            residual.extend(_purge_external_copies(file_plan))
        except Exception as e:  # noqa: BLE001 — one file must never block the rest
            residual.append(
                {
                    "stage": "storage",
                    "file_uuid": file_plan.get("file_uuid"),
                    "error": str(e),
                }
            )

    def _fail(stage: str, err: object) -> None:
        residual.append({"stage": stage, "error": str(err)})

    _erase_speaker_docs(plan.speaker_uuids, _fail)
    _erase_profile_docs(plan.profile_uuids, _fail)
    _erase_cluster_docs(plan.cluster_uuids, _fail)

    if plan.user_id is not None:
        try:
            from app.services.redis_cache_service import redis_cache

            redis_cache.invalidate_all_for_user(plan.user_id)
        except Exception as cache_err:  # noqa: BLE001 — caches expire on their own TTL
            logger.debug(f"account purge: cache invalidation failed (non-critical): {cache_err}")

    for avatar_path in plan.avatar_paths:
        try:
            from app.services.minio_service import delete_file

            delete_file(avatar_path)
        except Exception as e:  # noqa: BLE001 — one avatar must never block the rest
            residual.append({"stage": "avatar", "avatar_path": avatar_path, "error": str(e)})

    return residual
