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
    #: ``(owner user id, file id)``: the object-storage prefix is ``user_<uid>/file_<fid>/``.
    file_owners: list[tuple[int, int]] = field(default_factory=list)
    speaker_ids: list[int] = field(default_factory=list)
    speaker_uuids: list[str] = field(default_factory=list)
    profile_uuids: list[str] = field(default_factory=list)
    cluster_uuids: list[str] = field(default_factory=list)
    avatar_paths: list[str] = field(default_factory=list)
    #: Files a legal hold keeps (Art. 17(3)(e)); counted as retained, never as residual.
    held_file_ids: list[int] = field(default_factory=list)
    #: An active organization hold keeps the organization row (``erase_organization``).
    org_hold_active: bool = False
    email: str | None = field(default=None, repr=False)


def object_prefixes(scope: ErasureScope, captured: Captured) -> list[str]:
    """Media-bucket prefixes the erasure destroys and the receipt counts.

    The whole ``user_<uid>/`` prefix for an account erasure, which also covers objects the
    per-file destroy never names; per-file prefixes otherwise. A file under a legal hold is
    excluded (and so is the whole-user shortcut while one exists): its objects are retained, not
    residual.
    """
    held = set(captured.held_file_ids)
    if scope.kind == "user" and scope.user_id is not None and not held:
        return [f"user_{scope.user_id}/"]
    return [f"user_{uid}/file_{fid}/" for uid, fid in captured.file_owners if fid not in held]
