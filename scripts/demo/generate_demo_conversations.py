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
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

REFUSED_PROVIDERS = {'mock', 'mock-llm'}
TIMEOUT = 900
#: The corpus name `demo_seed import` gives every file it seeds (filename `demo__<item id>.transcript`).
SEEDED_CORPUS = 'demo'
#: A chat 429 (the remote-provider hourly message limit) is waited out, honouring Retry-After.
RETRY = Retry(
    total=None,
    connect=3,  # nothing was sent yet
    read=0,  # a read timeout on the streamed POST must not replay the question
    status=10,
    status_forcelist=[429],
    allowed_methods=None,  # the chat message is a POST; a 429 is refused before any work, so replay is safe
    backoff_factor=5,
    respect_retry_after_header=True,
    raise_on_status=False,
)


def _session(base: str, email: str, password: str) -> requests.Session:
    s = requests.Session()
    s.mount(base, HTTPAdapter(max_retries=RETRY))
    resp = s.post(f'{base}/api/auth/login', data={'username': email, 'password': password})
    resp.raise_for_status()
    if csrf := s.cookies.get('csrf_token'):
        s.headers['X-CSRF-Token'] = csrf
    return s


def seeded_filename(item_id: str) -> str:
    """The filename `demo_seed import` records for a bundle item; stable across title edits."""
    return f'{SEEDED_CORPUS}__{item_id}.transcript'


def match_files(files: list[dict], items: dict[str, dict]) -> tuple[dict[str, str], list[str]]:
    """Bundle item id -> file UUID, plus the item ids with no file.

    Tries, in order, the key the seeder records (the `demo__<id>.transcript` filename), the
    media filename the item was uploaded under, then the manifest title. A normal upload never
    carries the manifest title, so the title is the last resort and never the only route.
    """
    by_name: dict[str, str] = {}
    by_title: dict[str, str] = {}
    for f in files:
        if f.get('filename'):
            by_name.setdefault(f['filename'], f['uuid'])
        if f.get('title'):
            by_title.setdefault(f['title'], f['uuid'])
    found: dict[str, str] = {}
    missing: list[str] = []
    for item_id, item in items.items():
        candidates = (
            by_name.get(seeded_filename(item_id)),
            by_name.get(Path(str(item.get('media', ''))).name),
            by_title.get(str(item.get('title', ''))),
        )
        uuid = next((c for c in candidates if c), None)
        if uuid:
            found[item_id] = uuid
        else:
            missing.append(item_id)
    return found, missing


def split_prompts(
    prompts: list[dict], item_ids: set[str]
) -> tuple[list[dict], list[tuple[str, list[str]]]]:
    """(runnable prompts, [(prompt id, items the bundle lacks)]) for prompts naming absent items."""
    runnable: list[dict] = []
    skipped: list[tuple[str, list[str]]] = []
    for conv in prompts:
        absent = [i for i in conv['items'] if i not in item_ids]
        if absent:
            skipped.append((conv['id'], absent))
        else:
            runnable.append(conv)
    return runnable, skipped


def _list_files(s: requests.Session, base: str) -> list[dict]:
    files: list[dict] = []
    page = 1
    while True:
        data = s.get(f'{base}/api/files', params={'page': page, 'page_size': 100}).json()
        files += data['items']
        if not data.get('has_more'):
            return files
        page += 1


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
    ap.add_argument(
        '--strict',
        action='store_true',
        help='fail, rather than skip, when a prompt names an item that is not in the bundle',
    )
    args = ap.parse_args()
    base = args.base_url.rstrip('/')
    password = os.environ.get(args.password_env, '')
    if not password:
        sys.exit(f'set {args.password_env}')

    manifest_path = args.bundle / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    items = {i['id']: i for i in manifest['items']}
    all_prompts = json.loads(args.prompts.read_text(encoding='utf-8'))['conversations']
    prompts, skipped = split_prompts(all_prompts, set(items))
    for prompt_id, absent in skipped:
        print(f'skipping {prompt_id}: not in the bundle: {absent}', file=sys.stderr)
    if skipped and args.strict:
        sys.exit('prompts name items that are not in the bundle (drop --strict to skip them)')

    s = _session(base, args.email, password)
    needed = {i for conv in prompts for i in conv['items']}
    uuids, missing = match_files(_list_files(s, base), {i: items[i] for i in needed})
    if missing:
        sys.exit(f'seeded files not found for items: {missing}')
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
                if not m.get('citations'):
                    sys.exit(f'{conv["id"]}: an answer has no citations (retrieval failed?)')
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
