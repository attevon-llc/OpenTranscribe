#!/usr/bin/env python3
"""Pre-commit guard: no planning ids or history names in comments, docstrings or file names.

Ids that describe how a change was planned (work-package numbers, wave and round
numbers, review finding ids, plan-style PR ids) describe the plan, not the code,
and rot the moment the plan is archived. Say why instead, or link a public issue
(``#NNN`` and ``PR #NNN`` are GitHub references and are never matched).

Scope, from ``git ls-files`` plus the guard baselines on disk:

* Comments and docstrings of ``.py .ts .svelte .js .mjs .sh`` files, Dockerfiles,
  ``docker-compose*.yml``, ``.github/workflows/*.yml``, and the ``#`` comments of
  ``backend/pyproject.toml`` and ``.pre-commit-config.yaml``. Python is read with
  ``tokenize`` and ``ast`` (string literals that are not docstrings are data and
  are ignored); other languages by comment prefix.
* The reason column of every ``scripts/guards/baselines/*.txt`` and the comments of
  ``scripts/guards/vulture_whitelist.py`` when it exists.
* File names under ``backend/tests``, ``scripts`` and ``frontend/src``.

Excluded, with the reason: ``backend/alembic/versions/`` (revision history is
immutable), ``CHANGELOG.md``, ``docs/`` and ``docs-site/`` (prose and plans),
``frontend/ffmpeg-wasm-build/test/fixtures/`` (binary media with a ``.ts`` suffix).

The guard counts comment and docstring hits only; the plan's ``rg`` counts include code
strings and every file type (measured: backend/app 118 hits in 35 files here against 109 in
31 by ``rg``, frontend/src 28/15 against 30/10, scripts 8/6 against 9/7).

Current hits live in ``baselines/process_ids.txt`` (``path<TAB>hits<TAB>reason``;
``path (file name)`` for name hits) and can only shrink (see ``_baseline.py``).
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import shutil
import subprocess
import sys
import tokenize
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _baseline

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_DIR = Path('scripts') / 'guards' / 'baselines'
BASELINE_PATH = REPO_ROOT / BASELINE_DIR / 'process_ids.txt'

EXCLUDED_PREFIXES = (
    'backend/alembic/versions/',
    'docs/',
    'docs-site/',
    'frontend/ffmpeg-wasm-build/test/fixtures/',
)
EXCLUDED_FILES = ('CHANGELOG.md',)
NAME_DIRS = ('backend/tests/', 'scripts/', 'frontend/src/')
C_STYLE = ('.ts', '.svelte', '.js', '.mjs')
HASH_STYLE = ('.sh',)
HASH_FILES = ('backend/pyproject.toml', '.pre-commit-config.yaml')
WHITELIST = 'scripts/guards/vulture_whitelist.py'

_W_ID = r'W\d+\.\d+[a-z]?'
ID_PATTERNS = tuple(
    re.compile(p)
    for p in (
        rf'\b{_W_ID}\b',
        r'(?i)\bwave \d',
        r'(?i)\bround \d',
        r'(?i)\bWP-?\d+',
        r'(?i)\b(?:audit )?finding [A-Z]?-?\d+\b',
        rf'\b{_W_ID}\W{{0,3}}Task \d+\b|\bTask \d+\W{{0,3}}{_W_ID}\b',
        r'\bPR \d+[a-c]?\b',
    )
)
NAME_PATTERN = re.compile(
    r'(?i)(?:^|[^a-z0-9])(?:wave|round|wp)[-_]?\d'
    r'|(?:^|[^a-z0-9])w\d+[_.]\d'
    r'|(?i:(?:^|[^a-z0-9])(?:audit[-_])?finding[-_][a-z]?-?\d)'
    r'|(?:^|[^a-z0-9])pr[-_]?\d'
)
# Superset of every id pattern's first tokens: a cheap scan that lets clean files skip parsing.
_PREFILTER = re.compile(r'W\d+\.\d|PR \d|Task \d')
_PREFILTER_LOWER = re.compile(r'wave \d|round \d|wp-?\d|finding [a-z]?-?\d')
_TRAILING_SLASHES = re.compile(r'(?:^|\s)//\s?(.*)$')
_TRAILING_HASH = re.compile(r'(?:^|\s)#\s?(.*)$')


def tracked_files(repo_root: Path) -> list[str]:
    git = shutil.which('git')
    if git is None:
        msg = 'git not found on PATH; cannot list tracked files'
        raise _baseline.ScanError(msg)
    out = subprocess.run(  # nosec B603  absolute git path, fixed argv
        [git, 'ls-files', '-z'], cwd=repo_root, capture_output=True, text=True, check=True
    ).stdout
    return [p for p in out.split('\0') if p]


def _is_dockerfile(rel: str) -> bool:
    return rel.rsplit('/', 1)[-1].startswith('Dockerfile')


def _comment_texts_python(source: str, rel: str) -> list[str]:
    tree = ast.parse(source, filename=rel)
    texts = [
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(source).readline)
        if tok.type == tokenize.COMMENT
    ]
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                texts.append(doc)
    return texts


def _is_hash_style(rel: str) -> bool:
    return rel.endswith(HASH_STYLE) or rel in HASH_FILES or _is_dockerfile(rel) or _is_yaml(rel)


def _is_yaml(rel: str) -> bool:
    return rel.endswith('.yml') and (
        rel.startswith('.github/workflows/') or rel.rsplit('/', 1)[-1].startswith('docker-compose')
    )


def _comment_texts_other(source: str, rel: str) -> list[str]:
    texts: list[str] = []
    hash_style = _is_hash_style(rel)
    in_block = False
    for line in source.splitlines():
        stripped = line.strip()
        if hash_style:
            if stripped.startswith('#!'):
                continue
            match = _TRAILING_HASH.search(line)
            if match:
                texts.append(match.group(1))
            continue
        if in_block:
            texts.append(stripped)
            in_block = '*/' not in stripped
            continue
        if stripped.startswith('/*'):
            texts.append(stripped)
            in_block = '*/' not in stripped
        elif stripped.startswith(('//', '<!--')):
            texts.append(stripped)
        else:
            match = _TRAILING_SLASHES.search(line)
            if match:
                texts.append(match.group(1))
    return texts


def count_ids(texts: list[str]) -> int:
    return sum(len(pattern.findall(t)) for t in texts for pattern in ID_PATTERNS)


def content_hits(repo_root: Path, rel: str) -> int:
    source = (repo_root / rel).read_text(encoding='utf-8')
    lowered = source.lower()
    if not (_PREFILTER.search(source) or _PREFILTER_LOWER.search(lowered)):
        return 0  # no id anywhere in the raw text, so none can be in a comment (skips parsing)
    if rel.endswith('.py'):
        return count_ids(_comment_texts_python(source, rel))
    return count_ids(_comment_texts_other(source, rel))


def baseline_reason_hits(repo_root: Path, rel: str) -> int:
    """Hits in the reason column (third tab-separated field) of a guard baseline."""
    texts = []
    for line in (repo_root / rel).read_text(encoding='utf-8').splitlines():
        parts = line.split('\t', 2)
        if len(parts) == 3:
            texts.append(parts[2])
    return count_ids(texts)


def is_baseline_file(rel: str) -> bool:
    return rel.startswith(f'{BASELINE_DIR.as_posix()}/') and rel.endswith('.txt')


def excluded(rel: str) -> bool:
    return rel.startswith(EXCLUDED_PREFIXES) or rel in EXCLUDED_FILES


def in_content_scope(rel: str) -> bool:
    if rel.endswith(('.py', *C_STYLE, *HASH_STYLE)):
        return True
    return rel in HASH_FILES or _is_dockerfile(rel) or _is_yaml(rel)


def candidate_files(repo_root: Path) -> list[str]:
    """Tracked files plus guard baselines and the whitelist present on disk (maybe untracked)."""
    files = set(tracked_files(repo_root))
    directory = repo_root / BASELINE_DIR
    if directory.is_dir():
        files.update(p.relative_to(repo_root).as_posix() for p in directory.glob('*.txt'))
    if (repo_root / WHITELIST).is_file():
        files.add(WHITELIST)
    return sorted(files)


def scan(repo_root: Path, files: list[str] | None = None) -> tuple[dict[str, int], list[str]]:
    """Return ``({key: hits}, unreadable files)``."""
    found: dict[str, int] = {}
    broken: list[str] = []
    for rel in files if files is not None else candidate_files(repo_root):
        if excluded(rel):
            continue
        if rel.startswith(NAME_DIRS) and NAME_PATTERN.search(rel):
            found[f'{rel} (file name)'] = 1
        is_baseline = is_baseline_file(rel)
        if not (is_baseline or in_content_scope(rel)):
            continue
        try:
            n = (
                baseline_reason_hits(repo_root, rel)
                if is_baseline
                else content_hits(repo_root, rel)
            )
        except FileNotFoundError:
            continue  # tracked but deleted in the worktree: nothing to read
        except (SyntaxError, tokenize.TokenError, UnicodeDecodeError, OSError):
            broken.append(rel)
            continue
        if n:
            found[rel] = n
    return found, broken


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, default=BASELINE_PATH)
    parser.add_argument('--tighten', action='store_true', help='Lower baseline values, drop stale.')
    parser.add_argument('--root', type=Path, default=REPO_ROOT)
    _baseline.add_baseline_args(parser)
    args = parser.parse_args(argv)

    found, broken = scan(args.root)
    if args.write_baseline:
        if broken:
            sys.stderr.write(f'error: unreadable files, refusing to write: {broken}\n')
            return 2
        written: int = _baseline.run_write_baseline(args, args.baseline, found)
        return written
    try:
        baseline = _baseline.load(args.baseline)
    except ValueError as exc:
        sys.stderr.write(f'ERROR: {exc}\n')
        return 2
    errors = [f'{rel}: cannot be read, so its comments are unchecked' for rel in broken]
    errors += _baseline.compare(found, baseline, unit='hit(s)')
    if args.tighten:
        changed = _baseline.tighten(args.baseline, found, baseline)
        sys.stderr.write(f'tightened {changed} baseline line(s)\n')
        errors = [e for e in errors if 'new violation' in e or 'grew past' in e or 'read' in e]
    if errors:
        sys.stderr.write(
            'ERROR: planning ids or history names found. Say why in plain words, link a '
            'public issue (#NNN), and name files by what they test.\n'
        )
        for e in errors:
            sys.stderr.write(f'  {e}\n')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
