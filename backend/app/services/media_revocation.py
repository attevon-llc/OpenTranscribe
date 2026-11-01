"""Whether a takedown actually stopped a quarantined file's already-issued media URLs.

``takedown_service.quarantine_file`` revokes presigned media URLs by tagging every object a
URL can be minted for (issue #907); ``release_file`` restores them by clearing the tag. Both
are storage-plane side effects that can fail independently of the database write, and two
separate things have to be true for a revocation to be real:

1. **Every tag write landed.** On native S3 the usual cause of a refusal is an app identity
   without ``s3:GetObjectTagging`` / ``s3:PutObjectTagging`` on the media objects. Before this
   module the refusal was a WARNING in the log while the quarantine API answered a clean 200,
   so an issued URL kept working for ``MEDIA_URL_EXPIRE_SECONDS`` with nobody told.
2. **Something enforces the tag.** On MinIO that is the restricted presign identity
   (``storage_presign_identity``); on native S3 it is the operator-attached bucket-policy
   Deny. A tag with no enforcer is just a label.

**Decision: the takedown still succeeds at the database level when either is missing.** The
quarantine row is what every read surface gates on (list, detail, stream-url, search, chat,
export), so blocking or rolling it back because storage refused a tag would leave the file
MORE exposed, not less. Instead the outcome is reported, never shrugged off: the action
returns a :class:`MediaRevocationReport` that the API returns as ``media_revocation`` (with
``presign_revoked`` / ``presign_tag_cleared`` derived from it), the audit event records it with
outcome ``partial``, and the log line is an ERROR naming the missing storage actions.

:func:`report_native_s3_tagging_posture` is the startup half: on native S3 it writes and tags
a probe object so a missing tagging permission is found at boot, not at takedown time.
"""

from __future__ import annotations

import dataclasses
import io
import logging
import time
from typing import TYPE_CHECKING
from typing import Literal

from app.core.config import settings
from app.core.constants import STORAGE_QUARANTINE_TAG_KEY
from app.services import storage_backend
from app.services import storage_presign_identity

if TYPE_CHECKING:  # pragma: no cover - typing only
    from minio import Minio

    from app.models.media import MediaFile
    from app.services.minio_service import QuarantineTagWrite

logger = logging.getLogger(__name__)

ObjectRole = Literal["original", "thumbnail", "playback"]
Enforcement = Literal["presign_identity", "bucket_policy", "none", "unknown"]

#: Stable reason codes on the wire (``media_revocation.reason``). A client maps them to copy;
#: never change a value, only add one.
REASON_TAG_WRITE_FAILED = "tag_write_failed"
REASON_ENFORCEMENT_MISSING = "enforcement_missing"
REASON_ENFORCEMENT_UNVERIFIED = "enforcement_unverified"
REASON_TAG_CLEAR_FAILED = "tag_clear_failed"

# Retry budget for clearing the tag on release (issue #907). This is the dangerous edge of
# the two directions: a newly-minted presigned URL for a STILL-TAGGED object is ALSO 403
# (measured), so a failed untag leaves a file released in the DB but unplayable until a
# retry succeeds. Only a ``failed`` write is retried; a missing object is final.
UNTAG_RETRIES = 3
UNTAG_BACKOFF_SECONDS = 0.5

#: Key of the throwaway object the startup probe tags. Outside ``user_<uid>/file_<fid>/``, so
#: the storage orphan sweep never considers it; overwritten (not accumulated) on every boot.
TAG_PROBE_OBJECT_KEY = "_opentranscribe/probes/quarantine-tag-probe"


@dataclasses.dataclass(frozen=True)
class ObjectTagResult:
    """The tag write for one of a file's objects."""

    role: ObjectRole
    write: QuarantineTagWrite


@dataclasses.dataclass(frozen=True)
class MediaRevocationReport:
    """What a quarantine (``revoke``) or release (``restore``) did to a file's media URLs.

    Attributes:
        action: ``revoke`` on quarantine, ``restore`` on release.
        objects: One entry per object the file has (original, thumbnail, playback rendition).
        enforcement: What makes the tag stop a URL on this deployment (``revoke`` only; None
            on ``restore``, where the only risk is a tag left behind).
        policy_read_denied: The enforcement check could not read the bucket policy because
            the identity lacks ``s3:GetBucketPolicy``.
    """

    action: Literal["revoke", "restore"]
    objects: tuple[ObjectTagResult, ...]
    enforcement: Enforcement | None = None
    policy_read_denied: bool = False

    @property
    def failed_roles(self) -> list[str]:
        """Roles of the objects whose tag is not what the action asked for."""
        return [o.role for o in self.objects if o.write.outcome == "failed"]

    @property
    def tagged_any(self) -> bool:
        """Whether any object exists and was tagged (absent objects serve nothing)."""
        return any(o.write.outcome == "applied" for o in self.objects)

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
        if self.action == "restore":
            return REASON_TAG_CLEAR_FAILED if self.failed_roles else None
        if self.failed_roles:
            return REASON_TAG_WRITE_FAILED
        if not self.tagged_any:
            return None
        if self.enforcement == "none":
            return REASON_ENFORCEMENT_MISSING
        if self.enforcement == "unknown":
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
        """The ``media_revocation`` object of the quarantine/release response."""
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

    def to_audit(self) -> dict:
        """The audit-event form: the decision fields, never object keys."""
        wire = self.to_wire()
        return {
            "complete": wire["complete"],
            "reason": wire["reason"],
            "enforcement": wire["enforcement"],
            "missing_permissions": wire["missing_permissions"],
            "failed_objects": self.failed_roles,
        }


