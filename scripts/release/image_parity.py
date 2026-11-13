#!/usr/bin/env python3
"""Tested-vs-shipped parity for the backend image (EFFD / TR-P).

One implementation of "what are the Python bytes" for both ends of the release:

  record-tested  (60-test.sh)  fingerprint the checkout the gate tested plus the image it ran,
                               write tested-images.tsv and tested-fingerprint.txt.
  check-shipped  (70-tag.sh)   fingerprint the backend leg image that will ship and compare.

Fingerprint = pip_freeze_sha (sha256 of the sorted `pip freeze --all` of an image) and
app_tree_sha (sha256 of the sorted `<sha256>  <path>` manifest of every git-tracked file under
backend/ that backend/.dockerignore lets into the build context). The image side hashes exactly
the paths of that manifest inside /app, so build-time extras (.pyc, generated files) can never
cause a false mismatch while any changed, missing or substituted tracked file does.

.dockerignore semantics follow the Docker docs (patterns are relative to the context root, `*`
and `?` do not cross `/`, `**` does, a matching directory excludes its contents, `!` re-includes,
the last match wins): https://docs.docker.com/build/concepts/context/#dockerignore-files
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTEXT = 'backend'


def _regex(pattern: str) -> re.Pattern[str]:
    out, i = '', 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith('**/', i):
            out += '(?:.*/)?'
            i += 3
            continue
        if pattern.startswith('**', i):
            out += '.*'
            i += 2
            continue
        out += '[^/]*' if c == '*' else '[^/]' if c == '?' else re.escape(c)
        i += 1
    return re.compile(out + r'\Z')


def load_dockerignore(text: str) -> list[tuple[bool, re.Pattern[str]]]:
    rules = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        negate = line.startswith('!')
        line = line.lstrip('!').strip().lstrip('/').rstrip('/')
        if line:
            rules.append((negate, _regex(line)))
    return rules


def is_ignored(path: str, rules: list[tuple[bool, re.Pattern[str]]]) -> bool:
    parts = path.split('/')
    prefixes = ['/'.join(parts[: n + 1]) for n in range(len(parts))]
    ignored = False
    for negate, rx in rules:
        if any(rx.match(p) for p in prefixes):
            ignored = not negate
    return ignored


def tracked_paths(repo: Path = REPO_ROOT) -> list[str]:
    out = subprocess.run(
        ['git', '-C', str(repo), 'ls-files', '-z', '--', CONTEXT],
        check=True,
        capture_output=True,
    ).stdout.decode()
    rules = load_dockerignore((repo / CONTEXT / '.dockerignore').read_text(encoding='utf-8'))
    prefix = CONTEXT + '/'
    rel = [p[len(prefix) :] for p in out.split('\0') if p.startswith(prefix)]
    return sorted(p for p in rel if not is_ignored(p, rules) and (repo / CONTEXT / p).is_file())


def checkout_manifest(repo: Path = REPO_ROOT) -> list[str]:
    lines = []
    for p in tracked_paths(repo):
        digest = hashlib.sha256((repo / CONTEXT / p).read_bytes()).hexdigest()
        lines.append(f'{digest}  {p}')
    return lines


def image_manifest(image: str, paths: list[str]) -> list[str]:
    """sha256 of exactly `paths` inside the image's /app; a missing file is a loud line."""
    script = (
        'cd /app && while IFS= read -r p; do '
        'if [ -f "$p" ]; then sha256sum -- "$p"; else echo "MISSING  $p"; fi; done'
    )
    res = subprocess.run(
        ['docker', 'run', '--rm', '-i', '--entrypoint', 'sh', image, '-c', script],
        input='\n'.join(paths) + '\n',
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(res.stdout.splitlines())


def pip_freeze_lines(image: str) -> list[str]:
    res = subprocess.run(
        ['docker', 'run', '--rm', '--entrypoint', 'python', image, '-m', 'pip', 'freeze', '--all'],
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(line.strip() for line in res.stdout.splitlines() if line.strip())


def sha_of(lines: list[str]) -> str:
    return hashlib.sha256(('\n'.join(lines) + '\n').encode()).hexdigest()


def compare(tested: dict[str, str], shipped: dict[str, str]) -> list[str]:
    """Human-readable differences; empty list = parity. Pure, so it is unit-testable."""
    problems = []
    if tested.get('pip_freeze_sha') != shipped.get('pip_freeze_sha'):
        problems.append(
            f'pip freeze differs: tested {tested.get("pip_freeze_sha")} vs shipped '
            f'{shipped.get("pip_freeze_sha")}'
        )
    if tested.get('app_tree_sha') != shipped.get('app_tree_sha'):
        problems.append(
            f'/app tree differs: tested {tested.get("app_tree_sha")} vs shipped '
            f'{shipped.get("app_tree_sha")}'
        )
    return problems


def read_kv(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        k, sep, v = line.partition('=')
        if sep:
            out[k.strip()] = v.strip()
    return out


def cmd_record_tested(a: argparse.Namespace) -> int:
    rows = [r.split('\t') for r in Path(a.services).read_text().splitlines() if r.strip()]
    Path(a.tsv).write_text(''.join('\t'.join(r) + '\n' for r in rows), encoding='utf-8')
    manifest = checkout_manifest()
    fp = {
        'pip_freeze_sha': sha_of(pip_freeze_lines(a.backend_image)),
        'app_tree_sha': sha_of(manifest),
        'git_commit': subprocess.run(
            ['git', '-C', str(REPO_ROOT), 'rev-parse', 'HEAD'],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip(),
        'app_files': str(len(manifest)),
    }
    Path(a.fingerprint).write_text(''.join(f'{k}={v}\n' for k, v in fp.items()), encoding='utf-8')
    print(f'recorded {len(rows)} tested service image(s), {len(manifest)} /app files')
    return 0


def cmd_check_shipped(a: argparse.Namespace) -> int:
    tested = read_kv(Path(a.fingerprint))
    paths = tracked_paths()
    img = image_manifest(a.image, paths)
    shipped = {'pip_freeze_sha': sha_of(pip_freeze_lines(a.image)), 'app_tree_sha': sha_of(img)}
    expected = {line.split('  ', 1)[1]: line.split('  ', 1)[0] for line in checkout_manifest()}
    differing = []
    for line in img:
        digest, _, path = line.partition('  ')
        if digest == 'MISSING' or expected.get(path) != digest:
            differing.append(path.strip())
    problems = compare(tested, shipped)
    with open(a.out, 'w', encoding='utf-8') as f:
        f.write(f'image={a.image}\ntested_commit={tested.get("git_commit")}\n')
        f.write(
            f'tested_pip_freeze_sha={tested.get("pip_freeze_sha")}\nshipped_pip_freeze_sha={shipped["pip_freeze_sha"]}\n'
        )
        f.write(
            f'tested_app_tree_sha={tested.get("app_tree_sha")}\nshipped_app_tree_sha={shipped["app_tree_sha"]}\n'
        )
        f.write(f'first_differing_path={differing[0] if differing else ""}\n')
    for p in problems:
        print(p, file=sys.stderr)
    if differing:
        print(
            f'first differing /app path: {differing[0]} ({len(differing)} total)', file=sys.stderr
        )
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('record-tested')
    r.add_argument('--services', required=True, help='TSV: service, image id, GIT_SHA')
    r.add_argument('--backend-image', required=True)
    r.add_argument('--tsv', required=True)
    r.add_argument('--fingerprint', required=True)
    r.set_defaults(fn=cmd_record_tested)
    c = sub.add_parser('check-shipped')
    c.add_argument('--image', required=True)
    c.add_argument('--fingerprint', required=True)
    c.add_argument('--out', required=True)
    c.set_defaults(fn=cmd_check_shipped)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == '__main__':
    sys.exit(main())
