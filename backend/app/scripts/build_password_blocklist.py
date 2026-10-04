"""Build the offline password blocklist from Have I Been Pwned "Pwned Passwords".

Walks every 5-hex-character range of the official k-anonymity API
(``https://api.pwnedpasswords.com/range/{prefix}``, 16**5 = 1,048,576 requests), keeps the
SHA-1 hashes with the highest breach counts and writes them, uppercase hex, one per line,
most common first. No plaintext password is ever requested, stored or written.

Data source and terms: Troy Hunt's Pwned Passwords. The service documentation states
"no licensing or attribution requirement" (https://haveibeenpwned.com/API/V3 and
https://haveibeenpwned.com/Passwords). Nothing from it is committed to this repository;
each operator downloads it on their own host.

Run it with ``./opentranscribe.sh download-models password-blocklist`` (wraps this module
in the backend image), or directly::

    python -m app.scripts.build_password_blocklist --out /app/models/password-blocklist/pwned-passwords-top100k-sha1.txt

The walk is resumable: results are cached per 3-hex shard (256 ranges) in ``--cache-dir``
(default: ``.range-cache`` beside the output) and finished shards are skipped on the next
run. Only entries with a count of at least ``--min-count`` are cached, which keeps the cache
small. Expect a few hours and roughly 35 GB of transfer. Concurrency is modest by default and
429 / 5xx replies are retried with backoff (honouring ``Retry-After``).

A ``<out>.meta.json`` sidecar records the retrieval date and the count range.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import sys
from collections.abc import Iterable
from datetime import UTC
from datetime import datetime
from pathlib import Path

import httpx

logger = logging.getLogger("build_password_blocklist")

API_URL = "https://api.pwnedpasswords.com/range"
SHARD_HEX_LEN = 3  # 4096 shards of 256 ranges each
PREFIX_HEX_LEN = 5
_SUFFIX_LEN = 40 - PREFIX_HEX_LEN
_HEX_SUFFIX = re.compile(rf"^[0-9A-F]{{{_SUFFIX_LEN}}}$")
MAX_ATTEMPTS = 8


def parse_range_body(prefix: str, body: str, min_count: int) -> list[tuple[str, int]]:
    """Return ``(full_sha1, count)`` for every line of a range reply with count >= min_count.

    Padding entries (count 0) and malformed lines are dropped.
    """
    entries: list[tuple[str, int]] = []
    for line in body.splitlines():
        suffix, _, raw_count = line.strip().partition(":")
        suffix = suffix.upper()
        if not _HEX_SUFFIX.match(suffix):
            continue
        try:
            count = int(raw_count)
        except ValueError:
            continue
        if count >= min_count:
            entries.append((prefix + suffix, count))
    return entries


def select_top(entries: Iterable[tuple[str, int]], top: int) -> list[tuple[str, int]]:
    """Highest counts first; ties broken by hash so the output is deterministic."""
    return sorted(entries, key=lambda item: (-item[1], item[0]))[:top]


async def fetch_range(client: httpx.AsyncClient, prefix: str, base_url: str) -> str:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = await client.get(f"{base_url}/{prefix}", headers={"Add-Padding": "false"})
        except httpx.HTTPError as exc:
            delay = min(60.0, 2.0**attempt) + random.random()  # noqa: S311  # nosec B311
            logger.warning(
                "range %s attempt %d failed (%s); retry in %.0fs", prefix, attempt, exc, delay
            )
            await asyncio.sleep(delay)
            continue
        if response.status_code == 200:
            return str(response.text)
        if response.status_code in (429, 500, 502, 503, 504):
            retry_after = response.headers.get("Retry-After", "")
            delay = float(retry_after) if retry_after.isdigit() else min(60.0, 2.0**attempt)
            logger.warning("range %s HTTP %d; retry in %.0fs", prefix, response.status_code, delay)
            await asyncio.sleep(delay + random.random())  # noqa: S311  # nosec B311
            continue
        response.raise_for_status()
    raise RuntimeError(f"range {prefix}: gave up after {MAX_ATTEMPTS} attempts")


async def fetch_shard(
    client: httpx.AsyncClient, shard: str, cache_dir: Path, base_url: str, min_count: int
) -> None:
    target = cache_dir / f"{shard}.txt"
    if target.exists():
        return
    lines: list[str] = []
    for tail in range(16 ** (PREFIX_HEX_LEN - SHARD_HEX_LEN)):
        prefix = shard + format(tail, f"0{PREFIX_HEX_LEN - SHARD_HEX_LEN}X")
        body = await fetch_range(client, prefix, base_url)
        lines.extend(f"{h}:{c}" for h, c in parse_range_body(prefix, body, min_count))
    tmp = target.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="ascii")
    os.replace(tmp, target)  # atomic: a shard file is either complete or absent


def all_shards(limit: int | None = None) -> list[str]:
    shards = [format(i, f"0{SHARD_HEX_LEN}X") for i in range(16**SHARD_HEX_LEN)]
    return shards[:limit] if limit else shards


async def download(
    cache_dir: Path,
    *,
    base_url: str = API_URL,
    min_count: int,
    concurrency: int,
    shard_limit: int | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    shards = all_shards(shard_limit)
    pending = [s for s in shards if not (cache_dir / f"{s}.txt").exists()]
    logger.info("%d of %d shards to fetch", len(pending), len(shards))
    semaphore = asyncio.Semaphore(concurrency)
    done = 0
    headers = {"User-Agent": "OpenTranscribe-build-password-blocklist"}
    async with httpx.AsyncClient(
        timeout=30.0,
        headers=headers,
        transport=transport,
        limits=httpx.Limits(max_connections=concurrency),
    ) as client:

        async def run(shard: str) -> None:
            nonlocal done
            async with semaphore:
                await fetch_shard(client, shard, cache_dir, base_url, min_count)
            done += 1
            if done % 100 == 0:
                logger.info("%d/%d shards done", done, len(pending))

        await asyncio.gather(*(run(s) for s in pending))


def read_cache(cache_dir: Path, shard_limit: int | None = None) -> list[tuple[str, int]]:
    entries: list[tuple[str, int]] = []
    for shard in all_shards(shard_limit):
        path = cache_dir / f"{shard}.txt"
        if not path.exists():
            raise FileNotFoundError(f"shard {shard} missing from cache; download did not finish")
        for line in path.read_text(encoding="ascii").splitlines():
            digest, _, count = line.partition(":")
            entries.append((digest, int(count)))
    return entries


def write_blocklist(selected: list[tuple[str, int]], out: Path) -> None:
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text("".join(f"{digest}\n" for digest, _ in selected), encoding="ascii")
    os.replace(tmp, out)  # a reader never sees a half-written list


def default_out() -> Path:
    models_dir = os.environ.get("MODELS_DIR", "/app/models")
    return Path(models_dir) / "password-blocklist" / "pwned-passwords-top100k-sha1.txt"


def write_metadata(selected: list[tuple[str, int]], out: Path, min_count: int) -> None:
    meta = {
        "source": "Have I Been Pwned - Pwned Passwords (range API)",
        "url": "https://haveibeenpwned.com/Passwords",
        "retrieved": datetime.now(UTC).date().isoformat(),
        "entries": len(selected),
        "highest_count": selected[0][1],
        "lowest_count": selected[-1][1],
        "cache_min_count": min_count,
    }
    out.with_name(out.name + ".meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument(
        "--out", type=Path, default=None, help="default: <MODELS_DIR>/password-blocklist/..."
    )
    parser.add_argument("--top", type=int, default=100_000, help="entries to keep (default 100000)")
    parser.add_argument(
        "--min-count",
        type=int,
        default=2000,
        help="only cache hashes seen at least this often (default 2000); must not exceed the\n"
        "count of the --top'th entry or the result would be incomplete",
    )
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--shard-limit", type=int, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    out: Path = args.out or default_out()
    cache_dir: Path = args.cache_dir or out.parent / ".range-cache"
    out.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(
        download(
            cache_dir,
            min_count=args.min_count,
            concurrency=args.concurrency,
            shard_limit=args.shard_limit,
        )
    )
    entries = read_cache(cache_dir, args.shard_limit)
    selected = select_top(entries, args.top)
    if len(selected) < args.top:
        logger.error(
            "only %d hashes have count >= %d; lower --min-count (and delete the cache)",
            len(selected),
            args.min_count,
        )
        return 1
    write_blocklist(selected, out)
    write_metadata(selected, out, args.min_count)
    logger.info(
        "wrote %d hashes to %s (count range %d .. %d)",
        len(selected),
        out,
        selected[0][1],
        selected[-1][1],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
