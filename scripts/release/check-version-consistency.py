#!/usr/bin/env python3
"""Single source of truth for "is this repo internally consistent about its version?".

Python rather than shell because this parses two JSON files, TOML, YAML-ish
frontmatter and an Alembic revision graph, and because the agent interface needs
structured output. Pure stdlib (tomllib is 3.11+), so it runs on a bare CI runner
with no pip install.

Modes:
    ci        every commit — the checks that must hold at all times
    pre-tag   before tagging — adds CHANGELOG/tag/blog checks
    post-tag  after tagging — the tag must exist and point at HEAD

Exit codes (stable, for agents):
    0  all enforced checks passed
    1  one or more enforced checks failed
    2  misuse (bad arguments)

Usage:
    check-version-consistency.py --mode ci
    check-version-consistency.py --mode pre-tag --json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SEMVER_RE = re.compile(r'^\d+\.\d+\.\d+$')

# Severity: a check is enforced when its mode is active. WARN never fails the run.
BLOCK = 'blocking'
WARN = 'warn'


@dataclass
class Result:
    check_id: str
    status: str  # pass | fail | warn | skip
    summary: str
    detail: str = ''
    fix: str = ''
    modes: tuple[str, ...] = ()


@dataclass
class Ctx:
    mode: str
    version_file: str = ''
    semver: str = ''
    results: list[Result] = field(default_factory=list)

    def add(
        self,
        check_id: str,
        ok: bool | None,
        summary: str,
        detail: str = '',
        fix: str = '',
        severity: str = BLOCK,
    ) -> None:
        if ok is None:
            status = 'skip'
        elif ok:
            status = 'pass'
        else:
            status = 'fail' if severity == BLOCK else 'warn'
        self.results.append(Result(check_id, status, summary, detail, fix))


def _read(path: Path) -> str:
    return path.read_text(encoding='utf-8')


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ['git', '-C', str(REPO_ROOT), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ''


# --------------------------------------------------------------------- checks


def check_version_file(ctx: Ctx) -> None:
    path = REPO_ROOT / 'VERSION'
    if not path.is_file():
        ctx.add('version-file', False, 'VERSION file missing', fix='echo vX.Y.Z > VERSION')
        return
    raw = _read(path).strip()
    if not raw.startswith('v') or not SEMVER_RE.match(raw[1:]):
        ctx.add(
            'version-file',
            False,
            'VERSION must be vX.Y.Z',
            detail=f'found {raw!r}',
            fix='echo vX.Y.Z > VERSION',
        )
        return
    ctx.version_file = raw
    ctx.semver = raw[1:]
    ctx.add('version-file', True, f'VERSION = {raw}')


def check_pyproject(ctx: Ctx) -> None:
    path = REPO_ROOT / 'pyproject.toml'
    data = tomllib.loads(_read(path))
    actual = data.get('project', {}).get('version')
    ok = actual == ctx.semver
    ctx.add(
        'pyproject-version',
        ok,
        'pyproject.toml [project].version',
        detail='' if ok else f'{actual!r} != {ctx.semver!r}',
        fix=f'set version = "{ctx.semver}" in pyproject.toml',
    )


def check_package_json(ctx: Ctx) -> None:
    path = REPO_ROOT / 'frontend' / 'package.json'
    actual = json.loads(_read(path)).get('version')
    ok = actual == ctx.semver
    ctx.add(
        'frontend-package-version',
        ok,
        'frontend/package.json .version',
        detail='' if ok else f'{actual!r} != {ctx.semver!r}',
        fix=f'set "version": "{ctx.semver}" in frontend/package.json',
    )


def check_package_lock(ctx: Ctx) -> None:
    """package-lock.json carries the version TWICE and they can diverge.

    `npm install --package-lock-only` is a separate, skippable step in the old
    manual checklist, and hand-editing only the top-level .version leaves
    .packages[""].version stale. Both are checked.
    """
    path = REPO_ROOT / 'frontend' / 'package-lock.json'
    data = json.loads(_read(path))
    top = data.get('version')
    nested = data.get('packages', {}).get('', {}).get('version')
    bad = [
        f'{name}={value!r}'
        for name, value in (('.version', top), ('.packages[""].version', nested))
        if value != ctx.semver
    ]
    ctx.add(
        'frontend-lock-version',
        not bad,
        'frontend/package-lock.json (both version fields)',
        detail='' if not bad else f'expected {ctx.semver!r}; ' + ', '.join(bad),
        fix='(cd frontend && npm install --package-lock-only)',
    )


def check_alembic_single_head(ctx: Ctx) -> None:
    """Exactly one head, derived from the graph — see lib/alembic-head.py."""
    helper = REPO_ROOT / 'scripts' / 'release-tests' / 'lib' / 'alembic-head.py'
    proc = subprocess.run(
        [sys.executable, str(helper), str(REPO_ROOT / 'backend'), '--json'],
        capture_output=True,
        text=True,
    )
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        ctx.add(
            'alembic-head',
            False,
            'could not derive Alembic head',
            detail=proc.stderr.strip()[:400],
        )
        return
    problems = report.get('problems', [])
    ctx.add(
        'alembic-head',
        not problems,
        f'Alembic chain: 1 head ({report.get("head")}), {report.get("revision_count")} revisions',
        detail='; '.join(problems),
        fix='repoint down_revision, or add a merge migration',
    )


def check_dockerfile_build_arg(ctx: Ctx) -> None:
    """The prod image gets its version ONLY via --build-arg APP_VERSION.

    The backend build context is ./backend, so the repo-root VERSION file is not
    in the image. If this ARG ever disappears, released images silently report
    version "unknown" and the About modal's mismatch warning goes quiet.
    """
    path = REPO_ROOT / 'backend' / 'Dockerfile.prod'
    text = _read(path)
    ok = re.search(r'^ARG\s+APP_VERSION', text, re.MULTILINE) is not None
    ctx.add(
        'dockerfile-app-version-arg',
        ok,
        'backend/Dockerfile.prod declares ARG APP_VERSION',
        detail='' if ok else 'ARG APP_VERSION not found',
        fix='restore `ARG APP_VERSION=unknown` / `ENV APP_VERSION=${APP_VERSION}`',
    )


def check_blog_slugs(ctx: Ctx) -> None:
    """Duplicate slugs break `npm run build` the moment a draft is un-drafted."""
    blog = REPO_ROOT / 'docs-site' / 'blog'
    if not blog.is_dir():
        ctx.add('blog-slugs', None, 'docs-site/blog not present')
        return
    slugs: dict[str, list[str]] = {}
    for post in sorted(blog.glob('*.md')):
        match = re.search(r'^slug:\s*(\S+)', _read(post), re.MULTILINE)
        if match:
            slugs.setdefault(match.group(1), []).append(post.name)
    dupes = {slug: names for slug, names in slugs.items() if len(names) > 1}
    ctx.add(
        'blog-slugs',
        not dupes,
        'docs-site/blog has no duplicate slugs',
        detail='; '.join(f'{slug}: {", ".join(names)}' for slug, names in dupes.items()),
        fix='delete or re-slug the superseded draft',
    )


def check_changelog(ctx: Ctx) -> None:
    """Warn-only by design in ci mode.

    The release process bumps VERSION in one commit and writes the CHANGELOG in a
    later one, so hard-failing here would block the intermediate commit.
    """
    path = REPO_ROOT / 'CHANGELOG.md'
    text = _read(path)
    has_section = re.search(
        rf'^##\s*\[{re.escape(ctx.semver)}\]\s*-\s*\d{{4}}-\d{{2}}-\d{{2}}',
        text,
        re.MULTILINE,
    )
    severity = BLOCK if ctx.mode in ('pre-tag', 'post-tag') else WARN
    ctx.add(
        'changelog-section',
        bool(has_section),
        f'CHANGELOG.md has a dated section for {ctx.semver}',
        detail='' if has_section else f'no `## [{ctx.semver}] - YYYY-MM-DD` heading',
        fix=f'promote [Unreleased] to `## [{ctx.semver}] - <date>`',
        severity=severity,
    )


def check_git_tag(ctx: Ctx) -> None:
    tag = ctx.version_file
    exists = bool(_git('tag', '--list', tag))
    if ctx.mode == 'pre-tag':
        ctx.add(
            'git-tag-absent',
            not exists,
            f'{tag} not yet tagged',
            detail='' if not exists else f'tag {tag} already exists',
            fix=f'git tag -d {tag} && git push origin :refs/tags/{tag}',
        )
    elif ctx.mode == 'post-tag':
        if not exists:
            ctx.add('git-tag-present', False, f'{tag} tag missing', fix=f'git tag -a {tag}')
            return
        at_tag = _git('rev-list', '-n', '1', tag)
        head = _git('rev-parse', 'HEAD')
        ctx.add(
            'git-tag-present',
            at_tag == head,
            f'{tag} points at HEAD',
            detail='' if at_tag == head else f'tag={at_tag[:9]} head={head[:9]}',
        )
    else:
        ctx.add('git-tag', None, 'tag check not run in ci mode')


CHECKS = {
    'ci': [
        check_version_file,
        check_pyproject,
        check_package_json,
        check_package_lock,
        check_alembic_single_head,
        check_dockerfile_build_arg,
        check_blog_slugs,
        check_changelog,
    ],
    'pre-tag': [check_changelog, check_git_tag],
    'post-tag': [check_changelog, check_git_tag],
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['ci', 'pre-tag', 'post-tag'], default='ci')
    parser.add_argument('--json', action='store_true', help='machine-readable output')
    args = parser.parse_args()

    ctx = Ctx(mode=args.mode)
    check_version_file(ctx)
    if not ctx.semver:
        _emit(ctx, args.json)
        return 1

    ran = {check_version_file}
    for check in CHECKS['ci'] + CHECKS.get(args.mode, []):
        if check in ran:
            continue
        ran.add(check)
        check(ctx)

    return _emit(ctx, args.json)


def _emit(ctx: Ctx, as_json: bool) -> int:
    failed = [r for r in ctx.results if r.status == 'fail']
    warned = [r for r in ctx.results if r.status == 'warn']
    passed = [r for r in ctx.results if r.status == 'pass']

    if as_json:
        print(
            json.dumps(
                {
                    'stage': 'version-consistency',
                    'mode': ctx.mode,
                    'version': ctx.version_file,
                    'status': 'fail' if failed else 'pass',
                    'criteria': [
                        {
                            'id': r.check_id,
                            'status': r.status,
                            'summary': r.summary,
                            'detail': r.detail,
                            'fix': r.fix,
                        }
                        for r in ctx.results
                    ],
                    'next': ([r.fix for r in failed if r.fix] if failed else ['proceed']),
                },
                indent=2,
            )
        )
        return 1 if failed else 0

    for r in ctx.results:
        icon = {'pass': 'PASS', 'fail': 'FAIL', 'warn': 'WARN', 'skip': 'SKIP'}[r.status]
        print(f'{icon}  {r.summary}')
        if r.status in ('fail', 'warn') and r.detail:
            print(f'      {r.detail}')
        if r.status == 'fail' and r.fix:
            print(f'      fix: {r.fix}')
    print(
        f'—— {len(passed)} passed, {len(failed)} failed, {len(warned)} warnings (mode={ctx.mode})'
    )
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
