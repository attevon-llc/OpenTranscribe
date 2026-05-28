#!/usr/bin/env python3
"""Seed `benchmark/test_audio/` with a tier-distributed subset of production audio.

Pulls original audio files from a running OpenTranscribe instance via the API
and writes them to `benchmark/test_audio/` so the bench-stack soak has a
representative corpus.

Run this ONCE while your production / dev stack is up (so the API answers on
port 5174). Then stop production, run `./opentr.sh bench start current` to
bring up the bench stack, and start the soak — the bench stack mounts
`benchmark/test_audio/` read-only and will see the new files.

Usage:
    source backend/venv/bin/activate
    BENCHMARK_EMAIL=admin@example.com BENCHMARK_PASSWORD=password \\
        python scripts/soak_seed_audio.py --total 20

    # Per-tier counts (defaults: 5 short / 5 medium / 5 long / 5 extra-long):
    python scripts/soak_seed_audio.py --tier1 6 --tier2 6 --tier3 4 --tier4 4

    # Different backend URL / preserve existing seed files (default keeps them):
    python scripts/soak_seed_audio.py --backend-url http://localhost:5174

Files are saved with the pattern:
    benchmark/test_audio/seed_<tier>_<duration>s_<uuid8>.<ext>
so they sort by tier and are easily identifiable.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests

DEFAULT_BACKEND = os.environ.get("BENCHMARK_BACKEND_URL", "http://localhost:5174")
DEFAULT_EMAIL = os.environ.get("BENCHMARK_EMAIL", "admin@example.com")
DEFAULT_PASSWORD = os.environ.get("BENCHMARK_PASSWORD", "password")
AUDIO_DIR = Path("benchmark/test_audio")

# Tier boundaries (seconds)
TIER_BOUNDS = {
    1: (300, 25 * 60),       # 5-25 min
    2: (25 * 60, 60 * 60),   # 25-60 min
    3: (60 * 60, 180 * 60),  # 1-3 h
    4: (180 * 60, 24 * 3600),  # 3 h+
}


def authenticate(backend_url: str, email: str, password: str) -> str:
    r = requests.post(
        f"{backend_url}/api/auth/token",
        data={"username": email, "password": password},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def db_query(sql: str, container: str = "opentranscribe-postgres") -> list[list[str]]:
    """Run a SQL SELECT via docker exec (faster + no auth needed for a list)."""
    result = subprocess.run(
        [
            "docker", "exec", container, "psql", "-U", "postgres",
            "-d", "opentranscribe", "-t", "-A", "-F", "\t", "-c", sql,
        ],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"DB query failed: {result.stderr.strip()}")
    return [
        line.split("\t")
        for line in result.stdout.strip().splitlines()
        if line.strip()
    ]


def pick_files_for_tier(tier: int, count: int, exclude_uuids: set[str]) -> list[dict[str, Any]]:
    """Pick `count` random completed files in the duration range for this tier."""
    lo, hi = TIER_BOUNDS[tier]
    excl = " AND uuid NOT IN ('" + "','".join(exclude_uuids) + "')" if exclude_uuids else ""
    sql = (
        f"SELECT uuid, filename, duration, file_size, content_type "
        f"FROM media_file "
        f"WHERE status='completed' AND duration BETWEEN {lo} AND {hi} "
        f"AND file_size > 0{excl} "
        f"ORDER BY RANDOM() LIMIT {count};"
    )
    rows = db_query(sql)
    out = []
    for r in rows:
        out.append({
            "uuid": r[0].strip(),
            "filename": r[1].strip(),
            "duration": float(r[2].strip()),
            "size": int(r[3].strip()),
            "content_type": r[4].strip() if len(r) > 4 else "",
        })
    return out


def sanitize(name: str) -> str:
    name = re.sub(r"[^\w\-.]+", "_", name)
    return name[:80]


# Extensions the bench uploader (scripts soak Phase 0.5) will accept.
KNOWN_MEDIA_EXTS = {"wav", "mp3", "m4a", "mp4", "flac"}

# content_type → extension fallback when the filename carries no usable suffix.
CONTENT_TYPE_EXT = {
    "video/mp4": "mp4",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
}


def resolve_ext(fname: str, content_type: str = "", default: str = "mp4") -> str:
    """Pick a known media extension: filename suffix → content_type → default.

    yt-dlp titles frequently carry no suffix, so falling back to content_type
    (and finally `default`) guarantees the saved file has an extension the
    bench uploader's EXTS filter accepts — otherwise it gets silently skipped.
    """
    suffix = Path(fname).suffix.lstrip(".").lower()
    if suffix in KNOWN_MEDIA_EXTS:
        return suffix
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in CONTENT_TYPE_EXT:
        return CONTENT_TYPE_EXT[ct]
    return default


def download_one(backend_url: str, token: str, f: dict[str, Any], dest_dir: Path, tier: int) -> Path:
    uuid = f["uuid"]
    fname = sanitize(f["filename"])
    ext = resolve_ext(fname, f.get("content_type", ""))
    dur = int(f["duration"])
    # Strip any redundant suffix off the title before appending the canonical
    # extension, so we never produce names like "foo.mp4.mp4" or extension-less files.
    stem = fname
    if Path(fname).suffix.lstrip(".").lower() in KNOWN_MEDIA_EXTS:
        stem = fname[: -(len(Path(fname).suffix))]
    dest = dest_dir / f"seed_t{tier}_{dur}s_{uuid[:8]}_{stem}.{ext}"
    # Don't re-download if already present (allows safe Ctrl-C / restart).
    if dest.exists() and dest.stat().st_size > 0:
        print(f"    [skip — already present] {dest.name} ({dest.stat().st_size/1e6:.1f} MB)")
        return dest
    url = f"{backend_url}/api/files/{uuid}/download?original=true"
    # 4 MB chunks — larger than default 1 MB; bigger sustained-throughput.
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, stream=True, timeout=600)
    r.raise_for_status()
    # String-append the .part suffix (Path.with_suffix mangles names with dots).
    tmp = dest.parent / (dest.name + ".part")
    with open(tmp, "wb") as fh:
        for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
            if chunk:
                fh.write(chunk)
    tmp.rename(dest)
    print(f"    [done] {dest.name} ({dest.stat().st_size/1e6:.1f} MB)")
    return dest


def download_many(backend_url: str, token: str, picks: list[tuple[int, dict[str, Any]]],
                  dest_dir: Path, workers: int) -> None:
    """Download (tier, file_record) pairs in parallel via ThreadPoolExecutor."""
    if workers <= 1:
        for tier, f in picks:
            download_one(backend_url, token, f, dest_dir, tier)
        return
    print(f"\nDownloading {len(picks)} files with {workers} parallel workers ...")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(download_one, backend_url, token, f, dest_dir, tier): (tier, f)
            for tier, f in picks
        }
        done = 0
        for fut in as_completed(futures):
            tier, f = futures[fut]
            done += 1
            try:
                fut.result()
            except requests.RequestException as e:
                print(f"    [FAIL {done}/{len(picks)}] tier {tier} {f['uuid'][:8]}: {e}",
                      file=sys.stderr)
            except Exception as e:  # noqa: BLE001
                print(f"    [FAIL {done}/{len(picks)}] tier {tier} {f['uuid'][:8]}: {type(e).__name__}: {e}",
                      file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed bench audio corpus from production")
    ap.add_argument("--backend-url", default=DEFAULT_BACKEND)
    ap.add_argument("--total", type=int, default=20,
                    help="Total files (split evenly across 4 tiers if --tier* not given)")
    ap.add_argument("--tier1", type=int, default=None, help="Number of <25 min files")
    ap.add_argument("--tier2", type=int, default=None, help="Number of 25-60 min files")
    ap.add_argument("--tier3", type=int, default=None, help="Number of 1-3 h files")
    ap.add_argument("--tier4", type=int, default=None, help="Number of >3 h files")
    ap.add_argument("--workers", type=int, default=4,
                    help="Parallel download workers (default: 4). Set to 1 for sequential. "
                         "Each worker holds one HTTP stream to the backend; the practical limit "
                         "is your NAS read parallelism — 4-6 is usually the sweet spot.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print picks without downloading")
    args = ap.parse_args()

    if any(x is None for x in (args.tier1, args.tier2, args.tier3, args.tier4)):
        per_tier = args.total // 4
        rem = args.total - per_tier * 4
        args.tier1 = args.tier1 if args.tier1 is not None else per_tier + (1 if rem > 0 else 0)
        args.tier2 = args.tier2 if args.tier2 is not None else per_tier + (1 if rem > 1 else 0)
        args.tier3 = args.tier3 if args.tier3 is not None else per_tier + (1 if rem > 2 else 0)
        args.tier4 = args.tier4 if args.tier4 is not None else per_tier

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Seeding {AUDIO_DIR} from {args.backend_url}")
    print(f"Target distribution: T1={args.tier1}  T2={args.tier2}  T3={args.tier3}  T4={args.tier4}\n")

    if not args.dry_run:
        try:
            token = authenticate(args.backend_url, DEFAULT_EMAIL, DEFAULT_PASSWORD)
        except requests.RequestException as e:
            print(f"ERROR: auth failed against {args.backend_url} — is the prod stack running? ({e})",
                  file=sys.stderr)
            return 1
    else:
        token = ""

    excluded: set[str] = set()
    summary: dict[int, int] = {}
    all_picks: list[tuple[int, dict[str, Any]]] = []  # (tier, file_record)
    for tier, count in [(1, args.tier1), (2, args.tier2), (3, args.tier3), (4, args.tier4)]:
        if count <= 0:
            continue
        print(f"Tier {tier} — picking {count} file(s)...")
        picks = pick_files_for_tier(tier, count, excluded)
        if len(picks) < count:
            print(f"  WARNING: only {len(picks)} candidates in this tier (need {count})")
        for f in picks:
            excluded.add(f["uuid"])
            mins = f["duration"] / 60
            print(f"  picked: {f['uuid'][:8]} {f['filename'][:50]} ({mins:.1f} min)")
            all_picks.append((tier, f))
        summary[tier] = len(picks)

    if not args.dry_run and all_picks:
        download_many(args.backend_url, token, all_picks, AUDIO_DIR, args.workers)

    # Free-space check
    free = shutil.disk_usage(AUDIO_DIR).free
    total_in = sum(p.stat().st_size for p in AUDIO_DIR.iterdir() if p.is_file())
    print(f"\nDone. Tier counts: {summary}")
    print(f"benchmark/test_audio/ now contains {sum(1 for _ in AUDIO_DIR.iterdir())} files, "
          f"{total_in/1e9:.1f} GB total. Free disk: {free/1e9:.0f} GB.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
