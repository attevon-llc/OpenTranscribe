"""CLI: seed the #1337 synthetic dataset into an isolated Postgres.

Refuses unless the target is declared isolated (``--fresh NAME`` or ``--throwaway-db``) AND does
not resolve to the shared dev stack's ports. There is deliberately no override flag.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import fields

from app.scripts.corpus_injection import env as inj_env


class NotIsolatedError(RuntimeError):
    """The target was not declared a throwaway or ``--fresh`` database."""


def require_isolated_target(fresh: str | None, throwaway: bool) -> str:
    """Return the declared target's label, or raise unless exactly one isolation declaration is given and the port is not the dev stack's."""
    if bool(fresh) == bool(throwaway):
        raise NotIsolatedError(
            "Declare the target: exactly one of --fresh NAME or --throwaway-db. "
            "Seeding a database that is not explicitly isolated is refused."
        )
    inj_env.guard_live_stack(allow=False)
    return f"fresh:{fresh}" if fresh else "throwaway-db"


def build_parser() -> argparse.ArgumentParser:
    from app.scripts.scale_dataset import pg_synthetic as pg

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", default="perf1337", help="seed id, stored on every file")
    parser.add_argument("--fresh", metavar="NAME", help="a ./opentr.sh --fresh deployment")
    parser.add_argument(
        "--throwaway-db", action="store_true", help="a run-backend-tests throwaway DB"
    )
    for f in fields(pg.Shape):
        parser.add_argument(
            "--" + f.name.replace("_", "-"), type=int, default=f.default, dest=f.name
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    inj_env.bootstrap()  # before anything imports app.core.config
    from app.scripts.scale_dataset import pg_synthetic as pg
    from app.scripts.scale_dataset.pg_governance import seed_all

    args = build_parser().parse_args(argv)
    require_isolated_target(args.fresh, args.throwaway_db)
    shape = pg.Shape(**{f.name: getattr(args, f.name) for f in fields(pg.Shape)})

    from sqlalchemy import create_engine
    from sqlalchemy import text

    from app.core.config import settings
    from app.core.security import get_password_hash

    print(f"Target: {inj_env.describe_target()['postgres']}  seed={args.seed}  shape={shape}")
    engine = create_engine(settings.DATABASE_URL)
    started = time.monotonic()
    with engine.begin() as conn:
        counts = seed_all(conn, shape, args.seed, get_password_hash("loadtest-password"))
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for table in pg.ANALYZE_TABLES:
            conn.execute(text(f'ANALYZE "{table}"'))
    print(f"Seeded in {time.monotonic() - started:.1f}s: {counts}")
    print(
        "Identities: perf-1@loadtest.invalid (admin), perf-2@... (heavy), password loadtest-password"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
