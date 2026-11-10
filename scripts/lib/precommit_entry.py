#!/usr/bin/env python3
"""Print the ``entry`` of local pre-commit hooks, so other tools run the exact hook command.

``scripts/lane-check.sh`` uses this instead of copying hook commands: a copied command drifts the
moment someone edits the hook, and the drift is silent (the copy keeps passing while the hook
checks something else). A ``language: python`` hook's entry starts with ``python``, which
pre-commit resolves inside the hook's own environment; ``--python`` substitutes the interpreter
that holds the same pinned tools (the guards themselves refuse a different version).

Usage::

    precommit_entry.py [--config FILE] [--python EXE] HOOK_ID [HOOK_ID ...]

Prints ``HOOK_ID<TAB>ENTRY`` per hook, in argument order. An unknown id, a non-local hook, or a
hook whose files are passed to it (``pass_filenames`` not false) exits 2: a whole-tree runner
cannot reproduce what such a hook checks.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


class EntryError(Exception):
    """The requested hook cannot be run as a whole-tree command."""


def local_hooks(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map hook id (or alias) to its definition, for ``repo: local`` hooks only."""
    hooks: dict[str, dict[str, Any]] = {}
    for repo in config.get('repos') or []:
        if repo.get('repo') != 'local':
            continue
        for hook in repo.get('hooks') or []:
            hook_id = str(hook.get('alias') or hook['id'])
            if hook_id in hooks:
                msg = f'hook id {hook_id!r} is defined twice; give one an alias'
                raise EntryError(msg)
            hooks[hook_id] = hook
    return hooks


def resolve(config: dict[str, Any], hook_id: str, python: str | None = None) -> str:
    """The shell command that runs ``hook_id`` over the whole tree."""
    hooks = local_hooks(config)
    if hook_id not in hooks:
        msg = f'no local hook {hook_id!r} in the pre-commit config'
        raise EntryError(msg)
    hook = hooks[hook_id]
    if hook.get('pass_filenames', True) is not False:
        msg = f'hook {hook_id!r} is handed filenames; it has no whole-tree command'
        raise EntryError(msg)
    entry = str(hook['entry'])
    if hook.get('language') == 'python' and python:
        head, _, rest = entry.partition(' ')
        if head not in {'python', 'python3'}:
            msg = f'python hook {hook_id!r} entry does not start with python: {entry!r}'
            raise EntryError(msg)
        entry = f'{python} {rest}'
    return entry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n', 1)[0])
    parser.add_argument('--config', type=Path, default=REPO_ROOT / '.pre-commit-config.yaml')
    parser.add_argument('--python', help='interpreter for language: python hooks')
    parser.add_argument('hook_ids', nargs='+')
    args = parser.parse_args(argv)
    try:
        config = yaml.safe_load(args.config.read_text(encoding='utf-8'))
        for hook_id in args.hook_ids:
            print(f'{hook_id}\t{resolve(config, hook_id, args.python)}')
    except (EntryError, OSError, yaml.YAMLError, KeyError) as exc:
        print(f'precommit_entry: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
