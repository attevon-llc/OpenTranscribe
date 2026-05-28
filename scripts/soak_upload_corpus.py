"""Upload all benchmark/test_audio/ files into the bench DB and emit corpus.json.

Phase 0.5 of the GPU soak. Sequential (conc=1) upload + wait-to-complete, then
writes docs/benchmark-corpus/corpus.json from the resulting bench-DB UUIDs.
The conc=1 wall times recorded here are the soak's sequential baseline.
"""
import hashlib
import json
import mimetypes
import os
import time
from pathlib import Path

import requests


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

BACKEND = os.environ.get("BACKEND_URL", "http://localhost:5174")
EMAIL = os.environ.get("BENCHMARK_EMAIL", "admin@example.com")
PASSWORD = os.environ.get("BENCHMARK_PASSWORD", "password")
AUDIO_DIR = Path("benchmark/test_audio")
EXTS = {".wav", ".mp3", ".m4a", ".mp4", ".flac"}

r = requests.post(f"{BACKEND}/api/auth/token",
                  data={"username": EMAIL, "password": PASSWORD}, timeout=15)
r.raise_for_status()
token = r.json()["access_token"]
H = {"Authorization": f"Bearer {token}"}

files = sorted(p for p in AUDIO_DIR.iterdir() if p.suffix.lower() in EXTS)
if not files:
    raise SystemExit("No audio files found in benchmark/test_audio/ — aborting.")

print(f"Uploading {len(files)} files sequentially. This pre-populates the bench DB.", flush=True)
uploaded = []
for i, p in enumerate(files, 1):
    print(f"  [{i}/{len(files)}] uploading {p.name} ({p.stat().st_size/1e6:.1f} MB) ...", flush=True)
    content_type = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    upload_headers = {**H, "X-File-Hash": sha256_file(p)}
    with open(p, "rb") as fh:
        r = requests.post(f"{BACKEND}/api/files", headers=upload_headers,
                          files={"file": (p.name, fh, content_type)}, timeout=3600)
    r.raise_for_status()
    j = r.json()
    file_id = j.get("uuid") or j.get("id")
    uploaded.append({"path": str(p), "filename": p.name, "uuid": file_id, "t_upload": time.time()})
    print(f"      uploaded -> uuid={file_id}", flush=True)

print("\nWaiting for all uploads to finish initial pipeline run ...", flush=True)
pending = {x["uuid"] for x in uploaded}
deadline = time.time() + 8 * 3600
while pending and time.time() < deadline:
    for uuid in list(pending):
        info = requests.get(f"{BACKEND}/api/files/{uuid}", headers=H, timeout=15).json()
        status = info.get("status", "unknown")
        if status == "completed":
            for u in uploaded:
                if u["uuid"] == uuid:
                    u["duration_s"] = float(info.get("duration") or 0)
                    u["size_mb"] = int(info.get("file_size", 0)) // (1024 * 1024)
                    u["wall_s"] = round(time.time() - u["t_upload"], 1)
            pending.discard(uuid)
            print(f"  [{len(uploaded)-len(pending)}/{len(uploaded)}] {uuid} -> completed "
                  f"({info.get('duration', 0):.0f}s audio)", flush=True)
        elif status == "error":
            print(f"  WARNING: file {uuid} entered error state; dropping from corpus.", flush=True)
            pending.discard(uuid)
    if pending:
        time.sleep(10)

uploaded = [u for u in uploaded if "duration_s" in u]


def tier(duration_s: float) -> int:
    if duration_s < 25 * 60:
        return 1
    if duration_s < 60 * 60:
        return 2
    if duration_s < 180 * 60:
        return 3
    return 4


uploaded.sort(key=lambda x: x["duration_s"])
corpus = {
    "version": "bench-soak-2026-05-27",
    "tiers": {
        "1": {"label": "Short", "range": "< 25 min"},
        "2": {"label": "Medium", "range": "25-60 min"},
        "3": {"label": "Long", "range": "1-3 h"},
        "4": {"label": "Extra-long", "range": "> 3 h"},
    },
    "files": [
        {"uuid": u["uuid"], "filename": u["filename"],
         "duration_s": u["duration_s"], "size_mb": u["size_mb"],
         "wall_s": u.get("wall_s"), "tier": tier(u["duration_s"])}
        for u in uploaded
    ],
}
by_idx = list(range(len(corpus["files"])))
tier_buckets: dict[int, list[int]] = {1: [], 2: [], 3: [], 4: []}
for i, f in enumerate(corpus["files"]):
    tier_buckets[f["tier"]].append(i)
mixed = []
while any(tier_buckets.values()):
    for t in (1, 2, 3, 4):
        if tier_buckets[t]:
            mixed.append(tier_buckets[t].pop(0))
corpus["profiles"] = {
    "by_duration": {"description": "Duration-ascending - VRAM ceiling tests fail fast.",
                    "indices": by_idx},
    "mixed": {"description": "Tier round-robin - realistic scheduler stress.",
              "indices": mixed},
}
Path("docs/benchmark-corpus").mkdir(parents=True, exist_ok=True)
Path("docs/benchmark-corpus/corpus.json").write_text(json.dumps(corpus, indent=2))
print(f"\nWrote corpus.json with {len(uploaded)} files "
      f"({sum(u['duration_s'] for u in uploaded)/3600:.2f} h total audio).", flush=True)
