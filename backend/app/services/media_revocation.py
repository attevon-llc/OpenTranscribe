"""Whether a takedown actually stopped a quarantined file's already-issued media URLs.

``takedown_service.quarantine_file`` revokes presigned URLs for every copy of a file a URL
can be minted for (issue #907): the media objects (original, thumbnail, playback rendition)
are TAGGED, so a release can restore the same URLs, and the cache-bucket copies (derived
renders, bulk exports) are DELETED (``media_revocation_cache``). Both are storage-plane side
effects that can fail independently of the database write, and two things have to be true
for a revocation to be real:

1. **Every write landed.** On native S3 the usual cause of a refusal is an app identity
   without ``s3:GetObjectTagging`` / ``s3:PutObjectTagging``. Before this module the refusal
   was a WARNING while the quarantine API answered a clean 200, so an issued URL kept working
   for ``MEDIA_URL_EXPIRE_SECONDS`` with nobody told.
2. **Something enforces the tag.** On MinIO that is the restricted presign identity
   (``storage_presign_identity``); on native S3 the operator-attached bucket-policy Deny. A
   tag with no enforcer is just a label. Anything this module cannot confirm is reported as
   unverified, never as enforced.

**Decision: the takedown still succeeds at the database level when either is missing.** The
quarantine row is what every read surface gates on, so blocking or rolling it back because
storage refused a write would leave the file MORE exposed. Instead the outcome is reported:
the action returns a :class:`MediaRevocationReport` (the API's ``media_revocation``), the
audit event is written with outcome ``partial``, and the log line is an ERROR naming the
missing storage actions.

**Ordering.** The storage writes run after the database commit and outside the row lock (a
transaction must not be held across object-storage round trips), so a quarantine and a
release racing each other could apply their tags in the wrong order. :func:`settle_file`
therefore applies the state the database holds, re-reads it after writing and re-applies
until the two agree: whichever action finishes last leaves storage matching the last commit.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING
from typing import Literal

from app.core.config import settings
from app.services import storage_backend
from app.services import storage_presign_identity

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.media import MediaFile
    from app.services.minio_service import QuarantineTagWrite

logger = logging.getLogger(__name__)

ObjectRole = Literal["original", "thumbnail", "playback", "derived", "bulk_export"]
Enforcement = Literal["presign_identity", "bucket_policy", "none", "unknown"]

#: Stable reason codes on the wire (``media_revocation.reason``). A client maps them to copy;
#: never change a value, only add one.
REASON_TAG_WRITE_FAILED = "tag_write_failed"
REASON_COPY_REMOVAL_FAILED = "copy_removal_failed"
REASON_ENFORCEMENT_MISSING = "enforcement_missing"
REASON_ENFORCEMENT_UNVERIFIED = "enforcement_unverified"
REASON_TAG_CLEAR_FAILED = "tag_clear_failed"

#: Roles that are tagged (and restored on release); the others are deleted.
TAGGED_ROLES: frozenset[str] = frozenset({"original", "thumbnail", "playback"})

# Retry budget for clearing the tag on release (issue #907). This is the dangerous edge of
# the two directions: a newly-minted presigned URL for a STILL-TAGGED object is ALSO 403
# (measured), so a failed untag leaves a file released in the DB but unplayable until a
# retry succeeds. Only a ``failed`` write is retried; a missing object is final.
UNTAG_RETRIES = 3
UNTAG_BACKOFF_SECONDS = 0.5

#: How many times :func:`settle_file` re-reads the database and re-applies. Each round is
#: one quarantine or release committing in the middle of this one's writes.
SETTLE_ROUNDS = 4


@dataclasses.dataclass(frozen=True)
class ObjectTagResult:
    """The storage write for one of a file's objects."""

    role: ObjectRole
    write: QuarantineTagWrite


