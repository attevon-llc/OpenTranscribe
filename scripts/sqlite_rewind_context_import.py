#!/usr/bin/env python3
"""Import Rewind OCR/screen/activity context payloads into SQLite FTS."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("payload", nargs="+", type=Path)
    parser.add_argument("--user-id", type=int)
    parser.add_argument("--sqlite-path", default="~/.opentranscribe/search/search.sqlite3")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    from app.services.sqlite_search.context_import import import_context_payloads

    report = import_context_payloads(args.sqlite_path, args.payload, user_id=args.user_id)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
