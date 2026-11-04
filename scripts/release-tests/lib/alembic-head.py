#!/usr/bin/env python3
"""Derive the single Alembic head from a migration directory, using only stdlib.

Why not import alembic? This must run against an arbitrary git worktree checked
out at an old release tag, where no venv exists and the revision files may import
modules that are not installed. Parsing the two module-level assignments is
sufficient and cannot fail for those reasons.

Why not `grep '^revision' | tail -1`? That is what the release scenarios used to
do, and it sorts by *filename*. It only ever worked by luck of 3-digit
zero-padded ids, and the chain is already non-contiguous (v130 -> v071,
v073 -> v140, two v270* files, v375-v381 renumbered to v377-v383). A 4-digit id
or a second head would silently produce the wrong answer.

Usage:
    alembic-head.py <backend_dir>          # prints the head revision id
    alembic-head.py <backend_dir> --json   # full graph report
    alembic-head.py <backend_dir> --has-revision v448
        # exit 0 when that revision (id, or the id's prefix before the first `_`) is the head or an
        # ancestor of it; 1 when it is not; 2 on a malformed chain. Used to ask "did the FROM
        # release already ship the migration this check exercises?"

Exits non-zero when the chain is not a single clean line, printing every problem.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Matches:  revision = "v384_x"   |   revision: str = 'v384_x'
_REVISION_RE = re.compile(r"^revision\s*(?::\s*[^=]+)?=\s*[\"']([^\"']+)[\"']", re.MULTILINE)
# Matches down_revision as a string or as None (the base revision).
_DOWN_RE = re.compile(
    r"^down_revision\s*(?::\s*[^=]+)?=\s*(?:[\"']([^\"']+)[\"']|(None))", re.MULTILINE
)


def parse_versions(versions_dir: Path) -> tuple[dict[str, str | None], list[str]]:
    """Return ({revision: down_revision}, [problems])."""
    graph: dict[str, str | None] = {}
    problems: list[str] = []

    files = sorted(p for p in versions_dir.glob('*.py') if p.name != '__init__.py')
    if not files:
        problems.append(f'no revision files found in {versions_dir}')
        return graph, problems

    for path in files:
        text = path.read_text(encoding='utf-8')
        rev_match = _REVISION_RE.search(text)
        if not rev_match:
            problems.append(f'{path.name}: no module-level `revision = ...` found')
            continue
        revision = rev_match.group(1)

        down_match = _DOWN_RE.search(text)
        if not down_match:
            problems.append(f'{path.name}: no module-level `down_revision = ...` found')
            continue
        down = down_match.group(1) if down_match.group(1) else None

        if revision in graph:
            problems.append(f'duplicate revision id {revision!r} (second in {path.name})')
        graph[revision] = down

        # Repo convention: filename stem is the revision id. A mismatch means a
        # file was renamed without updating the id (or vice versa), which makes
        # the chain very hard to reason about.
        if path.stem != revision:
            problems.append(f'{path.name}: filename stem {path.stem!r} != revision id {revision!r}')

    for revision, down in graph.items():
        if down is not None and down not in graph:
            problems.append(f'{revision}: down_revision {down!r} does not resolve to any revision')

    return graph, problems


def find_heads(graph: dict[str, str | None]) -> list[str]:
    """Heads are revisions that no other revision points down at."""
    referenced = {down for down in graph.values() if down is not None}
    return sorted(rev for rev in graph if rev not in referenced)


def find_bases(graph: dict[str, str | None]) -> list[str]:
    return sorted(rev for rev, down in graph.items() if down is None)


def reachable_from_head(graph: dict[str, str | None], head: str) -> set[str]:
    """The head and every revision below it."""
    seen: set[str] = set()
    cur: str | None = head
    while cur is not None and cur not in seen:
        seen.add(cur)
        cur = graph.get(cur)
    return seen


def has_revision(graph: dict[str, str | None], wanted: str) -> bool:
    """Whether *wanted* (a full id, or the part of an id before its first ``_``) is in the chain."""
    heads = find_heads(graph)
    if len(heads) != 1:
        raise ValueError(f'expected exactly 1 head, found {heads}')
    chain = reachable_from_head(graph, heads[0])
    return any(rev == wanted or rev.split('_', 1)[0] == wanted for rev in chain)


def main(argv: list[str]) -> int:
    rest = argv[1:]
    wanted: str | None = None
    if '--has-revision' in rest:
        i = rest.index('--has-revision')
        if i + 1 >= len(rest):
            print(__doc__, file=sys.stderr)
            return 2
        wanted = rest[i + 1]
        del rest[i : i + 2]
    args = [a for a in rest if not a.startswith('--')]
    as_json = '--json' in rest

    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2

    backend_dir = Path(args[0]).resolve()
    versions_dir = backend_dir / 'alembic' / 'versions'
    if not versions_dir.is_dir():
        # Tolerate being handed the versions dir or the alembic dir directly.
        for candidate in (backend_dir / 'versions', backend_dir):
            if (candidate / '__init__.py').exists() or list(candidate.glob('v*.py')):
                versions_dir = candidate
                break
        else:
            print(f'ERROR: no alembic/versions under {backend_dir}', file=sys.stderr)
            return 1

    graph, problems = parse_versions(versions_dir)
    if wanted is not None:
        if problems:
            for problem in problems:
                print(f'ERROR: {problem}', file=sys.stderr)
            return 2
        try:
            return 0 if has_revision(graph, wanted) else 1
        except ValueError as err:
            print(f'ERROR: {err}', file=sys.stderr)
            return 2
    heads = find_heads(graph)
    bases = find_bases(graph)

    if len(heads) != 1:
        problems.append(f'expected exactly 1 head, found {len(heads)}: {heads}')
    if len(bases) != 1:
        problems.append(f'expected exactly 1 base, found {len(bases)}: {bases}')

    if as_json:
        print(
            json.dumps(
                {
                    'versions_dir': str(versions_dir),
                    'revision_count': len(graph),
                    'head': heads[0] if len(heads) == 1 else None,
                    'heads': heads,
                    'bases': bases,
                    'problems': problems,
                    'ok': not problems,
                },
                indent=2,
            )
        )
    else:
        for problem in problems:
            print(f'ERROR: {problem}', file=sys.stderr)
        if len(heads) == 1:
            print(heads[0])

    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