def _file_objects(file: MediaFile) -> list[tuple[ObjectRole, str]]:
    """Every object of ``file`` a browser-facing presigned URL can be minted for."""
    objects: list[tuple[ObjectRole, str]] = []
    if file.storage_path:
        objects.append(("original", str(file.storage_path)))
    if file.thumbnail_path:
        objects.append(("thumbnail", str(file.thumbnail_path)))
    # The playback rendition is the same media in another codec, so an already-minted URL
    # for it must die with the original's.
    if file.playback_path:
        objects.append(("playback", str(file.playback_path)))
    return objects


def resolve_enforcement() -> tuple[Enforcement, bool]:
    """What would make a quarantine tag stop an already-issued URL right now.

    Read at action time, not cached from startup: an operator who attaches the bucket policy
    after boot must see the takedown report it. On native S3 this is one
    ``GetBucketPolicy`` call per takedown (a rare, operator-driven action).

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
        return "unknown", code == "AccessDenied"
    if storage_presign_identity.presign_identity_available():
        return "presign_identity", False
    return "none", False


def revoke_media(file: MediaFile) -> MediaRevocationReport:
    """Tag every object of a just-quarantined file and report whether its URLs are dead.

    Args:
        file: The (committed) quarantined file.

    Returns:
        The report. Never raises; an incomplete revocation is logged at ERROR with the
        storage actions the identity was refused.
    """
    from app.services.minio_service import write_object_quarantine_tag

    results = tuple(
        ObjectTagResult(role, write_object_quarantine_tag(key, True))
        for role, key in _file_objects(file)
    )
    report = MediaRevocationReport("revoke", results)
    # Resolved even when a write failed, so one response names every missing permission.
    if any(o.write.outcome != "absent" for o in results):
        enforcement, policy_read_denied = resolve_enforcement()
        report = dataclasses.replace(
            report, enforcement=enforcement, policy_read_denied=policy_read_denied
        )
    if not report.complete:
        missing = report.missing_permissions
        logger.error(
            f"Quarantine of file {file.id} ({file.uuid}) did NOT revoke its presigned media "
            f"URLs ({report.reason}; enforcement={report.enforcement}; failed objects: "
            f"{', '.join(report.failed_roles) or 'none'}"
            + (f"; the storage identity is missing {', '.join(missing)}" if missing else "")
            + f"). An already-issued URL keeps working for up to {report.exposure_seconds}s. "
            "The file is quarantined in the database regardless. See "
            "docs/abuse-and-takedown.md."
        )
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


def restore_media(file: MediaFile) -> MediaRevocationReport:
    """Clear the quarantine tag on every object of a just-released file, retrying failures.

    Args:
        file: The (committed) released file.

    Returns:
        The report. Never raises; a tag still in place after every retry is logged at ERROR,
        because a still-tagged object 403s a BRAND NEW presigned URL too.
    """
    results = tuple(
        ObjectTagResult(role, _untag_with_retry(key)) for role, key in _file_objects(file)
    )
    report = MediaRevocationReport("restore", results)
    if not report.complete:
        missing = report.missing_permissions
        logger.error(
            f"Presign-tag clear FAILED after {UNTAG_RETRIES} attempts for file {file.id} "
            f"({file.uuid}), objects: {', '.join(report.failed_roles)}"
            + (f"; the storage identity is missing {', '.join(missing)}" if missing else "")
            + ". The file is released in the database but those objects keep returning 403 "
            "on every presigned URL until the tag is cleared."
        )
    return report


@dataclasses.dataclass(frozen=True)
class TaggingProbe:
    """Result of the startup tagging probe.

    Attributes:
        verified: The identity read and wrote an object's tags (set AND cleared).
        missing_permissions: Storage actions refused with ``AccessDenied``.
        error: A non-permission failure that left the result inconclusive, else None.
    """

    verified: bool
    missing_permissions: tuple[str, ...] = ()
    error: str | None = None


def probe_object_tagging(client: Minio, bucket: str) -> TaggingProbe:
    """Write a probe object, set and clear the quarantine tag on it, then delete it.

    Exercises exactly the calls a takedown makes (``write_object_quarantine_tag``'s
    read-modify-write) with the same identity, on an object the app owns. Needs
    ``s3:PutObject`` and ``s3:DeleteObject`` for the probe itself, which the app already
    needs to store and delete media. Never raises.

    Args:
        client: The storage client the takedown actions use.
        bucket: The media bucket.

    Returns:
        The probe result.
    """
    from minio.commonconfig import Tags
    from minio.error import S3Error

    from app.services.minio_service import TAG_READ_ACTION
    from app.services.minio_service import TAG_WRITE_ACTION

    body = b"opentranscribe quarantine-tag probe"
    try:
        client.put_object(bucket, TAG_PROBE_OBJECT_KEY, io.BytesIO(body), len(body))
    except Exception as e:  # noqa: BLE001 — a probe failure is reported, never raised at boot
        return TaggingProbe(False, error=f"probe object write failed ({_code(e)})")

    missing: list[str] = []
    error: str | None = None
    tagged = Tags.new_object_tags()
    tagged[STORAGE_QUARANTINE_TAG_KEY] = storage_presign_identity.QUARANTINE_TAG_VALUE
    steps = (
        (TAG_READ_ACTION, lambda: client.get_object_tags(bucket, TAG_PROBE_OBJECT_KEY)),
        (TAG_WRITE_ACTION, lambda: client.set_object_tags(bucket, TAG_PROBE_OBJECT_KEY, tagged)),
        (
            TAG_WRITE_ACTION,
            lambda: client.set_object_tags(bucket, TAG_PROBE_OBJECT_KEY, Tags.new_object_tags()),
        ),
    )
    for action, call in steps:
        try:
            call()
        except S3Error as e:
            if e.code == "AccessDenied":
                if action not in missing:
                    missing.append(action)
            elif error is None:
                error = f"{action} failed ({e.code})"
        except Exception as e:  # noqa: BLE001 — reported, never raised at boot
            if error is None:
                error = f"{action} failed ({_code(e)})"
    try:
        client.remove_object(bucket, TAG_PROBE_OBJECT_KEY)
    except Exception as e:  # noqa: BLE001 — a leftover probe object is harmless; say so
        logger.warning(
            f"Could not delete the quarantine-tag probe object '{TAG_PROBE_OBJECT_KEY}' in "
            f"'{bucket}' ({_code(e)}); it is a {len(body)}-byte object, safe to delete by hand."
        )
    return TaggingProbe(not missing and error is None, tuple(missing), error)


def _code(error: Exception) -> str:
    return str(getattr(error, "code", None) or type(error).__name__)


def report_native_s3_tagging_posture(client: Minio) -> TaggingProbe:
    """Log, once at startup on native S3, whether the app identity can quarantine-tag media.

    Without ``s3:GetObjectTagging`` + ``s3:PutObjectTagging`` on the media objects every
    takedown fails to revoke issued URLs even with the bucket-policy Deny attached, and
    before this check that was discovered at takedown time. Non-fatal like the rest of the
    storage posture checks: ERROR naming the exact missing actions, WARNING when the probe
    was inconclusive, INFO when verified.

    Args:
        client: The storage client (``minio_service.minio_client``).

    Returns:
        The probe result.
    """
    bucket = settings.MEDIA_BUCKET_NAME
    probe = probe_object_tagging(client, bucket)
    if probe.missing_permissions:
        logger.error(
            f"STORAGE_BACKEND=s3: the storage identity is missing "
            f"{', '.join(probe.missing_permissions)} on arn:aws:s3:::{bucket}/* — quarantine "
            f"CANNOT revoke already-issued presigned media URLs (they keep working for up to "
            f"{storage_backend.clamp_presigned_expiry(settings.MEDIA_URL_EXPIRE_SECONDS)}s), "
            f"and every takedown will report media_revocation.complete=false. Grant them; see "
            f"docs/abuse-and-takedown.md."
        )
    elif probe.error:
        logger.warning(
            f"STORAGE_BACKEND=s3: could not verify that the storage identity can tag media "
            f"objects for quarantine ({probe.error}). Takedowns report the real outcome in "
            f"media_revocation; see docs/abuse-and-takedown.md."
        )
    else:
        logger.info(
            "STORAGE_BACKEND=s3: the storage identity can read and write object tags — "
            "quarantine tags will land."
        )
    return probe
