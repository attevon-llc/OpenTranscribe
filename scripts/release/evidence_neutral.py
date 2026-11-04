#!/usr/bin/env python3
"""Which changed paths can NOT invalidate a release stage's evidence.

The policy lives in release-criteria.yaml under `evidence_neutral_paths:` (stage -> globs).
70-tag.sh pipes `git diff --name-only <evidence-sha> HEAD` in on stdin.

Usage:  evidence_neutral.py <stage> [--criteria PATH] < paths
Exit:   0 every path is neutral for <stage>; 1 prints the FIRST offending path; 2 misuse.

Glob rules, deliberately tiny: `dir/**` matches anything under `dir/`; any other pattern
is an fnmatch pattern that must match the whole path and cannot cross a `/` (so `*.md`
is repo-root markdown only, never `backend/README.md`).
"""

from __future__ import annotations

import argparse
import fnmatch
import sys
from pathlib import Path

import yaml

DEFAULT_CRITERIA = Path(__file__).resolve().parent / 'release-criteria.yaml'


def matches(pattern: str, path: str) -> bool:
    if pattern.endswith('/**'):
        return path.startswith(pattern[:-2])
    return path.count('/') == pattern.count('/') and fnmatch.fnmatchcase(path, pattern)


def load_policy(criteria: Path) -> dict[str, list[str]]:
    doc = yaml.safe_load(criteria.read_text(encoding='utf-8'))
    policy: dict[str, list[str]] = doc['evidence_neutral_paths']
    return policy


def first_offender(stage: str, paths: list[str], policy: dict[str, list[str]]) -> str | None:
    globs = policy[stage]
    for path in paths:
        if not any(matches(g, path) for g in globs):
            return path
    return None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage')
    parser.add_argument('--criteria', type=Path, default=DEFAULT_CRITERIA)
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.criteria)
        offender = first_offender(
            args.stage, [p for p in sys.stdin.read().splitlines() if p], policy
        )
    except (KeyError, OSError, yaml.YAMLError) as exc:
        sys.stderr.write(f"evidence_neutral: cannot read policy for '{args.stage}': {exc!r}\n")
        return 2
    if offender is None:
        return 0
    print(offender)
    return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
