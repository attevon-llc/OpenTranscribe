#!/usr/bin/env python3
"""Feature-parity inventory: prove the externally visible surface is unchanged between two refs.

    scripts/feature_inventory.py snapshot --ref master --out master.json
    scripts/feature_inventory.py diff a.json b.json [--accept accept.tsv]

``diff`` exits 1 on any difference not listed in the accept file (``section<TAB>key<TAB>reason``,
reason mandatory). Sections: api.routes, api.openapi, celery.*, db.*, settings.*, frontend.*,
cli.*, ws.events. Backend sections are extracted by feature_inventory_backend.py in a
``nice -n 19`` subprocess using this checkout's backend venv, against a ``git archive`` copy
of the ref, with a scrubbed environment of dummy values (the real .env is never read).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DUMMY_ENV = {
    'TESTING': 'True',
    'ENVIRONMENT': 'development',
    'POSTGRES_HOST': 'localhost',
    'POSTGRES_USER': 'dummy',
    'POSTGRES_PASSWORD': 'dummy',
    'POSTGRES_DB': 'dummy',
    'SECRET_KEY': 'dummy-secret-key-for-inventory-only-0123456789abcdef',
    'JWT_SECRET_KEY': 'dummy-secret-key-for-inventory-only-0123456789abcdef',
    'ENCRYPTION_KEY': 'dummy-encryption-key-for-inventory-only-0123456789',
    'MINIO_ROOT_USER': 'dummy',
    'MINIO_ROOT_PASSWORD': 'dummy-password',
    'OPENSEARCH_HOST': 'localhost',
    'REDIS_HOST': 'localhost',
}


def parse_shell_case(text: str, var_re: str = r'\$\{?(?:1|COMMAND)\b') -> dict:
    """Subcommand names from column-0 ``case "$1"`` blocks (patterns at the block's first indent)."""
    found: dict[str, bool] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if re.match(r'case\s+"?' + var_re, lines[i]):
            indent = None
            j = i + 1
            while j < len(lines) and not lines[j].startswith('esac'):
                m = re.match(
                    r'^(\s+)([A-Za-z0-9_|*"\'.\-?]+(?:\s*\|\s*[A-Za-z0-9_*"\'.\-?]+)*)\)', lines[j]
                )
                if m and (indent is None or len(m.group(1)) == indent):
                    indent = len(m.group(1))
                    for alt in m.group(2).split('|'):
                        alt = alt.strip().strip('"\'')
                        if alt and alt != '*':
                            found[alt] = True
                j += 1
            i = j
        i += 1
    return found


def cli_sections(root: Path) -> dict:
    out: dict = {}
    for script, label in (('opentr.sh', 'cli.opentr'), ('opentranscribe.sh', 'cli.opentranscribe')):
        p = root / script
        text = p.read_text('utf-8') if p.is_file() else ''
        entries = {f'cmd:{c}': True for c in parse_shell_case(text)}
        entries.update(
            {f'flag:{f}': True for f in set(re.findall(r'(?<![\w-])(--[a-z][a-z0-9-]+)', text))}
        )
        out[label] = dict(sorted(entries.items()))
    rel = root / 'scripts' / 'release.sh'
    stages: dict = {}
    if rel.is_file():
        m = re.search(r'^STAGES=\(([^)]*)\)', rel.read_text('utf-8'), re.M)
        stages = {f'stage:{s}': True for s in (m.group(1).split() if m else [])}
        stages.update(
            {f'cmd:{c}': True for c in parse_shell_case(rel.read_text('utf-8'), r'\$\{?COMMAND\b')}
        )
    out['cli.release'] = dict(sorted(stages.items()))
    return out


def frontend_sections(root: Path) -> dict:
    fe = root / 'frontend' / 'src'
    routes = {
        str(p.relative_to(fe / 'routes')): True
        for p in sorted((fe / 'routes').rglob('*'))
        if p.is_file()
    }
    locales = fe / 'lib' / 'i18n' / 'locales'

    def flat(d, prefix=''):
        for k, v in d.items():
            if isinstance(v, dict):
                yield from flat(v, f'{prefix}{k}.')
            else:
                yield f'{prefix}{k}'

    counts, keys = {}, {}
    for f in sorted(locales.glob('*.json')) if locales.is_dir() else []:
        ks = list(flat(json.loads(f.read_text('utf-8'))))
        counts[f.stem] = len(ks)
        if f.stem == 'en':
            keys = dict.fromkeys(sorted(ks), True)
    exports: dict = {}
    pat = re.compile(
        r'^export\s+(?:declare\s+)?(?:async\s+)?(?:function\*?|const|let|class|interface|type|enum)\s+(\w+)',
        re.M,
    )
    for f in sorted((fe / 'lib' / 'api').glob('*.ts')) if (fe / 'lib' / 'api').is_dir() else []:
        if '.test.' in f.name:
            continue
        text = f.read_text('utf-8')
        names = set(pat.findall(text))
        for grp in re.findall(r'^export\s*(?:type\s*)?\{([^}]*)\}', text, re.M):
            names.update(n.split(' as ')[-1].strip() for n in grp.split(',') if n.strip())
        exports.update(dict.fromkeys(names, True))
    return {
        'frontend.routes': routes,
        'frontend.i18n_keys': keys,
        'frontend.i18n_counts': counts,
        'frontend.api_exports': dict(sorted(exports.items())),
    }


def backend_sections(tree: Path) -> dict:
    py = REPO / 'backend' / 'venv' / 'bin' / 'python'
    env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(tree), **DUMMY_ENV}
    for d in ('TEMP_DIR', 'DATA_DIR', 'MODELS_DIR'):
        env[d] = str(tree / 'scratch')
    (tree / 'scratch').mkdir(exist_ok=True)
    env['PYTHONPATH'] = str(tree / 'backend')
    out = tree / 'backend-inventory.json'
    cmd = [
        'nice',
        '-n',
        '19',
        str(py if py.exists() else sys.executable),
        str(Path(__file__).with_name('feature_inventory_backend.py')),
        str(out),
    ]
    r = subprocess.run(
        cmd, cwd=tree / 'backend', env=env, capture_output=True, text=True, timeout=1800
    )
    if not out.is_file():
        raise RuntimeError(
            f'backend extraction produced nothing (rc={r.returncode}):\n{r.stderr[-3000:]}'
        )
    # The temp tree path leaks into path-valued settings defaults; make it comparable.
    return json.loads(out.read_text('utf-8').replace(str(tree), '<TREE>'))


