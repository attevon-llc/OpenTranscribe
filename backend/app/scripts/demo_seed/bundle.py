"""The public demo's seed bundle: format and licence policy (issue #628, decision D6).

A bundle is a directory the reset script mounts read-only at ``/seed``::

    manifest.json
    media/<item>.<ext>          the recording, byte-identical to its source
    transcripts/<item>.json     {"segments": [{"start", "end", "speaker", "text"}],
                                 "speakers": {"SPEAKER_00": "Display name"}}

Every item names its ``collection`` (a key of :data:`COLLECTIONS`); the importer creates that
collection for the demo user and files the item in it.

``manifest.json``::

    {"schema": 1, "bundle_version": "2026.11.1", "items": [ITEM, ...]}

and each ITEM carries the media and transcript paths, the media's SHA-256, a title and a
language, and the provenance fields in :data:`PROVENANCE_FIELDS`. Transcripts are produced
once, offline, on a machine with a GPU; the demo itself never runs speech recognition.

The licence policy is enforced here, before a byte reaches the demo: a licence outside
:data:`ALLOWED_LICENSES` is refused, every item must declare that it contains no private
individual, and any personal data shown for the redaction feature must be invented and
marked ``synthetic_pii``. Validation never touches the database or object storage, so it
runs in unit tests and as ``python -m app.scripts.demo_seed validate``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

#: SPDX identifiers (plus the US federal public-domain status) a demo item may carry.
ALLOWED_LICENSES = frozenset(
    {
        "CC0-1.0",
        "CC-BY-4.0",
        "CC-BY-SA-4.0",
        "PDM-1.0",  # Creative Commons Public Domain Mark
        "US-GOV-PD",  # a work of the US federal government (17 U.S.C. 105)
    }
)

#: Licences whose terms require the attribution text to be shown with the item.
ATTRIBUTION_REQUIRED = frozenset({"CC-BY-4.0", "CC-BY-SA-4.0"})

#: The named collections a seeded file may land in (id -> the name the visitor sees). The
#: catalogue (``demo/seed-catalog.json``) uses the same ids; a new one is added here first.
COLLECTIONS: dict[str, str] = {
    "news": "News",
    "government-meetings": "Government meetings & public talks",
    "business-finance": "Business & finance",
    "training-education": "Training & education",
    "science-briefings": "Science & briefings",
    "privacy-demo": "Privacy demo",
}

PROVENANCE_FIELDS = ("source_url", "license", "license_url", "attribution")
REQUIRED_FIELDS = ("id", "title", "language", "collection", "media", "media_sha256", "transcript")


@dataclass(frozen=True)
class Segment:
    """One transcript segment as stored in the bundle."""

    start: float
    end: float
    speaker: str
    text: str


@dataclass(frozen=True)
class SeedItem:
    """One validated bundle item."""

    id: str
    title: str
    language: str
    media: Path
    media_sha256: str
    transcript: Path
    collection: str
    license: str
    source_url: str
    license_url: str
    attribution: str
    synthetic_pii: bool

    def segments(self) -> list[Segment]:
        """The transcript's segments, in file order."""
        data = json.loads(self.transcript.read_text(encoding="utf-8"))
        return [
            Segment(float(s["start"]), float(s["end"]), str(s["speaker"]), str(s["text"]))
            for s in data["segments"]
        ]

    def speaker_names(self) -> dict[str, str]:
        """Label -> display name; a seeded demo shows names, never bare ``SPEAKER_00``."""
        data = json.loads(self.transcript.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.get("speakers", {}).items()}


