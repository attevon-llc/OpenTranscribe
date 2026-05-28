"""Rebuild docs/benchmark-corpus/corpus.json from completed files in the bench DB.

Resume helper: if the upload/wait script (soak_upload_corpus.py) dies before it
writes corpus.json, the files are still in the persistent bench DB and the
celery worker keeps processing them. Run this once all (or enough) files show
status=completed to regenerate corpus.json without re-uploading.

    BENCHMARK_EMAIL=admin@example.com BENCHMARK_PASSWORD=password \\
        backend/venv/bin/python scripts/soak_rebuild_corpus.py
"""
import json
import os
from pathlib import Path

import requests

BACKEND = os.environ.get("BACKEND_URL", "http://localhost:5174")
EMAIL = os.environ.get("BENCHMARK_EMAIL", "admin@example.com")
PASSWORD = os.environ.get("BENCHMARK_PASSWORD", "password")

tok = requests.post(f"{BACKEND}/api/auth/token",
                    data={"username": EMAIL, "password": PASSWORD}, timeout=15)
tok.raise_for_status()
H = {"Authorization": f"Bearer {tok.json()['access_token']}"}

# Pull all files (endpoint paginates: {items, total, page, page_size, has_more}).
items: list[dict] = []
page = 1
while True:
    resp = requests.get(f"{BACKEND}/api/files", headers=H,
                        params={"page": page, "page_size": 100}, timeout=30).json()
    batch = resp["items"] if isinstance(resp, dict) else resp
    items.extend(batch)
    if not isinstance(resp, dict) or not resp.get("has_more"):
        break
    page += 1

completed = [f for f in items if f.get("status") == "completed" and (f.get("duration") or 0) > 0]
print(f"{len(items)} files in DB, {len(completed)} completed with duration.")


def tier(d: float) -> int:
    if d < 25 * 60:
        return 1
    if d < 60 * 60:
        return 2
    if d < 180 * 60:
        return 3
    return 4


completed.sort(key=lambda x: x["duration"])
corpus = {
    "version": "bench-soak-2026-05-27",
    "tiers": {
        "1": {"label": "Short", "range": "< 25 min"},
        "2": {"label": "Medium", "range": "25-60 min"},
        "3": {"label": "Long", "range": "1-3 h"},
        "4": {"label": "Extra-long", "range": "> 3 h"},
    },
    "files": [
        {"uuid": f.get("uuid") or f.get("id"), "filename": f.get("filename", ""),
         "duration_s": float(f["duration"]), "size_mb": int(f.get("file_size", 0)) // (1024 * 1024),
         "tier": tier(float(f["duration"]))}
        for f in completed
    ],
}
by_idx = list(range(len(corpus["files"])))
buckets: dict[int, list[int]] = {1: [], 2: [], 3: [], 4: []}
for i, f in enumerate(corpus["files"]):
    buckets[f["tier"]].append(i)
mixed: list[int] = []
while any(buckets.values()):
    for t in (1, 2, 3, 4):
        if buckets[t]:
            mixed.append(buckets[t].pop(0))
corpus["profiles"] = {
    "by_duration": {"description": "Duration-ascending - VRAM ceiling tests fail fast.",
                    "indices": by_idx},
    "mixed": {"description": "Tier round-robin - realistic scheduler stress.", "indices": mixed},
}
Path("docs/benchmark-corpus").mkdir(parents=True, exist_ok=True)
Path("docs/benchmark-corpus/corpus.json").write_text(json.dumps(corpus, indent=2))
print(f"Wrote corpus.json with {len(completed)} files "
      f"({sum(f['duration'] for f in completed)/3600:.2f} h total audio).")
