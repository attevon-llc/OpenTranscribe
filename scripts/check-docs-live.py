#!/usr/bin/env python3
"""Prove the LIVE docs site is current and working (stdlib only).

Why this exists: after v0.6.1 shipped, https://docs.opentranscribe.app/ stayed on v0.6.0 for
hours because the deploy was queued behind a GitHub Actions incident and nothing compared the
live site with the release. ``deploy-docs.yml`` only deploys on a push to master touching
``docs-site/**`` or ``VERSION`` (plus a daily cron), so a missed deploy is otherwise silent.

Every expectation is DERIVED from the checkout being checked (VERSION, the newest blog file,
the newest ``upgrading-to-X-Y-Z`` page, ``sidebars.ts``) -- nothing is hard-coded to a release.

Checks:
  a  home page returns 200 (redirects followed)
  b  the version badge on the home page equals VERSION
  c  newest blog post is reachable and listed on /blog/
  d  newest upgrade page under docs/operations is reachable
  e  key pages (derived from sidebars.ts, plus --key-page) return 200
  f  sitemap.xml parses and contains the newest blog URL
  g  freshness: site build time (Last-Modified of the home page, or the latest successful
     deploy run with --use-gh) is not older than the last commit touching docs-site/** or
     VERSION. Inside the grace window that is "deploy pending", not a failure.

Exit codes: 0 all checks ok, 1 a check failed, 3 the site could not be reached at all.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

EXIT_OK, EXIT_FAILED, EXIT_UNREACHABLE = 0, 1, 3
DEFAULT_BASE_URL = 'https://docs.opentranscribe.app'
DEFAULT_REPO = 'attevon-llc/OpenTranscribe'
DEPLOY_WORKFLOW = 'deploy-docs.yml'
# Sidebar doc ids probed as "key pages" when the sidebar lists them.
KEY_PAGE_IDS = (
    'getting-started/quick-start',
    'installation/docker-compose',
    'configuration/environment-variables',
)
PASS, FAIL, PENDING = 'PASS', 'FAIL', 'PENDING'


class Unreachable(Exception):
    """The host could not be contacted (DNS/connect/timeout), as opposed to an HTTP error."""


@dataclass
class Result:
    check: str
    status: str
    detail: str


def fetch(url: str, timeout: float = 20.0) -> tuple[int, str, dict[str, str]]:
    """GET ``url`` following redirects. Returns (status, body, headers); raises Unreachable."""
    req = urllib.request.Request(url, headers={'User-Agent': 'opentranscribe-docs-live-check'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode('utf-8', errors='replace')
            return resp.status, body, {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        return exc.code, '', {k.lower(): v for k, v in exc.headers.items()}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise Unreachable(f'{url}: {exc}') from exc


# ---- derivation from the repo checkout -------------------------------------------------


def read_version(repo: Path) -> str:
    return (repo / 'VERSION').read_text(encoding='utf-8').strip().lstrip('v')


def newest_blog_slug(repo: Path) -> str | None:
    """Slug of the newest post by the date prefix in its filename (front-matter slug wins)."""
    blog = repo / 'docs-site' / 'blog'
    dated = re.compile(r'^(\d{4}-\d{2}-\d{2})-(.+?)(?:\.mdx?)?$')
    best: tuple[str, str, Path] | None = None
    best_src: Path | None = None
    for entry in blog.iterdir() if blog.is_dir() else []:
        m = dated.match(entry.name)
        if not m:
            continue
        src = entry if entry.is_file() else next(iter(sorted(entry.glob('index.md*'))), None)
        if src is None:
            continue
        if best is None or (m.group(1), entry.name) > (best[0], best[2].name):
            best = (m.group(1), m.group(2), entry)
            best_src = src
    if best is None or best_src is None:
        return None
    front = re.search(r"^slug:\s*['\"]?([^'\"\n]+?)['\"]?\s*$", best_src.read_text('utf-8'), re.M)
    return front.group(1).lstrip('/') if front else best[1]


def newest_upgrade_page(repo: Path) -> str | None:
    """Doc id (operations/upgrading-to-X-Y-Z) with the highest version, or None."""
    pat = re.compile(r'^upgrading-to-(\d+)-(\d+)-(\d+)\.mdx?$')
    found = []
    ops = repo / 'docs-site' / 'docs' / 'operations'
    for f in ops.iterdir() if ops.is_dir() else []:
        m = pat.match(f.name)
        if m:
            found.append((tuple(int(x) for x in m.groups()), f.stem))
    return f'operations/{max(found)[1]}' if found else None


def key_page_ids(repo: Path, extra: list[str]) -> list[str]:
    sidebars = repo / 'docs-site' / 'sidebars.ts'
    text = sidebars.read_text('utf-8') if sidebars.is_file() else ''
    text = re.sub(r'^\s*//.*$', '', text, flags=re.M)  # commented-out sidebars are not pages
    docs = repo / 'docs-site' / 'docs'
    ids = {
        i
        for i in re.findall(r"['\"]([a-z0-9][\w-]*(?:/[\w-]+)+)['\"]", text)
        if (docs / f'{i}.md').is_file() or (docs / f'{i}.mdx').is_file()
    }
    keys = [i for i in KEY_PAGE_IDS if i in ids]
    api = sorted(i for i in ids if re.search(r'(^|/)api(-|/|$)', i))
    keys += api[:1]
    return keys + [e for e in extra if e not in keys]


def last_change_time(repo: Path, ref: str) -> datetime | None:
    try:
        out = subprocess.run(
            [
                'git',
                '-C',
                str(repo),
                'log',
                '-1',
                '--format=%cI',
                ref,
                '--',
                'docs-site',
                'VERSION',
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout.strip()
        return datetime.fromisoformat(out) if out else None
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


# ---- checks ---------------------------------------------------------------------------


def shown_version(html: str) -> str | None:
    m = re.search(r'heroBadges[^"\s>]*"?>\s*<span[^>]*>\s*v?(\d+\.\d+\.\d+[^<\s]*)\s*</span>', html)
    return m.group(1) if m else None


def run_content_checks(a: argparse.Namespace, repo: Path | None) -> list[Result]:
    base = a.base_url.rstrip('/')
    out: list[Result] = []
    status, home, _ = fetch(base + '/')
    out.append(Result('a home page', PASS if status == 200 else FAIL, f'GET {base}/ -> {status}'))

    expected = a.expected_version or (read_version(repo) if repo else None)
    if expected is None:
        out.append(Result('b version', FAIL, 'no VERSION file and no --expected-version'))
    else:
        got = shown_version(home)
        ok = got == expected.lstrip('v')
        out.append(
            Result(
                'b version',
                PASS if ok else FAIL,
                f'site shows {got or "no version badge"}, expected {expected.lstrip("v")}',
            )
        )

    slug = newest_blog_slug(repo) if repo else None
    blog_url = f'{base}/blog/{slug}' if slug else None
    if slug is None:
        out.append(Result('c newest blog', FAIL, 'no dated post found under docs-site/blog'))
    else:
        st, _, _ = fetch(blog_url)
        _, listing, _ = fetch(base + '/blog/')
        listed = f'/blog/{slug}' in listing
        ok = st == 200 and listed
        out.append(
            Result(
                'c newest blog',
                PASS if ok else FAIL,
                f'/blog/{slug} -> {st}; listed on /blog/: {listed}',
            )
        )

    page = newest_upgrade_page(repo) if repo else None
    if page is None:
        out.append(
            Result('d upgrade page', FAIL, 'no operations/upgrading-to-X-Y-Z page in the repo')
        )
    else:
        st, _, _ = fetch(f'{base}/docs/{page}')
        out.append(Result('d upgrade page', PASS if st == 200 else FAIL, f'/docs/{page} -> {st}'))

    keys = key_page_ids(repo, a.key_page) if repo else list(a.key_page)
    bad = []
    for kid in keys:
        st, _, _ = fetch(f'{base}/docs/{kid}')
        if st != 200:
            bad.append(f'{kid} -> {st}')
    out.append(
        Result(
            'e key pages',
            FAIL if bad or not keys else PASS,
            ('; '.join(bad) if bad else f'{len(keys)} pages ok: {", ".join(keys)}')
            if keys
            else 'no key pages derived',
        )
    )

    st, xml, _ = fetch(base + '/sitemap.xml')
    if st != 200:
        out.append(Result('f sitemap', FAIL, f'sitemap.xml -> {st}'))
    else:
        try:
            locs = [
                urlparse(e.text.strip()).path.rstrip('/')
                for e in ET.fromstring(xml).iter()
                if e.tag.endswith('loc') and e.text
            ]
        except ET.ParseError as exc:
            out.append(Result('f sitemap', FAIL, f'sitemap.xml does not parse: {exc}'))
        else:
            # Paths, not full URLs: the sitemap carries the configured production host even when
            # --base-url points at a mirror or a test server.
            ok = slug is not None and f'/blog/{slug}' in locs
            out.append(
                Result(
                    'f sitemap',
                    PASS if ok else FAIL,
                    f'{len(locs)} urls; newest blog URL present: {ok}',
                )
            )
    return out


def deploy_run_state(repo_slug: str) -> str | None:
    """'queued'/'in_progress' when a Deploy Documentation run is waiting or running."""
    for state in ('in_progress', 'queued'):
        try:
            out = subprocess.run(
                [
                    'gh',
                    'api',
                    f'repos/{repo_slug}/actions/workflows/{DEPLOY_WORKFLOW}/runs?status={state}&per_page=1',
                    '--jq',
                    '.total_count',
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            ).stdout.strip()
            if out and int(out) > 0:
                return state
        except (subprocess.SubprocessError, OSError, ValueError):
            return None
    return None


def built_time(a: argparse.Namespace, headers: dict[str, str]) -> tuple[datetime | None, str]:
    if a.use_gh:
        try:
            out = subprocess.run(
                [
                    'gh',
                    'api',
                    f'repos/{a.repo}/actions/workflows/{DEPLOY_WORKFLOW}/runs?status=success&per_page=1',
                    '--jq',
                    '.workflow_runs[0].updated_at',
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            ).stdout.strip()
            if out:
                return datetime.fromisoformat(
                    out.replace('Z', '+00:00')
                ), 'last successful Deploy Documentation run'
        except (subprocess.SubprocessError, OSError, ValueError):
            pass
    lm = headers.get('last-modified')
    if lm:
        try:
            return parsedate_to_datetime(lm), 'Last-Modified of the home page'
        except (TypeError, ValueError):
            pass
    return None, 'no build time available'


def freshness(
    a: argparse.Namespace, repo: Path | None, headers: dict[str, str], now: datetime
) -> Result:
    changed = (
        datetime.fromisoformat(a.last_change_time)
        if a.last_change_time
        else (last_change_time(repo, a.ref) if repo else None)
    )
    if changed is None:
        return Result(
            'g freshness',
            PASS,
            'skipped: no git history for docs-site/** or VERSION to compare against',
        )
    if changed.tzinfo is None:
        changed = changed.replace(tzinfo=UTC)
    built, source = built_time(a, headers)
    if built is None:
        return Result('g freshness', FAIL, f'cannot determine when the site was built ({source})')
    if built >= changed:
        return Result(
            'g freshness',
            PASS,
            f'built {built.isoformat()} ({source}) >= last change {changed.isoformat()}',
        )
    age = now - changed
    state = deploy_run_state(a.repo) if a.use_gh else None
    queue = f'; Deploy Documentation run is {state}' if state else ''
    if age <= timedelta(minutes=a.grace_minutes):
        return Result(
            'g freshness',
            PENDING,
            f'deploy queued/in progress: last change {changed.isoformat()} is {int(age.total_seconds() // 60)} min old, '
            f'inside the {a.grace_minutes} min grace window{queue}',
        )
    return Result(
        'g freshness',
        FAIL,
        f'STALE: site built {built.isoformat()} ({source}) but docs-site/VERSION last changed {changed.isoformat()} '
        f'({int(age.total_seconds() // 60)} min ago, grace {a.grace_minutes} min){queue}',
    )


def run_once(a: argparse.Namespace, repo: Path | None) -> list[Result]:
    now = datetime.now(UTC)
    _, _, headers = fetch(a.base_url.rstrip('/') + '/')
    fresh = freshness(a, repo, headers, now)
    results = run_content_checks(a, repo)
    if fresh.status == PENDING:
        # A site that is behind because its deploy is still within the grace window is not an alarm.
        results = [Result(r.check, PENDING, r.detail) if r.status == FAIL else r for r in results]
    return results + [fresh]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument('--base-url', default=DEFAULT_BASE_URL)
    p.add_argument(
        '--repo-root',
        default=str(Path(__file__).resolve().parent.parent),
        help='checkout to derive expectations from',
    )
    p.add_argument(
        '--ref', default='HEAD', help="git ref whose last docs-site/VERSION commit defines 'fresh'"
    )
    p.add_argument('--expected-version', help='override the VERSION file')
    p.add_argument(
        '--key-page',
        action='append',
        default=[],
        help='extra doc id to probe, e.g. features/rag-chat',
    )
    p.add_argument(
        '--wait-seconds', type=int, default=0, help='retry window for pages-cache lag (checks a-f)'
    )
    p.add_argument('--grace-minutes', type=int, default=90)
    p.add_argument(
        '--use-gh', action='store_true', help='use the gh CLI for build time / queued-run detection'
    )
    p.add_argument('--repo', default=os.environ.get('GITHUB_REPOSITORY', DEFAULT_REPO))
    p.add_argument('--last-change-time', help='ISO time overriding the git lookup')
    a = p.parse_args(argv)
    if not a.base_url.startswith(('http://', 'https://')):
        p.error('--base-url must be http(s)')
    repo = Path(a.repo_root)
    repo = repo if (repo / 'docs-site').is_dir() else None

    deadline, delay = time.monotonic() + a.wait_seconds, 5.0
    while True:
        try:
            results = run_once(a, repo)
            unreachable = None
        except Unreachable as exc:
            results, unreachable = [], str(exc)
        failed = unreachable or any(r.status == FAIL for r in results)
        if not failed or time.monotonic() + delay > deadline:
            break
        print(
            f'... not ready ({unreachable or "checks failing"}); retrying in {delay:.0f}s',
            flush=True,
        )
        time.sleep(delay)
        delay = min(delay * 2, 60.0)

    if unreachable:
        print(f'UNREACHABLE: {unreachable}')
        return EXIT_UNREACHABLE
    for r in results:
        print(f'{r.status:7} {r.check}: {r.detail}')
    page = newest_upgrade_page(repo) if repo else None
    if repo and page:
        side = (
            (repo / 'docs-site' / 'sidebars.ts').read_text('utf-8')
            if (repo / 'docs-site' / 'sidebars.ts').is_file()
            else ''
        )
        if f"'{page}'" not in side:
            print(f'NOTE    {page} exists but is not listed in sidebars.ts (reachable by URL only)')
    bad = [r for r in results if r.status == FAIL]
    pend = [r for r in results if r.status == PENDING]
    print(json.dumps({'failed': [r.check for r in bad], 'pending': [r.check for r in pend]}))
    return EXIT_FAILED if bad else EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
