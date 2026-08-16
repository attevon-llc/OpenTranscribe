"""Command-line entry point for the synthetic corpus generator.

```bash
cd backend
python3 -m tests.eval.synthetic generate --out /mnt/nas/.../otsynth-core-v1 --meetings 2000
python3 -m tests.eval.synthetic validate  <dir>
python3 -m tests.eval.synthetic measure   <dir>
python3 -m tests.eval.synthetic verify    <dir>      # sha256sum -c, in-process
```

``generate`` validates by default; ``--no-validate`` is available for a fast smoke run but
is never what a published corpus should be built with.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

from .corpus import build_corpus
from .corpus import default_config
from .manifest import load_config
from .manifest import write_manifest
from .manifest import write_readme
from .measure import measure_corpus
from .measure import write_metrics
from .validate import format_report
from .validate import validate_corpus


def verify_checksums(corpus_dir: Path) -> list[str]:
    """Return the list of files whose digest does not match ``SHA256SUMS``."""
    corpus_dir = Path(corpus_dir)
    bad: list[str] = []
    for line in (corpus_dir / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, rel = line.split("  ", 1)
        path = corpus_dir / rel
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            bad.append(rel)
    return bad


def _cmd_generate(args: argparse.Namespace) -> int:
    overrides = {
        "meetings": args.meetings,
        "seed": args.seed,
        "corpus_id": args.corpus_id,
        "near_duplicate_rate": args.near_duplicate_rate,
        "shard_size": args.shard_size,
        "meetings_per_team": args.meetings_per_team,
    }
    config = default_config(**{k: v for k, v in overrides.items() if v is not None})
    out = Path(args.out)
    started = time.time()
    stats = build_corpus(config, out)
    print(
        f"generated {stats['meetings']:,} meetings / {stats['total_words']:,} words "
        f"in {time.time() - started:.1f}s -> {out}"
    )
    metrics = None
    if not args.no_validate:
        report = validate_corpus(out)
        print(format_report(report))
        if not report.ok:
            return 1
    if args.measure:
        metrics = measure_corpus(out, limit=args.measure_limit)
        write_metrics(out, metrics)
        print(json.dumps(metrics["overall"], indent=2))
    write_readme(out, config, stats, metrics)
    write_manifest(out, config)
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    report = validate_corpus(Path(args.corpus_dir))
    print(format_report(report))
    return 0 if report.ok else 1


def _cmd_measure(args: argparse.Namespace) -> int:
    metrics = measure_corpus(Path(args.corpus_dir), limit=args.limit)
    write_metrics(Path(args.corpus_dir), metrics)
    config = load_config(Path(args.corpus_dir))
    stats = json.loads((Path(args.corpus_dir) / "stats.json").read_text(encoding="utf-8"))
    write_readme(Path(args.corpus_dir), config, stats, metrics)
    print(json.dumps(metrics, indent=2))
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    bad = verify_checksums(Path(args.corpus_dir))
    if bad:
        print(f"MISMATCH: {len(bad)} file(s)")
        for rel in bad[:20]:
            print(f"  {rel}")
        return 1
    print("OK — every file matches SHA256SUMS")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser (exposed so tests can exercise it directly)."""
    parser = argparse.ArgumentParser(prog="tests.eval.synthetic", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="generate a corpus")
    gen.add_argument("--out", required=True)
    gen.add_argument("--meetings", type=int)
    gen.add_argument("--seed", type=int)
    gen.add_argument("--corpus-id")
    gen.add_argument("--near-duplicate-rate", type=float)
    gen.add_argument("--shard-size", type=int)
    gen.add_argument("--meetings-per-team", type=int)
    gen.add_argument("--no-validate", action="store_true")
    gen.add_argument("--measure", action="store_true")
    gen.add_argument("--measure-limit", type=int, default=None)
    gen.set_defaults(func=_cmd_generate)

    val = sub.add_parser("validate", help="re-derive every ground truth from the text")
    val.add_argument("corpus_dir")
    val.set_defaults(func=_cmd_validate)

    mea = sub.add_parser("measure", help="BM25 difficulty + near-duplicate structure")
    mea.add_argument("corpus_dir")
    mea.add_argument("--limit", type=int, default=None)
    mea.set_defaults(func=_cmd_measure)

    ver = sub.add_parser("verify", help="check the corpus against SHA256SUMS")
    ver.add_argument("corpus_dir")
    ver.set_defaults(func=_cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch."""
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