@dataclasses.dataclass(frozen=True)
class MediaRevocationReport:
    """What a quarantine (``revoke``) or release (``restore``) did to a file's media URLs.

    Attributes:
        action: ``revoke`` on quarantine, ``restore`` on release.
        objects: One entry per object the file has.
        enforcement: What makes the tag stop a URL on this deployment (``revoke`` only).
        policy_read_denied: The enforcement check was refused ``s3:GetBucketPolicy``.
    """

    action: Literal["revoke", "restore"]
    objects: tuple[ObjectTagResult, ...]
    enforcement: Enforcement | None = None
    policy_read_denied: bool = False

    @property
    def failed_roles(self) -> list[str]:
        """Roles of the objects that are NOT in the state the action asked for."""
        return [o.role for o in self.objects if o.write.outcome == "failed"]

    @property
    def tagged_any(self) -> bool:
        """Whether any tagged object exists and was tagged (absent objects serve nothing)."""
        return any(o.write.outcome == "applied" and o.role in TAGGED_ROLES for o in self.objects)

    @property
    def missing_permissions(self) -> list[str]:
        """Storage actions the app identity was refused, sorted, each named once."""
        denied = {o.write.denied_action for o in self.objects if o.write.denied_action}
        if self.policy_read_denied:
            denied.add(storage_presign_identity.BUCKET_POLICY_READ_ACTION)
        return sorted(a for a in denied if a)

    @property
    def reason(self) -> str | None:
        """Why the action is incomplete (a ``REASON_*`` code), or None when it is complete."""
        failed = set(self.failed_roles)
        if self.action == "restore":
            return REASON_TAG_CLEAR_FAILED if failed else None
        if failed & TAGGED_ROLES:
            return REASON_TAG_WRITE_FAILED
        if failed:
            return REASON_COPY_REMOVAL_FAILED
        if not self.tagged_any:
            return None
        if self.enforcement == "none":
            return REASON_ENFORCEMENT_MISSING
        if self.enforcement != "presign_identity" and self.enforcement != "bucket_policy":
            return REASON_ENFORCEMENT_UNVERIFIED
        return None

    @property
    def complete(self) -> bool:
        """True only when every object is in the asked-for state (and, on revoke, enforced)."""
        return self.reason is None

    @property
    def exposure_seconds(self) -> int | None:
        """How long an already-issued URL may keep working, when a revoke is incomplete."""
        if self.action != "revoke" or self.complete:
            return None
        return storage_backend.clamp_presigned_expiry(settings.MEDIA_URL_EXPIRE_SECONDS)

    def to_wire(self) -> dict:
        """The full ``media_revocation`` object (platform-admin routes)."""
        return {
            "action": self.action,
            "complete": self.complete,
            "reason": self.reason,
            "enforcement": self.enforcement,
            "missing_permissions": self.missing_permissions,
            "exposure_seconds": self.exposure_seconds,
            "objects": [
                {
                    "role": o.role,
                    "outcome": o.write.outcome,
                    "error_code": o.write.error_code,
                    "failed_action": o.write.failed_action,
                }
                for o in self.objects
            ],
        }

    def to_tenant_wire(self) -> dict:
        """What an organization admin may see: no storage permissions, enforcer or errors.

        Those describe the operator's infrastructure, not the tenant's file; the operator gets
        them on the platform route, the audit event and the log.
        """
        reason = None
        if not self.complete:
            reason = "revocation_incomplete" if self.action == "revoke" else "restore_incomplete"
        return {
            "action": self.action,
            "complete": self.complete,
            "reason": reason,
            "exposure_seconds": self.exposure_seconds,
        }

    def to_audit(self) -> dict:
        """The audit-event form: the decision fields, never object keys."""
        return {
            "complete": self.complete,
            "reason": self.reason,
            "enforcement": self.enforcement,
            "missing_permissions": self.missing_permissions,
            "failed_objects": self.failed_roles,
        }


def _media_objects(file: MediaFile) -> list[tuple[ObjectRole, str]]:
    """The file's media-bucket objects a browser-facing presigned URL can be minted for."""
    objects: list[tuple[ObjectRole, str]] = []
    if file.storage_path:
        objects.append(("original", str(file.storage_path)))
    if file.thumbnail_path:
        objects.append(("thumbnail", str(file.thumbnail_path)))
    if file.playback_path:
        objects.append(("playback", str(file.playback_path)))
    return objects


def resolve_enforcement() -> tuple[Enforcement, bool]:
    """What would make a quarantine tag stop an already-issued URL right now.

    Read at action time, not cached from startup: an operator who attaches the bucket policy
    after boot must see the takedown report it. Conservative: a policy whose Deny this code
    cannot prove covers the app's URLs is ``unknown``, never enforced.

    Returns:
        ``(enforcement, policy_read_denied)``. Never raises.
    """
    if storage_backend.is_native_s3():
        from app.services.minio_service import minio_client

        state, code = storage_presign_identity.read_native_s3_revocation_policy(minio_client)
        if state == "enforced":
            return "bucket_policy", False
        if state == "missing":
            return "none", False
        return "unknown", state == "unreadable" and code == "AccessDenied"
    if storage_presign_identity.presign_identity_available():
        return "presign_identity", False
    return "none", False


def _log_incomplete(file: MediaFile, report: MediaRevocationReport) -> None:
    missing = report.missing_permissions
    perms = f"; the storage identity is missing {', '.join(missing)}" if missing else ""
    if report.action == "revoke":
        logger.error(
            f"Quarantine of file {file.id} ({file.uuid}) did NOT revoke its presigned media "
            f"URLs ({report.reason}; enforcement={report.enforcement}; failed objects: "
            f"{', '.join(report.failed_roles) or 'none'}{perms}). An already-issued URL keeps "
            f"working for up to {report.exposure_seconds}s. The file is quarantined in the "
            "database regardless. See docs/abuse-and-takedown.md."
        )
    else:
        logger.error(
            f"Presign-tag clear FAILED after {UNTAG_RETRIES} attempts for file {file.id} "
            f"({file.uuid}), objects: {', '.join(report.failed_roles)}{perms}. The file is "
            "released in the database but those objects keep returning 403 on every presigned "
            "URL until the tag is cleared."
        )


