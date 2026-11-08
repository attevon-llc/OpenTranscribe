#!/usr/bin/env python3
"""Pre-compile every frontend route in a freshly started dev stack, before the browser tests.

Why: a cold Vite dev server compiles each route on its first request. The e2e smoke runs three
browsers at once, and the first dark-theme pages to land on an uncompiled route were served the
app's "500 / Internal Error" page or never rendered `.user-button`; a different a11y test failed
each run, every one passed alone, and the same 69 tests all passed once the server was warm.
Visiting each route once, in the same browser engine, moves that cost out of the gate. Routes are
discovered from ``frontend/src/routes`` so a new page is warmed without editing this script.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_ROUTES_DIR = Path(__file__).resolve().parents[2] / 'frontend' / 'src' / 'routes'


def discover_routes(routes_dir: Path) -> list[str]:
    """URL paths for every ``+page.svelte`` under *routes_dir*.

    ``[param]`` segments become the literal ``{param}`` placeholder (callers substitute a real
    value); optional ``[[param]]`` segments are dropped. Route groups ``(group)`` do not appear
    in URLs.
    """
    routes: set[str] = set()
    for page in routes_dir.rglob('+page.svelte'):
        parts = []
        for seg in page.parent.relative_to(routes_dir).parts:
            if re.fullmatch(r'\(.*\)', seg) or re.fullmatch(r'\[\[.*\]\]', seg):
                continue
            m = re.fullmatch(r'\[(.*)\]', seg)
            parts.append('{' + m.group(1) + '}' if m else seg)
        routes.add('/' + '/'.join(parts))
    return sorted(routes)


def _get_json(url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None) -> Any:
    """Fetch *url* and decode its JSON body; refuses anything but an http(s) URL."""
    if urllib.parse.urlsplit(url).scheme not in ('http', 'https'):
        raise ValueError(f'refusing a non-HTTP URL: {url}')
    req = urllib.request.Request(url, data=data, headers=headers or {})  # scheme checked above
    with urllib.request.urlopen(req, timeout=30) as resp:  # scheme checked above
        return json.load(resp)


def _token(api: str, email: str, password: str) -> str:
    body = urllib.parse.urlencode({'username': email, 'password': password}).encode()
    return str(_get_json(f'{api}/api/auth/token', data=body)['access_token'])


def _first_file_id(api: str, token: str) -> str | None:
    try:
        data = _get_json(
            f'{api}/api/files?page_size=1', headers={'Authorization': f'Bearer {token}'}
        )
    except (OSError, ValueError):  # URLError/HTTPError are OSErrors; warming is best effort
        return None
    items = data if isinstance(data, list) else data.get('items') or data.get('files') or []
    return str(items[0].get('uuid') or items[0].get('id')) if items else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', required=True, help='frontend URL')
    ap.add_argument('--api', required=True, help='backend URL')
    ap.add_argument('--email', default='admin@example.com')
    ap.add_argument('--password', default='password')
    ap.add_argument('--routes-dir', type=Path, default=DEFAULT_ROUTES_DIR)
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    routes = discover_routes(args.routes_dir)
    file_id = _first_file_id(args.api, _token(args.api, args.email, args.password))
    visited = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(f'{args.base}/login')
        page.fill('#email', args.email)
        page.fill('#password', args.password)
        page.click('button[type=submit]')
        page.wait_for_selector('.user-button', timeout=60000)
        for route in routes:
            if '{' in route:
                if not file_id:
                    continue
                route = re.sub(r'\{[^}]*\}', file_id, route)
            page.goto(args.base + route, wait_until='networkidle', timeout=90000)
            visited += 1
        browser.close()
    print(f'warmed {visited} of {len(routes)} routes', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
