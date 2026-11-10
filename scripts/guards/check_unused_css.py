#!/usr/bin/env python3
"""Ratchet guard: global CSS classes that no frontend source references.

A global class is one defined in a tracked ``frontend/src/styles/*.css`` file or inside a
``:global(...)`` selector of a component ``<style>`` block. svelte-check already reports an unused
*scoped* selector; a global one is invisible to it, so a class whose last user was deleted stays
in the bundle and in every reader's head forever.

A class counts as referenced when its name appears as a whole token (bounded by anything but
``[A-Za-z0-9_-]``) in a tracked non-test ``.svelte`` / ``.ts`` / ``.js`` / ``.html`` file under
``frontend/src``, with ``<style>`` blocks stripped (a rule is a definition, not a use). That
covers ``class="..."``, ``class:x``, ``classList``, ``querySelector('.x')``, clsx and
string literals. It is deliberately lenient: a class named like a common word passes because the
word appears somewhere. The guard therefore reports only classes that are certainly unused.

Names built at runtime are handled two ways. A template-literal or concatenation prefix seen in
a source (``\\`badge-${tone}\\```, ``'icon-' + name``) marks every class starting with it as
referenced. Anything else (third-party DOM such as Plyr or highlight.js, classes set by a
library at runtime) goes in ``allowlists/unused_css.txt`` as ``prefix<TAB>1<TAB>class: reason``
where the key is a class name or a ``prefix*`` pattern.

Findings are ``path::class`` (value 1) against ``baselines/unused_css.txt``; the baseline can only
shrink. An unreadable file or unbalanced braces exit 2: an unparseable stylesheet must never read
as clean.

Usage::

    check_unused_css.py [--root DIR] [--baseline FILE] [--allowlist FILE] [--tighten]
    check_unused_css.py --write-baseline --reason TEXT [--force]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _baseline as bl

_HERE = Path(__file__).resolve().parent

#: Allowlist reason classes (see ``_baseline.load_allowlist``).
ALLOW_CLASSES = ('third-party-dom', 'runtime-generated', 'external-consumer')

_COMMENT = re.compile(r'/\*[\s\S]*?\*/')
_STYLE_BLOCK = re.compile(r'<style\b[^>]*>([\s\S]*?)</style>', re.IGNORECASE)
_STRING = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
_URL = re.compile(r'url\([^)]*\)', re.IGNORECASE)
_CLASS_IN_SELECTOR = re.compile(r'\.(-?[A-Za-z_][\w-]*)')
_TOKEN = re.compile(r'[A-Za-z_][\w-]*')
# `badge-${x}`, "badge-" + x, 'badge-' + x : a prefix that ends in "-" or "_" right before the
# dynamic part. A bare `${x}` with no literal prefix cannot be resolved and needs an allowlist.
_DYNAMIC_PREFIX = re.compile(r'([A-Za-z_][\w-]*[-_])\$\{|([A-Za-z_][\w-]*[-_])[\'"]\s*\+')
_REF_SUFFIXES = ('.svelte', '.ts', '.js', '.html')
_TEST_FILE = re.compile(r'\.(test|spec)\.[jt]s$|(^|/)(__tests__|__mocks__)/')


def _tracked(root: Path, prefix: str) -> list[str]:
    try:
        out = subprocess.run(
            ['git', 'ls-files', '-z', '--', prefix],
            cwd=root,
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        msg = f'git ls-files failed under {root}: {exc}'
        raise bl.ScanError(msg) from exc
    return sorted(p for p in out.decode().split('\0') if p)


def _read(root: Path, rel: str) -> str:
    try:
        return (root / rel).read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as exc:
        msg = f'cannot read {rel}: {exc}'
        raise bl.ScanError(msg) from exc


def _selectors(css: str, rel: str) -> list[str]:
    """Every selector (the text before a ``{``) in ``css``, at any nesting depth."""
    text = _URL.sub('url()', _STRING.sub('""', _COMMENT.sub('', css)))
    selectors: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(text):
        if ch == '{':
            selectors.append(text[start:i])
            depth += 1
            start = i + 1
        elif ch == '}':
            depth -= 1
            if depth < 0:
                msg = f'{rel}: unbalanced "}}"'
                raise bl.ScanError(msg)
            start = i + 1
        elif ch == ';':
            start = i + 1
    if depth != 0:
        msg = f'{rel}: unbalanced "{{"'
        raise bl.ScanError(msg)
    # A declaration-list "selector" is an at-rule prelude or a property: skip those without a dot
    # in a selector position. `@media (min-width: 1.5rem)` carries no `.name`.
    return [s.strip() for s in selectors if not s.strip().startswith('@')]


def _global_parts(selector: str) -> list[str]:
    """The argument text of every ``:global(...)`` in a component selector (balanced parens)."""
    parts: list[str] = []
    for m in re.finditer(r':global\(', selector):
        depth = 1
        i = m.end()
        while i < len(selector) and depth:
            depth += {'(': 1, ')': -1}.get(selector[i], 0)
            i += 1
        parts.append(selector[m.end() : i - 1])
    return parts


def defined_classes(root: Path) -> dict[str, set[str]]:
    """Map defining file -> global class names it defines."""
    found: dict[str, set[str]] = {}
    for rel in _tracked(root, 'frontend/src'):
        if rel.startswith('frontend/src/styles/') and rel.endswith('.css'):
            chunks = _selectors(_read(root, rel), rel)
        elif rel.endswith('.svelte'):
            chunks = [
                part
                for block in _STYLE_BLOCK.findall(_read(root, rel))
                for sel in _selectors(block, rel)
                for part in _global_parts(sel)
            ]
        else:
            continue
        names = {m.group(1) for chunk in chunks for m in _CLASS_IN_SELECTOR.finditer(chunk)}
        if names:
            found[rel] = names
    return found


def references(root: Path) -> tuple[set[str], set[str]]:
    """``(tokens, dynamic prefixes)`` across every non-test source under ``frontend/src``."""
    tokens: set[str] = set()
    prefixes: set[str] = set()
    for rel in _tracked(root, 'frontend/src'):
        if not rel.endswith(_REF_SUFFIXES) or _TEST_FILE.search(rel):
            continue
        text = _read(root, rel)
        if rel.endswith('.svelte'):
            text = _STYLE_BLOCK.sub('', text)
        tokens.update(_TOKEN.findall(text))
        for m in _DYNAMIC_PREFIX.finditer(text):
            prefixes.add(m.group(1) or m.group(2))
    return tokens, prefixes


def _allowed(name: str, allow: dict[str, bl.Entry]) -> bool:
    return name in allow or any(k.endswith('*') and name.startswith(k[:-1]) for k in allow)


def scan(root: Path, allow: dict[str, bl.Entry]) -> dict[str, int]:
    """``path::class -> 1`` for every defined global class nothing references."""
    if not (root / 'frontend' / 'src').is_dir():
        msg = f'{root}/frontend/src is not a directory; pass --root explicitly'
        raise bl.ScanError(msg)
    tokens, prefixes = references(root)
    findings: dict[str, int] = {}
    for rel, names in defined_classes(root).items():
        for name in sorted(names):
            if name in tokens or _allowed(name, allow):
                continue
            if any(name.startswith(p) for p in prefixes):
                continue
            findings[f'{rel}::{name}'] = 1
    return findings


def stale_allowlist(root: Path, allow: dict[str, bl.Entry]) -> list[str]:
    """Allowlist keys that match no defined class: an exemption for nothing is a stale exemption."""
    names = {n for ns in defined_classes(root).values() for n in ns}
    return sorted(k for k in allow if not any(_allowed(n, {k: allow[k]}) for n in names))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--root', type=Path, default=None, help='repository root')
    parser.add_argument('--baseline', type=Path, default=None, help='baseline file')
    parser.add_argument('--allowlist', type=Path, default=None, help='allowlist file')
    parser.add_argument('--tighten', action='store_true', help='drop stale baseline entries')
    bl.add_baseline_args(parser)
    args = parser.parse_args(argv)
    try:
        root = args.root or bl.find_root(_HERE, ('frontend/src',))
        guards = root / 'scripts' / 'guards'
        baseline_path = args.baseline or (guards / 'baselines' / 'unused_css.txt')
        allow_path = args.allowlist or (guards / 'allowlists' / 'unused_css.txt')
        allow = bl.load_allowlist(allow_path, ALLOW_CLASSES)
        findings = scan(root, allow)
        stale = stale_allowlist(root, allow)
        if args.write_baseline:
            written: int = bl.run_write_baseline(args, baseline_path, findings)
            return written
        baseline = bl.load(baseline_path)
    except (bl.ScanError, ValueError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    if stale:
        for key in stale:
            print(
                f'{allow_path}: {key}: matches no defined class; delete the line', file=sys.stderr
            )
        return 1
    return bl.finish_ratchet(
        args,
        baseline_path,
        findings,
        baseline,
        unit='unused global classes',
        heading='global CSS classes no frontend source references:',
        ok_message=f'unused css: {len(findings)} unreferenced global classes, within the baseline',
    )


if __name__ == '__main__':
    sys.exit(main())