class BundleError(ValueError):
    """The bundle is not acceptable; ``problems`` lists every reason found."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def _inside(root: Path, rel: object) -> Path | None:
    if not isinstance(rel, str) or not rel:
        return None
    path = (root / rel).resolve()
    return path if path.is_relative_to(root.resolve()) else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _transcript_problems(item_id: str, path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        segments = data["segments"]
    except (OSError, ValueError, KeyError, TypeError):
        return [f"{item_id}: transcript is not a JSON object with a segments list"]
    if not isinstance(segments, list) or not segments:
        return [f"{item_id}: transcript has no segments"]
    problems: list[str] = []
    previous_start = -1.0
    for n, seg in enumerate(segments):
        try:
            start, end = float(seg["start"]), float(seg["end"])
            text, speaker = seg["text"], seg["speaker"]
        except (KeyError, TypeError, ValueError):
            problems.append(f"{item_id}: segment {n} lacks start/end/speaker/text")
            continue
        if not (0 <= start < end):
            problems.append(f"{item_id}: segment {n} has start {start} >= end {end}")
        if start < previous_start:
            problems.append(f"{item_id}: segment {n} starts before the previous one")
        if not str(text).strip() or not str(speaker).strip():
            problems.append(f"{item_id}: segment {n} has an empty speaker or text")
        previous_start = start
    return problems


def _item_problems(root: Path, raw: dict[str, Any]) -> list[str]:
    item_id = str(raw.get("id") or "?")
    problems = [f"{item_id}: missing {f}" for f in REQUIRED_FIELDS if not raw.get(f)]
    problems += [f"{item_id}: missing {f}" for f in PROVENANCE_FIELDS if not raw.get(f)]
    collection = raw.get("collection")
    if collection and collection not in COLLECTIONS:
        problems.append(f"{item_id}: unknown collection {collection!r}")
    licence = raw.get("license")
    if licence and licence not in ALLOWED_LICENSES:
        problems.append(f"{item_id}: licence {licence!r} is not allowed on the demo")
    if raw.get("contains_private_individuals") is not False:
        problems.append(f"{item_id}: must declare contains_private_individuals: false")
    if not isinstance(raw.get("synthetic_pii", False), bool):
        problems.append(f"{item_id}: synthetic_pii must be true or false")
    media = _inside(root, raw.get("media"))
    if raw.get("media") and (media is None or not media.is_file()):
        problems.append(f"{item_id}: media file missing or outside the bundle")
    elif media is not None and raw.get("media_sha256") and _sha256(media) != raw["media_sha256"]:
        problems.append(f"{item_id}: media SHA-256 does not match the manifest")
    transcript = _inside(root, raw.get("transcript"))
    if raw.get("transcript") and (transcript is None or not transcript.is_file()):
        problems.append(f"{item_id}: transcript missing or outside the bundle")
    elif transcript is not None:
        problems += _transcript_problems(item_id, transcript)
    return problems


def _select(raw_items: list[Any], only: Sequence[str]) -> tuple[list[Any], list[str]]:
    """The items named in ``only``, in that order, and every problem with the selection."""
    wanted = [str(i) for i in only]
    if not wanted:
        return [], ["no items selected"]
    by_id = {str(raw.get("id")): raw for raw in raw_items if isinstance(raw, dict)}
    problems = [
        f"{d}: selected more than once" for d in sorted({i for i in wanted if wanted.count(i) > 1})
    ]
    problems += [f"{i}: not in the bundle" for i in dict.fromkeys(wanted) if i not in by_id]
    return [by_id[i] for i in dict.fromkeys(wanted) if i in by_id], problems


def load_bundle(root: Path, only: Sequence[str] | None = None) -> tuple[str, list[SeedItem]]:
    """``(bundle_version, items)`` for a valid bundle; :class:`BundleError` otherwise.

    ``only`` names a selection (``demo_seed subset``): just those items are validated and
    returned, in that order, so a defect in an item nobody selected does not block it.
    """
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BundleError([f"manifest.json unreadable: {type(exc).__name__}"]) from exc
    problems: list[str] = []
    if manifest.get("schema") != SCHEMA_VERSION:
        problems.append(f"schema must be {SCHEMA_VERSION}")
    if not manifest.get("bundle_version"):
        problems.append("missing bundle_version")
    raw_items = manifest.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        problems.append("items must be a non-empty list")
        raise BundleError(problems)
    ids = [str(i.get("id")) for i in raw_items if isinstance(i, dict)]
    problems += [f"{d}: duplicate id" for d in sorted({i for i in ids if ids.count(i) > 1})]
    if only is not None:
        raw_items, selection_problems = _select(raw_items, only)
        problems += selection_problems
    for raw in raw_items:
        if not isinstance(raw, dict):
            problems.append("an item is not an object")
            continue
        problems += _item_problems(root, raw)
    if problems:
        raise BundleError(problems)
    items = [
        SeedItem(
            id=str(raw["id"]),
            title=str(raw["title"]),
            language=str(raw["language"]),
            media=root / raw["media"],
            media_sha256=str(raw["media_sha256"]),
            transcript=root / raw["transcript"],
            collection=str(raw["collection"]),
            license=str(raw["license"]),
            source_url=str(raw["source_url"]),
            license_url=str(raw["license_url"]),
            attribution=str(raw["attribution"]),
            synthetic_pii=bool(raw.get("synthetic_pii", False)),
        )
        for raw in raw_items
    ]
    return str(manifest["bundle_version"]), items
