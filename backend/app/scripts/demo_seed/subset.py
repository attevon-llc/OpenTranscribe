"""A fixed slice of a seed bundle, written as a bundle of its own (``demo_seed subset``).

The visual baselines and the docs screenshots photograph a few neutral, licensed items of
the public demo's bundle (#43), loaded with the precomputed transcripts by ``demo_seed
import``, so no speech recognition runs and no benchmark recording appears in a public image.
The slice is a bundle in the format ``bundle.py`` describes, so the importer reads it
unchanged: the selected items, and only the conversations whose whole scope is inside the
selection (a conversation citing an item outside it would fail ``load_conversations``).

Only the selected items are validated (licence policy, provenance, media digest), so a defect
in an unrelated item does not block the slice. A caller that records which corpus it used
pins ``bundle_version``: a rebuilt bundle then refuses instead of changing the corpus silently.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.scripts.demo_seed.bundle import BundleError
from app.scripts.demo_seed.bundle import SeedItem
from app.scripts.demo_seed.bundle import load_bundle
from app.scripts.demo_seed.conversations import load_conversations


def select_items(
    bundle: Path, ids: Sequence[str], bundle_version: str | None = None
) -> tuple[str, list[SeedItem]]:
    """``(bundle_version, items)`` for the selection; :class:`BundleError` otherwise."""
    version, items = load_bundle(bundle, only=ids)
    if bundle_version is not None and version != bundle_version:
        raise BundleError([f"the bundle is version {version}; the selection pins {bundle_version}"])
    return version, items


def _place(src: Path, dst: Path) -> None:
    """A hard link where the filesystem allows one (the media is large), else a copy."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _conversations_in_scope(bundle: Path, entries: list[Any], chosen: set[str]) -> list[Any]:
    root = bundle.resolve()
    kept: list[Any] = []
    for entry in entries:
        rel = entry.get("file") if isinstance(entry, dict) else None
        path = (root / rel).resolve() if isinstance(rel, str) and rel else None
        if path is None or not path.is_relative_to(root) or not path.is_file():
            raise BundleError([f"{entry!r}: conversation file missing or outside the bundle"])
        try:
            scope = json.loads(path.read_text(encoding="utf-8")).get("items")
        except (ValueError, AttributeError) as exc:
            raise BundleError([f"{rel}: conversation is not a JSON object"]) from exc
        if isinstance(scope, list) and scope and set(map(str, scope)) <= chosen:
            kept.append(entry)
    return kept


def write_subset(
    bundle: Path, out: Path, ids: Sequence[str], bundle_version: str | None = None
) -> tuple[str, list[SeedItem]]:
    """Write the selection to ``out`` (absent or empty) and return it as loaded from there."""
    version, _ = select_items(bundle, ids, bundle_version)
    if out.exists() and any(out.iterdir()):
        raise BundleError([f"{out} is not empty"])
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    by_id = {str(raw["id"]): raw for raw in manifest["items"] if isinstance(raw, dict)}
    chosen = list(dict.fromkeys(ids))
    items = [by_id[i] for i in chosen]
    conversations = _conversations_in_scope(
        bundle, manifest.get("conversations") or [], set(chosen)
    )
    for raw in items:
        _place(bundle / raw["media"], out / raw["media"])
        _place(bundle / raw["transcript"], out / raw["transcript"])
    for entry in conversations:
        _place(bundle / entry["file"], out / entry["file"])
    out.mkdir(parents=True, exist_ok=True)
    body = {
        "schema": manifest["schema"],
        "bundle_version": version,
        "items": items,
        "conversations": conversations,
    }
    (out / "manifest.json").write_text(json.dumps(body, indent=1), encoding="utf-8")
    written = load_bundle(out)
    load_conversations(out, set(chosen))
    return written
