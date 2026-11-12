"""``export``: write a bundle's ``transcripts/<item>.json`` from a stack that processed its media.

The inverse of ``import``'s row builder. The bundle's manifest names each item; the item's
media is uploaded to a normal stack (real speech recognition and diarization) and this reads
the finished transcript back, in exactly the shape :mod:`app.scripts.demo_seed.bundle` loads.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.scripts.demo_seed.bundle import BundleError

UNKNOWN_SPEAKER = "UNKNOWN"


def _find_file(db: Session, item: dict) -> MediaFile | None:
    """The newest completed file whose title or filename is the item's (as the chat generator does)."""
    names = {str(item["title"]), Path(str(item.get("media", ""))).name, f"{item['id']}"}
    names |= {f"{item['id']}{Path(str(item.get('media', ''))).suffix}"}
    stmt = (
        select(MediaFile)
        .where(
            MediaFile.status == FileStatus.COMPLETED,
            (MediaFile.title.in_(names)) | (MediaFile.filename.in_(names)),
        )
        .order_by(MediaFile.id.desc())
    )
    return db.scalars(stmt).first()


def _transcript(db: Session, media_file: MediaFile) -> dict:
    speakers = {
        s.id: s for s in db.scalars(select(Speaker).where(Speaker.media_file_id == media_file.id))
    }
    rows = db.scalars(
        select(TranscriptSegment)
        .where(TranscriptSegment.media_file_id == media_file.id)
        .order_by(TranscriptSegment.start_time, TranscriptSegment.id)
    )
    segments = []
    for row in rows:
        text = (row.text or "").strip()
        if not text or row.end_time <= row.start_time:
            continue
        speaker = speakers.get(row.speaker_id) if row.speaker_id is not None else None
        label = (speaker.name if speaker and speaker.name else None) or UNKNOWN_SPEAKER
        segments.append(
            {"start": row.start_time, "end": row.end_time, "speaker": label, "text": text}
        )
    names = {s.name: s.display_name for s in speakers.values() if s.name and s.display_name}
    return {"segments": segments, "speakers": names}


def export_transcripts(db: Session, bundle: Path, manifest_path: Path) -> list[str]:
    """Write every item's transcript under ``bundle``; returns the item ids written.

    Raises :class:`BundleError` listing each item with no finished file in the database.
    """
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    problems: list[str] = []
    written: list[str] = []
    for item in manifest["items"]:
        media_file = _find_file(db, item)
        if media_file is None:
            problems.append(f"{item['id']}: no completed file titled {item['title']!r}")
            continue
        data = _transcript(db, media_file)
        if not data["segments"]:
            problems.append(f"{item['id']}: the processed file has no transcript segments")
            continue
        target = bundle / item["transcript"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(item["id"])
    if problems:
        raise BundleError(problems)
    destination = bundle / "manifest.json"
    if destination.resolve() != manifest_path.resolve():
        shutil.copyfile(manifest_path, destination)
    return written
