#!/usr/bin/env python3
"""Fail when a rendered architecture diagram no longer matches its spec (issue #1230).

The diagrams at ``/architecture`` are rendered by ``scripts/generate-architecture-diagrams.sh``
(Archify) from ``docs-site/architecture-diagrams/specs/<name>.<type>.json`` into
``docs-site/static/architecture/<name>.html``. The generator stamps each HTML with
``<!-- spec-sha256: <hex> -->``, the SHA-256 of the spec bytes it rendered. This check needs
only the standard library (CI's pre-commit job installs nothing else, and Archify is not
available there), so it compares stamps rather than re-rendering:

- a spec whose HTML is missing, unstamped, or stamped with another hash -> drift;
- an HTML with no spec -> orphan (a renamed or deleted spec left its old render behind).

Usage:
    python3 scripts/check-architecture-diagram-drift.py            # check the repo
    python3 scripts/check-architecture-diagram-drift.py --selftest # prove it can fail
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_DIR = REPO_ROOT / 'docs-site' / 'architecture-diagrams' / 'specs'
HTML_DIR = REPO_ROOT / 'docs-site' / 'static' / 'architecture'
STAMP = re.compile(r'<!-- spec-sha256: ([0-9a-f]{64}) -->')
REGENERATE = 'regenerate with scripts/generate-architecture-diagrams.sh <name>'


def spec_name(spec: Path) -> str:
    """``transcription-pipeline.workflow.json`` -> ``transcription-pipeline`` (the script's rule)."""
    return spec.name.split('.', 1)[0]


def spec_hash(spec: Path) -> str:
    return hashlib.sha256(spec.read_bytes()).hexdigest()


def stamp_of(html: Path) -> str | None:
    found = STAMP.findall(html.read_text(encoding='utf-8', errors='replace'))
    return found[-1] if found else None


def find_drift(spec_dir: Path = SPEC_DIR, html_dir: Path = HTML_DIR) -> list[str]:
    """Every spec/HTML mismatch, as one human-readable line each (empty means clean)."""
    problems: list[str] = []
    specs = {spec_name(p): p for p in sorted(spec_dir.glob('*.json'))}
    if not specs:
        return [f'no specs found in {spec_dir}']
    for name, spec in specs.items():
        html = html_dir / f'{name}.html'
        if not html.is_file():
            problems.append(f'{name}: no rendered HTML at {html}; {REGENERATE}')
            continue
        stamp = stamp_of(html)
        if stamp is None:
            problems.append(f'{name}: {html.name} carries no spec-sha256 stamp; {REGENERATE}')
        elif stamp != spec_hash(spec):
            problems.append(
                f'{name}: {spec.name} changed since {html.name} was rendered; {REGENERATE}'
            )
    for html in sorted(html_dir.glob('*.html')):
        if html.stem not in specs:
            problems.append(f'{html.stem}: {html.name} has no spec in {spec_dir} (orphan render)')
    return problems


def _write_case(root: Path, spec_text: str, html_text: str | None) -> tuple[Path, Path]:
    spec_dir, html_dir = root / 'specs', root / 'html'
    spec_dir.mkdir(parents=True)
    html_dir.mkdir(parents=True)
    spec = spec_dir / 'demo.workflow.json'
    spec.write_text(spec_text, encoding='utf-8')
    if html_text is not None:
        (html_dir / 'demo.html').write_text(html_text, encoding='utf-8')
    return spec_dir, html_dir


def selftest() -> list[str]:
    """Must-fire and must-stay-clean cases; returns the cases that misbehaved."""
    spec_text = '{"diagram_type": "workflow"}\n'
    good = hashlib.sha256(spec_text.encode()).hexdigest()
    cases = [
        ('clean: stamp matches', spec_text, f'<html></html>\n<!-- spec-sha256: {good} -->\n', 0),
        ('fire: spec edited', spec_text + ' ', f'<html></html>\n<!-- spec-sha256: {good} -->\n', 1),
        ('fire: no stamp', spec_text, '<html></html>\n', 1),
        ('fire: html missing', spec_text, None, 1),
    ]
    failures = []
    for label, spec_body, html_body, expected in cases:
        with tempfile.TemporaryDirectory() as tmp:
            spec_dir, html_dir = _write_case(Path(tmp), spec_body, html_body)
            got = len(find_drift(spec_dir, html_dir))
        if got != expected:
            failures.append(f'{label}: expected {expected} finding(s), got {got}')
    with tempfile.TemporaryDirectory() as tmp:
        spec_dir, html_dir = _write_case(Path(tmp), spec_text, f'<!-- spec-sha256: {good} -->')
        (html_dir / 'stale.html').write_text('<html></html>', encoding='utf-8')
        if len(find_drift(spec_dir, html_dir)) != 1:
            failures.append('fire: orphan html: expected 1 finding')
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--selftest', action='store_true', help='run the must-fire cases')
    args = parser.parse_args()
    if args.selftest:
        failures = selftest()
        for failure in failures:
            print(f'SELF-TEST BROKEN: {failure}', file=sys.stderr)
        return 1 if failures else 0
    problems = find_drift()
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    print('architecture diagrams match their specs')
    return 0


if __name__ == '__main__':
    sys.exit(main())
