#!/usr/bin/env python3
"""Read a single value out of a .env file using python-dotenv (issue #590).

Every hand-rolled ``grep ... | cut -d= -f2- | tr -d '"'`` pipeline in this repo's bash
scripts had the same latent bug: none of them strip a dotenv-style trailing
``  # comment`` before the value, so a perfectly normal line like
``FLOWER_PORT=5175  # Celery Task Monitor`` parses to ``5175#CeleryTaskMonitor``. That
exact corruption broke ``gpu-scale-smoke.sh`` and ``diar-native-smoke.sh`` live (issue
#590) and, independently, a dozen reads in ``opentranscribe.sh`` (fixed separately with
its own bash-only helper in ``scripts/common.sh::read_env_value`` -- that one has to stay
bash because ``opentranscribe.sh`` ships standalone to end users with no guarantee
python-dotenv is installed).

This is the python-dotenv-backed replacement for bash callers that already require
python3 (they all do -- every offending script already shells out to ``python3 -c`` for
JSON handling). ``dotenv_values()`` implements the real dotenv grammar: quoted values,
inline comments, escaping, multi-line values -- none of which a grep/cut/tr chain gets
right in the first place.

Usage:
    python3 scripts/lib/env_reader.py <path-to-env-file> <KEY> [--default DEFAULT]

Prints the value (or DEFAULT, or nothing) to stdout with no trailing newline surprises,
and exits 0 whether or not the key was found -- an absent optional key is the normal
case for every caller here, matching the ``|| true`` contract the bash callers already
relied on. Exits 2 only on genuine misuse (wrong argument count).
"""

from __future__ import annotations

import sys

from dotenv import dotenv_values


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 4):
        print(
            'usage: env_reader.py <env-file> <KEY> [--default DEFAULT]',
            file=sys.stderr,
        )
        return 2

    env_file, key = argv[0], argv[1]
    default = None
    if len(argv) == 4:
        if argv[2] != '--default':
            print(
                'usage: env_reader.py <env-file> <KEY> [--default DEFAULT]',
                file=sys.stderr,
            )
            return 2
        default = argv[3]

    values = dotenv_values(env_file)
    value = values.get(key)
    if value is None:
        value = default
    if value is not None:
        print(value)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
