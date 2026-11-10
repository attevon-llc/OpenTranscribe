"""Plain data the erasure receipt passes between its parts."""

from __future__ import annotations

from collections.abc import Callable
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
    #: Speaker-index ``_id``s a hold keeps: the speakers of held files, and the profiles and
    #: cluster centroids that still hold one of their voices. Never deleted, never residual.
    kept_voiceprint_ids: list[str] = field(default_factory=list)
    #: An active organization hold keeps the organization row (``erase_organization``).
    org_hold_active: bool = False
    #: ``user_<uid>/`` holds objects of no file the erasure keeps or another account owns, so the
    #: whole prefix (with objects no file names) may go. Only ever set for a ``user`` scope.
    whole_user_prefix: bool = False
    #: The same for ``avatars/<uid>/``: no surviving profile's avatar sits under it.
    whole_avatar_prefix: bool = False
    #: What outlives the erasure under ``user_<uid>/`` when the prefix is not taken whole: each
    #: surviving file's ``user_<uid>/file_<fid>/`` (ends in ``/``) and any of its keys of another
    #: shape (exact). ``None`` = not captured, and then nothing is taken in part (``user_residue``).
    user_prefix_keep: list[str] | None = None
    #: The same for ``avatars/<uid>/``: each surviving profile's ``avatars/<uid>/<uuid>/``, or its
    #: avatar key when that is of another shape.
    avatar_prefix_keep: list[str] | None = None
    email: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class HeldVoices:
    """Voiceprints a legal hold keeps out of an erasure (``capture.held_voices``)."""

    speaker_uuids: tuple[str, ...] = ()
    profile_uuids: tuple[str, ...] = ()
    cluster_uuids: tuple[str, ...] = ()

    def doc_ids(self) -> list[str]:
        """Their speaker-index ``_id`` values."""
        return [
            *self.speaker_uuids,
            *(f"profile_{u}" for u in self.profile_uuids),
            *(f"cluster_{u}" for u in self.cluster_uuids),
        ]


@dataclass(frozen=True)
class ResidueSweep:
    """A prefix an account erasure takes in part: every key under ``prefix`` except ``keep``.

    ``keep`` is what outlives the erasure; ``elsewhere`` is what another step of the same erasure
    already destroys and counts (left out so the receipt counts each key once). An entry ending in
    ``/`` covers every key under it; any other entry covers that one key. The destroy and the
    receipt both select with :meth:`selects`, so they act on the same set.
    """

    prefix: str
    keep: tuple[str, ...] = ()
    elsewhere: tuple[str, ...] = ()

    def selects(self, key: str) -> bool:
        """Whether ``key`` is residue: under the prefix and covered by no kept or counted entry."""
        if not key.startswith(self.prefix):
            return False
        return not any(
            key.startswith(entry) if entry.endswith("/") else key == entry
            for entry in (*self.keep, *self.elsewhere)
        )


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


def user_residue(scope: ErasureScope, captured: Captured) -> ResidueSweep | None:
    """What an account erasure takes of ``user_<uid>/`` when it cannot take it whole.

    Objects no file row names (``user_<uid>/youtube_<id>/`` thumbnails, a directory a failed upload
    left) are the subject's data too: everything under the prefix goes except the directories and
    keys of the files that survive (held, or handed to another owner).
    """
    if scope.kind != "user" or scope.user_id is None or captured.whole_user_prefix:
        return None
    if captured.user_prefix_keep is None:
        return None
    return ResidueSweep(
        f"user_{scope.user_id}/",
        keep=tuple(captured.user_prefix_keep),
        elsewhere=tuple(object_prefixes(scope, captured)),
    )


def avatar_residue(scope: ErasureScope, captured: Captured) -> ResidueSweep | None:
    """What an account erasure takes of ``avatars/<uid>/`` when a surviving profile's photo is there.

    Every key except the surviving profiles' own; the erased profiles' current avatars are deleted
    and counted by key (``captured.avatar_paths``).
    """
    if scope.kind != "user" or scope.user_id is None or captured.whole_avatar_prefix:
        return None
    if captured.avatar_prefix_keep is None:
        return None
    return ResidueSweep(
        f"avatars/{scope.user_id}/",
        keep=tuple(captured.avatar_prefix_keep),
        elsewhere=tuple(captured.avatar_paths),
    )


def residue_sweeps(scope: ErasureScope, captured: Captured) -> list[ResidueSweep]:
    """The prefixes this erasure takes in part (``user_residue`` / ``avatar_residue``)."""
    return [s for s in (user_residue(scope, captured), avatar_residue(scope, captured)) if s]


def multipart_targets(
    scope: ErasureScope, captured: Captured
) -> list[tuple[str, Callable[[str], bool] | None]]:
    """``(prefix, selector)`` pairs whose unfinished uploads the erasure aborts and counts.

    A selector of ``None`` takes every upload under the prefix; a residue sweep's takes only what
    it selects.
    """
    whole: list[tuple[str, Callable[[str], bool] | None]] = [
        (p, None) for p in object_prefixes(scope, captured)
    ]
    return whole + [(s.prefix, s.selects) for s in residue_sweeps(scope, captured)]