def snapshot(ref: str) -> dict:
    tree = Path(tempfile.mkdtemp(prefix='ot-redcheck.'))
    try:
        arc = subprocess.Popen(['git', '-C', str(REPO), 'archive', ref], stdout=subprocess.PIPE)
        subprocess.run(['tar', '-x', '-C', str(tree)], stdin=arc.stdout, check=True)
        if arc.wait() != 0:
            raise RuntimeError(f'git archive {ref} failed')
        sections = {**backend_sections(tree), **frontend_sections(tree), **cli_sections(tree)}
    finally:
        shutil.rmtree(tree, ignore_errors=True)
    commit = subprocess.run(
        ['git', '-C', str(REPO), 'rev-parse', ref], capture_output=True, text=True
    ).stdout.strip()
    return {'_meta': {'ref': ref, 'commit': commit}, **sections}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='mode', required=True)
    s = sub.add_parser('snapshot')
    s.add_argument('--ref', required=True)
    s.add_argument('--out', required=True)
    d = sub.add_parser('diff')
    d.add_argument('a')
    d.add_argument('b')
    d.add_argument('--accept')
    args = ap.parse_args(argv)
    if args.mode == 'snapshot':
        t0 = time.time()
        snap = snapshot(args.ref)
        Path(args.out).write_text(
            json.dumps(snap, indent=1, sort_keys=True) + '\n', encoding='utf-8'
        )
        print(f'{args.ref}: {len(snap) - 1} sections in {time.time() - t0:.0f}s -> {args.out}')
        return 0
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from feature_inventory_diff import AcceptError, compute_diff, load_accept, render

    a, b = (json.loads(Path(p).read_text('utf-8')) for p in (args.a, args.b))
    a.pop('_meta', None)
    b.pop('_meta', None)
    try:
        accept = load_accept(args.accept)
    except AcceptError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    text, unaccepted = render(compute_diff(a, b), accept)
    sys.stdout.write(text)
    return 1 if unaccepted else 0


if __name__ == '__main__':
    sys.exit(main())
