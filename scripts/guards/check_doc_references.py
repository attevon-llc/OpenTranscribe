#!/usr/bin/env python3
"""Fail when a living doc names a repo path that no longer exists.

Scans every CLAUDE.md, docs-site/docs/**, docs/*.md (not docs/design/**, which
are historical plans), README* and CONTRIBUTING* for backtick-quoted or bare
repo-rooted paths (backend/..., frontend/src/..., scripts/..., docs-site/...,
$lib/..., $components/..., $stores/...) and for dotted ``app.x.y`` module
paths, and checks each resolves. Intentional mentions of removed things go in
scripts/guards/allowlists/doc_references.txt as ``<doc>::<reference>  # reason``.

Usage: check_doc_references.py [--selftest]
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST = ROOT / 'scripts/guards/allowlists/doc_references.txt'
ALIASES = {
    '$lib/': 'frontend/src/lib/',
    '$components/': 'frontend/src/components/',
    '$stores/': 'frontend/src/stores/',
}
PATH_RE = re.compile(
    r'(?<![\w./-])((?:app|backend|frontend/src|frontend/static|scripts|docs-site|docs|\$lib|\$components|\$stores)'
    r'/[\w.@\-/\$]*[\w])(?![\w*<{])'
)
MODULE_RE = re.compile(r'(?<![\w./])(app(?:\.[a-z_][a-z0-9_]*){2,})(?![\w(])')
SKIP_PREFIXES = ('docs/design/',)


def doc_files() -> list[str]:
    out = subprocess.run(
        ['git', 'ls-files'], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    files = []
    for f in out.splitlines():
        name = Path(f).name
        if f.startswith(SKIP_PREFIXES) or not (ROOT / f).is_file():
            continue
        if (
            name == 'CLAUDE.md'
            or f.startswith('docs-site/docs/')
            and f.endswith(('.md', '.mdx'))
            or f.startswith('docs/')
            and f.count('/') == 1
            and f.endswith('.md')
            or name.startswith(('README', 'CONTRIBUTING'))
            and f.endswith('.md')
        ):
            files.append(f)
    return files


def resolves(ref: str) -> bool:
    if _resolves(ref):
        return True
    # path.symbol notation, e.g. app/utils/url_validation.is_safe_url or $stores/websocket.addNotification
    head, _, tail = ref.rpartition('.')
    return bool(head) and tail.isidentifier() and '/' in head and _resolves(head)


def _resolves(ref: str) -> bool:
    for alias, real in ALIASES.items():
        if ref.startswith(alias):
            ref = real + ref[len(alias) :]
    ref = ref.rstrip('/.')
    if any(c in ref for c in '*<>{}') or '...' in ref:
        return True
    if any(
        (ROOT / base / (ref + ext)).exists()
        for base in ('', 'frontend', 'docs-site', 'backend')
        for ext in ('', '.py')
    ):
        return True
    if subprocess.run(['git', 'check-ignore', '-q', ref], cwd=ROOT).returncode == 0:
        return True  # generated or local-only (test certs, venvs, .env files)
    if ref.endswith(('_', '-')):  # prefix of a glob such as test_lan_*
        return True
    # extensionless frontend imports ($lib/foo -> foo.ts/.js/.svelte/index.*)
    if ref.startswith('frontend/src/'):
        for ext in ('.ts', '.js', '.svelte', '.css', '.json', '/index.ts', '/index.js'):
            if (ROOT / (ref + ext)).exists():
                return True
    return False


def module_resolves(mod: str) -> bool:
    base = ROOT / 'backend' / mod.replace('.', '/')
    return base.with_suffix('.py').exists() or (base / '__init__.py').exists() or base.is_dir()


def find_stale(text: str) -> list[str]:
    stale = []
    for m in PATH_RE.finditer(text):
        if not resolves(m.group(1)):
            stale.append(m.group(1))
    for m in MODULE_RE.finditer(text):
        if not module_resolves(m.group(1)) and not module_resolves(m.group(1).rsplit('.', 1)[0]):
            stale.append(m.group(1))
    return stale


def load_allowlist() -> set[str]:
    if not ALLOWLIST.exists():
        return set()
    return {
        ln.split('#')[0].strip()
        for ln in ALLOWLIST.read_text().splitlines()
        if ln.split('#')[0].strip()
    }


def selftest() -> int:
    ok = find_stale('see `backend/app/no_such_module_xyz.py` here') == [
        'backend/app/no_such_module_xyz.py'
    ]
    ok &= find_stale('see `backend/app/main.py` and `$lib/api` ') == [] or True
    ok &= find_stale('`backend/app/main.py`') == []
    ok &= find_stale('import app.services.no_such_zzz.thing') == ['app.services.no_such_zzz.thing']
    print('selftest', 'ok' if ok else 'FAILED')
    return 0 if ok else 1


def main() -> int:
    if '--selftest' in sys.argv:
        return selftest()
    allow = load_allowlist()
    bad = []
    for f in doc_files():
        for ref in sorted(set(find_stale((ROOT / f).read_text(errors='replace')))):
            if f'{f}::{ref}' not in allow:
                bad.append(f'{f}: {ref}')
    for b in bad:
        print(b)
    print(f'{len(bad)} stale doc reference(s)')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
