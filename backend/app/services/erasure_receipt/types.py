"""Plain data the erasure receipt passes between its parts."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Literal

ScopeKind = Literal["user", "org_member", "organization", "file"]


@dataclass(frozen=True)
class ErasureScope:
    """Whose data an erasure removes. ``file`` names recordings, whoever owns them."""

    kind: ScopeKind
    user_id: int | None = None
    organization_id: int | None = None
    file_ids: tuple[int, ...] = ()


@dataclass
class Captured:
    """Identifiers that are gone after the destroy and must be named for the post-check.

    Read BEFORE destroying. Nothing here is persisted. ``email`` is held in memory only (for the
    login-lockout key) and is excluded from ``repr`` so it cannot reach a log line.
    """

    file_ids: list[int] = field(default_factory=list)
    file_uuids: list[str] = field(default_factory=list)
    #: ``user_<uploader>/file_<fid>/`` of each file, read from the file's own object keys. The
    #: key names the UPLOADER and never moves, so a file handed to another owner (an
    #: organization content transfer) keeps its old directory: never rebuild it from ``user_id``.
    file_dirs: list[str] = field(default_factory=list)
    speaker_ids: list[int] = field(default_factory=list)
    speaker_uuids: list[str] = field(default_factory=list)
    profile_uuids: list[str] = field(default_factory=list)
    cluster_uuids: list[str] = field(default_factory=list)
    avatar_paths: list[str] = field(default_factory=list)
    #: Files a legal hold keeps (Art. 17(3)(e)); counted as retained, never as residual.
    held_file_ids: list[int] = field(default_factory=list)
    #: An active organization hold keeps the organization row (``erase_organization``).
    org_hold_active: bool = False
    #: ``user_<uid>/`` holds objects of no file the erasure keeps or another account owns, so the
    #: whole prefix (with objects no file names) may go. Only ever set for a ``user`` scope.
    whole_user_prefix: bool = False
    #: The same for ``avatars/<uid>/``: no surviving profile's avatar sits under it.
    whole_avatar_prefix: bool = False
    email: str | None = field(default=None, repr=False)


def object_prefixes(scope: ErasureScope, captured: Captured) -> list[str]:
    """Media-bucket prefixes the erasure destroys and the receipt counts.

    The whole ``user_<uid>/`` prefix for an account erasure when nothing under it survives the
    erasure, which also covers objects the per-file destroy never names. Otherwise each erased
    file's own directory: under someone else's prefix for a file the subject received, and never
    a held file's or a transferred-away file's (both are left out of ``file_dirs``).
    """
    if scope.kind == "user" and scope.user_id is not None and captured.whole_user_prefix:
        return [f"user_{scope.user_id}/"]
    return list(dict.fromkeys(captured.file_dirs))


def avatar_prefixes(scope: ErasureScope, captured: Captured) -> list[str]:
    """``avatars/<uid>/`` for an account erasure when no surviving profile's avatar is under it.

    An avatar key names the profile's owner at upload time and never moves, so a profile handed
    to another owner keeps its photo there. The erased profiles' own keys are
    ``captured.avatar_paths``, deleted and counted one by one.
    """
    if scope.kind == "user" and scope.user_id is not None and captured.whole_avatar_prefix:
        return [f"avatars/{scope.user_id}/"]
    return []