def revoke_media(
    file: MediaFile,
    *,
    enforcement: tuple[Enforcement, bool] | None = None,
    log: bool = True,
) -> MediaRevocationReport:
    """Tag the file's media objects, delete its cache copies, and report whether URLs died.

    Args:
        file: The (committed) quarantined file.
        enforcement: A verdict already resolved by the caller (the startup backfill resolves
            it once per run); resolved here otherwise.
        log: Log an incomplete revocation at ERROR (the backfill logs one summary instead).

    Returns:
        The report. Never raises.
    """
    from app.services.media_revocation_cache import remove_cache_copies
    from app.services.minio_service import write_object_quarantine_tag

    results = [
        ObjectTagResult(role, write_object_quarantine_tag(key, True))
        for role, key in _media_objects(file)
    ]
    results.extend(remove_cache_copies(int(file.id)))
    report = MediaRevocationReport("revoke", tuple(results))
    # Resolved even when a write failed, so one response names every missing permission.
    if any(o.write.outcome != "absent" and o.role in TAGGED_ROLES for o in results):
        verdict, denied = enforcement if enforcement is not None else resolve_enforcement()
        report = dataclasses.replace(report, enforcement=verdict, policy_read_denied=denied)
    if log and not report.complete:
        _log_incomplete(file, report)
    return report


def _untag_with_retry(key: str) -> QuarantineTagWrite:
    from app.services.minio_service import write_object_quarantine_tag

    write = write_object_quarantine_tag(key, False)
    for _ in range(1, UNTAG_RETRIES):
        if write.outcome != "failed":
            break
        time.sleep(UNTAG_BACKOFF_SECONDS)
        write = write_object_quarantine_tag(key, False)
    return write


def restore_media(file: MediaFile, *, log: bool = True) -> MediaRevocationReport:
    """Clear the quarantine tag on the file's media objects, retrying failures.

    Cache copies were deleted on quarantine and are rebuilt on demand, so nothing else is
    restored. Never raises; a tag still in place after every retry is logged at ERROR,
    because a still-tagged object 403s a BRAND NEW presigned URL too.
    """
    results = tuple(
        ObjectTagResult(role, _untag_with_retry(key)) for role, key in _media_objects(file)
    )
    report = MediaRevocationReport("restore", results)
    if log and not report.complete:
        _log_incomplete(file, report)
    return report


def settle_file(file: MediaFile, read_quarantined: Callable[[], bool]) -> MediaRevocationReport:
    """Make storage match the file's CURRENT quarantine state, and keep it matching.

    Applies what the database holds (not what the caller just did), re-reads it after the
    writes, and re-applies when another action committed in between. Without this, a release
    whose untag is still retrying could finish after a second quarantine committed and leave
    a quarantined file's media playable.

    Args:
        file: The file (only its id, uuid and object paths are read).
        read_quarantined: Reads ``is_quarantined`` from the database, fresh.

    Returns:
        The report of the last state applied.
    """
    desired = read_quarantined()
    report = revoke_media(file, log=False) if desired else restore_media(file, log=False)
    for _ in range(SETTLE_ROUNDS - 1):
        now = read_quarantined()
        if now == desired:
            break
        logger.info(
            f"File {file.id}'s quarantine state changed while its media tags were being "
            f"written; re-applying {'revoke' if now else 'restore'}."
        )
        desired = now
        report = revoke_media(file, log=False) if desired else restore_media(file, log=False)
    if not report.complete:
        _log_incomplete(file, report)
    return report


def settle_object_tag(
    role: ObjectRole, key: str, read_quarantined: Callable[[], bool], *, file_label: str
) -> QuarantineTagWrite | None:
    """Tag (or untag) one media object that appeared after the file's takedown ran.

    For a playback rendition finishing during a takedown: the takedown could not see an
    object that did not exist yet, so the producer applies the current state itself, with the
    same re-read-and-correct loop as :func:`settle_file`. A failure is an ERROR naming the
    missing permission, never a WARNING. Returns None when nothing had to be written.
    """
    from app.services.minio_service import write_object_quarantine_tag

    desired = read_quarantined()
    result: QuarantineTagWrite | None = None
    for _ in range(SETTLE_ROUNDS):
        if desired or result is not None:
            result = write_object_quarantine_tag(key, desired)
        now = read_quarantined()
        if now == desired:
            break
        desired = now
    if result is not None and result.outcome == "failed":
        denied = (
            f"; the storage identity is missing {result.denied_action}"
            if result.denied_action
            else ""
        )
        logger.error(
            f"Could not {'tag' if desired else 'untag'} the {role} of {file_label} "
            f"({result.error_code}{denied}). "
            + (
                "Its presigned URLs are NOT revoked although the file is quarantined."
                if desired
                else "It keeps returning 403 although the file is released."
            )
        )
    return result
