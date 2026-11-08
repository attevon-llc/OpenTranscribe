#!/usr/bin/env python3
"""Generate the demo bundle's chat conversations with the real app chat pipeline (#628, P-D).

Run it ONCE, by the person building the seed bundle, against a throwaway stack that has the
bundle's recordings seeded (DEMO_MODE=false) and a REAL model configured for the demo user
(vLLM on GPU 2, or GPU 1). The mock LLM is refused on purpose: shipped demo content must be
real model output. For every conversation in the prompts file it creates a chat, asks the
questions over HTTP (``POST /api/chat/conversations/{uuid}/messages``, the same route the UI
uses), reads the stored turns back, and writes ``conversations/<id>.json`` plus the
``conversations`` list in the bundle's ``manifest.json``. ``demo_seed validate`` then checks it.

    python3 scripts/demo/generate_demo_conversations.py \\
        --bundle /path/to/bundle --base-url http://localhost:5174 \\
        --email demo@example.com --password-env DEMO_USER_PASSWORD \\
        [--prompts demo/conversation-prompts.json]

Review every answer before shipping: a wrong or revealing answer is edited out by deleting
the conversation and regenerating, never by hand-editing model text.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

REFUSED_PROVIDERS = {'mock', 'mock-llm'}
TIMEOUT = 900


def _session(base: str, email: str, password: str) -> requests.Session:
    s = requests.Session()
    resp = s.post(f'{base}/api/auth/login', data={'username': email, 'password': password})
    resp.raise_for_status()
    if csrf := s.cookies.get('csrf_token'):
        s.headers['X-CSRF-Token'] = csrf
    return s


def _file_uuids(s: requests.Session, base: str, titles: dict[str, str]) -> dict[str, str]:
    """bundle item id -> seeded file UUID, matched on the manifest title."""
    by_title: dict[str, str] = {}
    page = 1
    while True:
        data = s.get(f'{base}/api/files', params={'page': page, 'page_size': 100}).json()
        for f in data['items']:
            for key in ('title', 'filename'):
                if f.get(key):
                    by_title.setdefault(f[key], f['uuid'])
        if not data.get('has_more'):
            break
        page += 1
    missing = [i for i, t in titles.items() if t not in by_title]
    if missing:
        sys.exit(f'seeded files not found for items: {missing}')
    return {i: by_title[t] for i, t in titles.items()}


def _ask(s: requests.Session, base: str, conv_uuid: str, question: str) -> None:
    with s.post(
        f'{base}/api/chat/conversations/{conv_uuid}/messages',
        json={'content': question},
        stream=True,
        timeout=TIMEOUT,
    ) as resp:
        resp.raise_for_status()
        for _ in resp.iter_lines():  # drain the SSE stream until the server closes it
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--bundle', type=Path, required=True)
    ap.add_argument('--base-url', required=True)
    ap.add_argument('--email', required=True)
    ap.add_argument('--password-env', default='DEMO_USER_PASSWORD')
    ap.add_argument('--prompts', type=Path, default=Path('demo/conversation-prompts.json'))
    args = ap.parse_args()
    base = args.base_url.rstrip('/')
    password = os.environ.get(args.password_env, '')
    if not password:
        sys.exit(f'set {args.password_env}')

    manifest_path = args.bundle / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    titles = {i['id']: i['title'] for i in manifest['items']}
    prompts = json.loads(args.prompts.read_text(encoding='utf-8'))['conversations']

    s = _session(base, args.email, password)
    uuids = _file_uuids(s, base, titles)
    item_by_uuid = {v: k for k, v in uuids.items()}
    out_dir = args.bundle / 'conversations'
    out_dir.mkdir(exist_ok=True)
    entries = []
    for conv in prompts:
        created = s.post(
            f'{base}/api/chat/conversations',
            json={
                'title': conv['title'],
                'scope': {'file_uuids': [uuids[i] for i in conv['items']]},
            },
        )
        created.raise_for_status()
        conv_uuid = created.json()['uuid']
        for question in conv['questions']:
            _ask(s, base, conv_uuid, question)
        stored = s.get(f'{base}/api/chat/conversations/{conv_uuid}/messages').json()['messages']
        messages = []
        for m in stored:
            if m['status'] != 'complete':
                sys.exit(f'{conv["id"]}: a turn ended {m["status"]!r}: {m.get("error")}')
            if (m.get('provider') or '').lower() in REFUSED_PROVIDERS:
                sys.exit(f'{conv["id"]}: answered by a mock provider; use a real model')
            msg = {'role': m['role'], 'content': m['content']}
            if m['role'] == 'assistant':
                msg.update(provider=m['provider'], model=m['model'])
                if m.get('reasoning_content'):
                    msg['reasoning_content'] = m['reasoning_content']
                msg['citations'] = [
                    {
                        'item_id': item_by_uuid[c['file_uuid']],
                        **{
                            k: c.get(k)
                            for k in (
                                'kind',
                                'title',
                                'chunk_index',
                                'start_time',
                                'end_time',
                                'speaker',
                                'snippet',
                            )
                        },
                    }
                    for c in (m.get('citations') or [])
                    if c['file_uuid'] in item_by_uuid
                ]
            messages.append(msg)
        rel = f'conversations/{conv["id"]}.json'
        (args.bundle / rel).write_text(
            json.dumps(
                {'title': conv['title'], 'items': conv['items'], 'messages': messages},
                indent=2,
                ensure_ascii=False,
            )
            + '\n',
            encoding='utf-8',
        )
        entries.append({'id': conv['id'], 'file': rel})
        print(f'wrote {rel} ({len(messages)} messages)')
    manifest['conversations'] = entries
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
