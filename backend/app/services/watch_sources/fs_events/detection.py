"""Decide whether native filesystem events can actually be delivered (issue #294).

The backend always runs in a **Linux container**, so ``watchdog`` will happily
pick ``InotifyObserver`` for any path — including paths where inotify provably
never fires:

- a bind mount from a **macOS** host (VirtioFS / gRPC-FUSE): host-side changes
  are not translated into inotify events inside the VM;
- a bind mount of a **Windows** drive under Docker Desktop/WSL2 (9p / drvfs);
- any **network mount** (NFS, SMB/CIFS, a NAS) on any host OS — inotify is a
  kernel-local mechanism and never sees a remote writer.

A naive ``Observer()`` therefore *looks* correct on a Linux developer box and
silently does nothing for everyone else. This module answers "will events
arrive here?" two ways, cheapest first:

1. :func:`classify_path` — read the filesystem type backing the directory out
   of ``/proc/self/mountinfo`` and reject the families that are known not to
   deliver host/remote-side events.
2. :func:`probe_delivery` — the authoritative check: create a throwaway file in
   the watched directory with the observer already running and wait to be told
   about it. Only a positive result keeps the native observer.

Either negative answer means the caller falls back to watchdog's
``PollingObserver``, which works on every filesystem at the cost of a stat sweep.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import threading
import uuid as uuid_pkg
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

MOUNTINFO_PATH = "/proc/self/mountinfo"

# Probe files are dot-prefixed and carry no media extension, so a concurrent
# scan cannot mistake one for content; they also live for barely a second and
# the scanner's file-stability window (watch.file_stability_seconds, 30 s by
# default) rejects anything that young.
PROBE_PREFIX = ".opentranscribe-fsprobe-"

# Filesystem types that never deliver inotify events for writes made by the
# host OS or by a remote machine. Anything with a "fuse" prefix is treated the
# same way — that is what Docker Desktop's macOS bind mounts show up as.
NON_EVENT_FILESYSTEMS = frozenset(
    {
        # network filesystems (NAS, file servers) — the writer is another machine
        "9p",
        "afs",
        "beegfs",
        "ceph",
        "cifs",
        "davfs",
        "glusterfs",
        "lustre",
        "moosefs",
        "nfs",
        "nfs4",
        "ocfs2",
        "smb2",
        "smb3",
        "smbfs",
        # host/guest passthrough — Docker Desktop (macOS), WSL2 Windows drives, VMs
        "drvfs",
        "virtiofs",
        "vboxsf",
        "vmhgfs",
    }
)

_OCTAL_ESCAPE = re.compile(r"\\(\d{3})")


@dataclass(frozen=True)
class DeliveryCheck:
    """Static verdict for a directory, before any observer is started."""

    supported: bool
    reason: str
    fs_type: str | None = None


def _unescape(field: str) -> str:
    """Decode mountinfo's octal escapes (``\\040`` for space, etc.)."""
    return _OCTAL_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), field)


def parse_mountinfo(content: str) -> list[tuple[str, str]]:
    """Parse mountinfo text into ``[(mount_point, fs_type), ...]``.

    Split out from :func:`filesystem_type` so the (fiddly, optional-field)
    format can be unit-tested without a real ``/proc``.
    """
    mounts: list[tuple[str, str]] = []
    for line in content.splitlines():
        head, sep, tail = line.partition(" - ")
        if not sep:
            continue
        head_fields = head.split()
        tail_fields = tail.split()
        if len(head_fields) < 5 or not tail_fields:
            continue
        mounts.append((_unescape(head_fields[4]), tail_fields[0]))
    return mounts


def filesystem_type(path: str | os.PathLike[str]) -> str | None:
    """Return the filesystem type backing ``path``, or None if undeterminable.

    Picks the longest matching mount point, which is how the kernel resolves
    overlapping mounts. Returns None off Linux (no ``/proc/self/mountinfo``) —
    the caller then relies on the live probe alone.
    """
    try:
        with open(MOUNTINFO_PATH, encoding="utf-8") as fh:
            mounts = parse_mountinfo(fh.read())
    except OSError as e:
        logger.debug("Cannot read %s: %s", MOUNTINFO_PATH, e)
        return None

    try:
        target = os.path.realpath(path)
    except OSError:
        target = str(path)

    best: tuple[int, str] | None = None
    for mount_point, fs_type in mounts:
        covers = target == mount_point or target.startswith(mount_point.rstrip("/") + "/")
        if covers and (best is None or len(mount_point) > best[0]):
            best = (len(mount_point), fs_type)
    return best[1] if best else None


def classify_path(path: str | os.PathLike[str]) -> DeliveryCheck:
    """Cheap pre-check: is this mount in a family that never delivers events?"""
    fs_type = filesystem_type(path)
    if fs_type is None:
        return DeliveryCheck(True, "filesystem type unknown — verifying with a live probe", None)
    normalized = fs_type.lower()
    if normalized.startswith("fuse"):
        return DeliveryCheck(
            False,
            f"{fs_type} is a FUSE passthrough (Docker Desktop bind mount) — "
            "host-side changes do not raise inotify events",
            fs_type,
        )
    if normalized in NON_EVENT_FILESYSTEMS:
        return DeliveryCheck(
            False,
            f"{fs_type} is a network/passthrough mount — inotify never sees a remote writer",
            fs_type,
        )
    return DeliveryCheck(True, f"{fs_type} supports native events", fs_type)


def sweep_stale_probes(directory: str | os.PathLike[str]) -> int:
    """Delete probe files left behind by a process that died mid-probe."""
    removed = 0
    with contextlib.suppress(OSError):
        for entry in Path(directory).glob(PROBE_PREFIX + "*"):
            with contextlib.suppress(OSError):
                entry.unlink()
                removed += 1
    return removed


def probe_delivery(
    directory: str | os.PathLike[str],
    arm_probe: Callable[[str], threading.Event],
    timeout: float = 5.0,
) -> tuple[bool, str]:
    """Create a throwaway file and wait for the running observer to report it.

    Args:
        directory: The directory the observer was just scheduled on.
        arm_probe: Callback that registers ``filename`` with the event handler
            and returns the :class:`threading.Event` it will set on delivery.
        timeout: How long to wait for the event.

    Returns:
        ``(delivered, reason)``. A directory we cannot write to yields
        ``False`` — undecidable is treated as unsupported so the caller lands
        on the observer that always works.
    """
    name = f"{PROBE_PREFIX}{uuid_pkg.uuid4().hex}"
    target = Path(directory) / name
    delivered_event = arm_probe(name)

    try:
        target.write_bytes(b"OpenTranscribe FS-event probe\n")
    except OSError as e:
        return False, f"cannot write a probe file here ({type(e).__name__}) — assuming no events"

    try:
        delivered = delivered_event.wait(timeout)
    finally:
        with contextlib.suppress(OSError):
            target.unlink()

    if delivered:
        return True, "native events verified by live probe"
    return False, f"no native event within {timeout:g}s of a probe write"
