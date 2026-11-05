#!/usr/bin/env python3
"""Compare a file's transcript segments before and after an upgrade or rollback.

Usage: transcript-compare.py {prefix|exact} PRE_JSON POST_JSON

Both files hold the API's segment listing (``segments`` or ``transcript_segments``).
``prefix``: every pre-upgrade segment is still there, in order, with the same text and start
time (later segments may be added). ``exact``: the segment count matches too. Prints
``pre=N post=M`` for the rehearsal log; exit 0 when the check holds, 1 when it does not or
either file is unreadable, 2 on misuse.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

START_TOLERANCE_S = 0.01


def load_segments(path: Path) -> list[dict[str, Any]] | None:
    """The segment list in *path*, or None when the file is missing or not JSON."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data.get('segments') or data.get('transcript_segments') or []


def segments_match(pre: list[dict[str, Any]], post: list[dict[str, Any]], *, exact: bool) -> bool:
    """True when *post* keeps every segment of *pre* (and, if *exact*, adds none)."""
    if len(post) < len(pre) or (exact and len(post) != len(pre)):
        return False
    return all(
        a.get('text') == b.get('text')
        and abs((a.get('start') or 0) - (b.get('start') or 0)) <= START_TOLERANCE_S
        for a, b in zip(pre, post[: len(pre)], strict=True)
    )


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[1] not in ('prefix', 'exact'):
        print(__doc__, file=sys.stderr)
        return 2
    pre = load_segments(Path(argv[2]))
    post = load_segments(Path(argv[3]))
    print(f'pre={len(pre or [])} post={len(post or [])}')
    if pre is None or post is None:
        return 1
    return 0 if segments_match(pre, post, exact=argv[1] == 'exact') else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
