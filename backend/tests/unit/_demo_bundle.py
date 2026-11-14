"""A small demo seed bundle on disk, for tests of ``demo_seed subset`` and its callers (#43).

Media bytes are text derived from the item id, so every file's size and SHA-256 are known.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

VERSION = "2026.11.1"


def media_bytes(item_id: str) -> bytes:
    """The media content written for ``item_id``."""
    return f"bytes of {item_id}".encode()


def item(item_id: str, ext: str = "mp4", **overrides) -> dict:
    """One valid manifest item whose media is ``media/<id>.<ext>``."""
    raw = {
        "id": item_id,
        "title": f"Title of {item_id}",
        "language": "en",
        "collection": "science-briefings",
        "media": f"media/{item_id}.{ext}",
        "media_sha256": hashlib.sha256(media_bytes(item_id)).hexdigest(),
        "transcript": f"transcripts/{item_id}.json",
        "source_url": "https://www.nasa.gov/",
        "license": "US-GOV-PD",
        "license_url": "https://www.nasa.gov/nasa-brand-center/images-and-media/",
        "attribution": "NASA",
        "contains_private_individuals": False,
        "synthetic_pii": False,
    }
    raw.update(overrides)
    return raw


def _conversation(conv_id: str, scope: list[str]) -> dict:
    return {
        "title": f"About {conv_id}",
        "items": scope,
        "messages": [
            {"role": "user", "content": "What is it about?"},
            {
                "role": "assistant",
                "content": "Space.",
                "provider": "vllm",
                "model": "m",
                "citations": [{"item_id": scope[0], "start_time": 0.0, "snippet": "x"}],
            },
        ],
    }


def write_bundle(
    root: Path, items: list[dict], conversations: dict[str, list[str]] | None = None
) -> Path:
    """Write a bundle with ``items`` and one conversation per ``{id: scope}`` entry."""
    for sub in ("media", "transcripts", "conversations"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    for raw in items:
        (root / raw["media"]).write_bytes(media_bytes(raw["id"]))
        segment = {"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00", "text": "Hi."}
        (root / raw["transcript"]).write_text(json.dumps({"segments": [segment]}), encoding="utf-8")
    entries = []
    for conv_id, scope in (conversations or {}).items():
        rel = f"conversations/{conv_id}.json"
        (root / rel).write_text(json.dumps(_conversation(conv_id, scope)), encoding="utf-8")
        entries.append({"id": conv_id, "file": rel})
    manifest = {"schema": 1, "bundle_version": VERSION, "items": items, "conversations": entries}
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root
