#!/usr/bin/env python3
"""List the failed/errored pytest node ids of a junit XML file, one per line.

Usage: junit_failed.py <junit.xml> [<backend-dir>]

The junit XML carries ``classname`` (dotted module path plus optional class) and ``name``;
the node id is rebuilt by finding the longest dotted prefix that is a ``.py`` file under
<backend-dir> (default: cwd). Used by scripts/rerun-failed.sh, a fix-loop aid that is never
evidence (see backend/tests/CLAUDE.md).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Same stdlib parser scripts/analyze-test-timing.py uses on junit files this repo wrote itself.
from xml.etree import ElementTree as ET  # nosec B405


def node_id(classname: str, name: str, base: Path) -> str:
    parts = classname.split('.')
    for i in range(len(parts), 0, -1):
        rel = Path(*parts[:i]).with_suffix('.py')
        if (base / rel).is_file():
            return '::'.join([rel.as_posix(), *parts[i:], name])
    return '::'.join([*parts, name])


def failed_ids(xml_path: Path, base: Path) -> list[str]:
    tree = ET.parse(xml_path)  # nosec B314
    out: list[str] = []
    for case in tree.iter('testcase'):
        if case.find('failure') is not None or case.find('error') is not None:
            out.append(node_id(case.get('classname', ''), case.get('name', ''), base))
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    base = Path(argv[2]) if len(argv) > 2 else Path.cwd()
    for nid in failed_ids(Path(argv[1]), base):
        print(nid)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
