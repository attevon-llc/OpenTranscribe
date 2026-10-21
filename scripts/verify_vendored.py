#!/usr/bin/env python3
"""Verify or generate MANIFEST.json for a vendored copy of documents from this repo.

Verify:   verify_vendored.py VENDORED_DIR LEGAL_REPO_DIR
Write:    verify_vendored.py --write OUT_DIR LEGAL_REPO_DIR --tag TAG REL_PATH [REL_PATH ...]
          (copies nothing; writes OUT_DIR/MANIFEST.json from the repo files' front matter)
Selftest: verify_vendored.py --selftest

MANIFEST.json: {"tag": str, "files": {"<relative path>": {"key", "version", "sha256"}}}
The relative path is the same in the vendored folder and in this repo.
"""

import hashlib
import json
import sys
import tempfile
from pathlib import Path


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def front_matter(p: Path) -> dict:
    lines = p.read_text(encoding='utf-8').splitlines()
    out: dict = {}
    if not lines or lines[0].strip() != '---':
        return out
    for line in lines[1:]:
        if line.strip() == '---':
            break
        if ':' in line:
            k, v = line.split(':', 1)
            out[k.strip()] = v.strip()
    return out


def verify(vendored: Path, repo: Path) -> list[str]:
    errors: list[str] = []
    mf = vendored / 'MANIFEST.json'
    if not mf.is_file():
        return [f'missing {mf}']
    manifest = json.loads(mf.read_text())
    if not manifest.get('files'):
        return ['manifest lists no files']
    for name, meta in manifest['files'].items():
        v, c = vendored / name, repo / name
        if not v.is_file():
            errors.append(f'{name}: not in vendored folder')
            continue
        if not c.is_file():
            errors.append(f'{name}: not in central repo')
            continue
        vh, ch = sha256(v), sha256(c)
        if vh != meta.get('sha256'):
            errors.append(f'{name}: vendored file differs from manifest hash')
        if ch != meta.get('sha256'):
            errors.append(f'{name}: central repo file differs from manifest hash')
        if vh != ch:
            errors.append(f'{name}: vendored file differs from central repo')
    return errors


def write(out: Path, repo: Path, tag: str, rels: list[str]) -> None:
    files = {}
    for rel in rels:
        fm = front_matter(repo / rel)
        if 'key' not in fm or 'version' not in fm:
            raise SystemExit(f'{rel}: front matter needs key and version')
        files[rel] = {'key': fm['key'], 'version': fm['version'], 'sha256': sha256(repo / rel)}
    out.mkdir(parents=True, exist_ok=True)
    (out / 'MANIFEST.json').write_text(json.dumps({'tag': tag, 'files': files}, indent=2) + '\n')


def selftest() -> int:
    with tempfile.TemporaryDirectory() as t:
        repo, ven = Path(t) / 'repo', Path(t) / 'ven'
        (repo / 'd').mkdir(parents=True)
        doc = repo / 'd/x.md'
        doc.write_text('---\nkey: k-x\nversion: 1.0\n---\nbody\n')
        write(ven, repo, 't-v1', ['d/x.md'])
        (ven / 'd').mkdir()
        (ven / 'd/x.md').write_bytes(doc.read_bytes())
        ok = verify(ven, repo) == []
        (ven / 'd/x.md').write_text('---\nkey: k-x\nversion: 1.0\n---\nbody edited\n')
        bad = verify(ven, repo)
        print('must-pass:', 'PASS' if ok else 'FAIL')
        print('must-fail:', 'PASS' if bad else 'FAIL', bad)
        return 0 if ok and bad else 1


def main(a: list[str]) -> int:
    if a[:1] == ['--selftest']:
        return selftest()
    if a[:1] == ['--write'] and '--tag' in a:
        i = a.index('--tag')
        write(Path(a[1]), Path(a[2]), a[i + 1], a[i + 2 :])
        return 0
    if len(a) == 2:
        errs = verify(Path(a[0]), Path(a[1]))
        for e in errs:
            print('FAIL', e)
        print('OK' if not errs else f'{len(errs)} problem(s)')
        return 1 if errs else 0
    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
