#!/usr/bin/env python3
"""Export / import / verify the OpenSearch speaker-embedding (voiceprint) indices.

Issue #658: speaker voiceprints exist in exactly ONE place. PostgreSQL stores no
embedding vectors at all — ``SpeakerProfile`` carries only ``embedding_count`` and
``last_embedding_update`` (``backend/app/models/media.py``), and ``Speaker`` /
``SpeakerCluster`` have no vector column — so the ``speakers_v*`` indices are the
sole copy of the deployment's biometric data. ``./opentr.sh backup`` was a bare
``pg_dump``, which means a stock deployment had **no recoverable copy of it**.
Voiceprints are not "derived data": re-deriving one needs the source media (which
may be gone) plus a GPU re-embed run.

This module is the artifact half of the fix. ``scripts/common.sh`` drives it —
``os_export_speaker_indices`` / ``os_import_speaker_indices`` /
``os_verify_speaker_restore`` — and both front ends (``opentr.sh`` and
``opentranscribe.sh``) reach it through those.

**It runs INSIDE the OpenSearch container**, piped in as
``docker compose exec -T opensearch python3 -c "$(cat this-file)" <mode>``. That
placement is deliberate and load-bearing:

* it needs no host tooling (the OpenSearch image ships ``python3``), so a
  production ``curl | bash`` install is covered identically to a git checkout;
* it talks to ``127.0.0.1:9200`` inside the container, so it works while every
  application container is **stopped** — which is exactly the state
  ``restore_database`` leaves them in between the replay and the restart decision.

⚠️ It authenticates with nothing. The shipped OpenSearch runs with
``DISABLE_SECURITY_PLUGIN=true`` in both dev and prod (``docker-compose.yml``,
``docker-compose.prod.yml``'s ``OPENSEARCH_DISABLE_SECURITY`` default), the same
assumption ``scripts/speaker-profiles-backup.sh`` already makes. On a deployment
that turned the security plugin on, a 401/403 is reported as a hard failure naming
the cause — it never degrades to an empty artifact, because an empty artifact is
indistinguishable from "this deployment has no voiceprints".

Artifact format — one self-describing file, ``<dump-stem>.voiceprints.ndjson``:

* **line 1** a manifest object: format/version, the alias target, and per index the
  mappings, the settings worth restoring, the document count and a content digest.
* **lines 2..n** ``_bulk``-ready pairs: an action line, then the ``_source`` line.

Keeping the manifest in the same file matters: a count/digest stored separately is
a count/digest that gets moved, encrypted or copied without its data, and the
verification step that makes a restore trustworthy would then silently have
nothing to compare against.

The digest is a content hash over ``id + canonical JSON source``, never a
similarity comparison, so nothing here reads or writes an OpenSearch
``cosinesimil`` score and ``app/utils/cosine_space.py`` does not apply.

Modes::

    export           # -> artifact on stdout
    import           # <- artifact on stdin; creates missing indices, bulk-loads
    verify           # <- artifact on stdin; exit 1 if the live cluster differs
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.client
import json
import sys
import time
from typing import Any

FORMAT_NAME = 'opentranscribe-voiceprints'
FORMAT_VERSION = 1

# Index settings worth carrying across a restore. Everything else OpenSearch
# reports under `settings.index` is read-only metadata (`uuid`, `creation_date`,
# `provided_name`, `version.*`) that a `PUT /<index>` rejects outright, so an
# unfiltered round-trip fails on every index it tries to recreate.
_RESTORABLE_SETTINGS = ('number_of_shards', 'number_of_replicas', 'knn')

_SCROLL_KEEPALIVE = '2m'
_SCROLL_PAGE = 500
_BULK_DOCS_PER_REQUEST = 500


class VoiceprintBackupError(RuntimeError):
    """Any failure that must abort the backup/restore rather than degrade."""


class _Client:
    """Minimal OpenSearch HTTP client (stdlib only — the container has no urllib3)."""

    def __init__(self, host: str, port: int, timeout: float = 120.0) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout

    def request(self, method: str, path: str, body: Any = None) -> dict:
        """Issue one request and return the decoded JSON body.

        Args:
            method: HTTP verb.
            path: Path beginning with ``/``.
            body: A JSON-serialisable object, a pre-encoded NDJSON ``str``, or None.

        Returns:
            The decoded response body.

        Raises:
            VoiceprintBackupError: On a transport failure or a non-2xx status.
        """
        if isinstance(body, str):
            payload = body.encode('utf-8')
            content_type = 'application/x-ndjson'
        elif body is None:
            payload = None
            content_type = 'application/json'
        else:
            payload = json.dumps(body).encode('utf-8')
            content_type = 'application/json'

        conn = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)
        try:
            headers = {'Content-Type': content_type} if payload is not None else {}
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            status = response.status
        except OSError as exc:
            raise VoiceprintBackupError(
                f'OpenSearch is unreachable at {self._host}:{self._port} ({exc})'
            ) from exc
        finally:
            conn.close()

        if status in (401, 403):
            raise VoiceprintBackupError(
                f'OpenSearch refused the request with HTTP {status}. This helper '
                'authenticates with nothing, which is correct for the shipped '
                'DISABLE_SECURITY_PLUGIN=true configuration. This deployment has the '
                'security plugin enabled, so voiceprints cannot be exported this way — '
                'use the in-app scheduled backup (Settings -> Backups), which holds the '
                'OpenSearch credentials.'
            )
        if status >= 400:
            detail = raw.decode('utf-8', 'replace')[:500]
            raise VoiceprintBackupError(f'{method} {path} failed with HTTP {status}: {detail}')
        if not raw:
            return {}
        return json.loads(raw.decode('utf-8'))

    def exists(self, path: str) -> bool:
        """Return True when a HEAD on ``path`` answers 2xx, False on 404."""
        conn = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)
        try:
            conn.request('HEAD', path)
            status = conn.getresponse().status
        except OSError as exc:
            raise VoiceprintBackupError(
                f'OpenSearch is unreachable at {self._host}:{self._port} ({exc})'
            ) from exc
        finally:
            conn.close()
        return status < 400


def _canonical(doc_id: str, source: dict) -> str:
    """Render one document as the stable line the content digest is computed over."""
    return doc_id + '\t' + json.dumps(source, sort_keys=True, separators=(',', ':'))


def digest_documents(documents: list) -> str:
    """Content digest over ``(doc_id, source)`` pairs, order-independent.

    Args:
        documents: ``(doc_id, source)`` tuples.

    Returns:
        ``sha256:<hex>`` over the id-sorted canonical rendering of every document.
        This is an exact-content hash, NOT a similarity measure — a single changed
        vector component changes it.
    """
    hasher = hashlib.sha256()
    for doc_id, source in sorted(documents, key=lambda pair: pair[0]):
        hasher.update(_canonical(doc_id, source).encode('utf-8'))
        hasher.update(b'\n')
    return 'sha256:' + hasher.hexdigest()


def speaker_index_names(client: _Client, index_base: str) -> list:
    """Return every concrete speaker index, newest-name-last, alias entries excluded.

    ``_cat/indices`` resolves the ``speakers`` alias onto its concrete target, so a
    pattern scan finds ``speakers_v3`` / ``speakers_v4`` / ``speakers_v3_backup``
    and any future sibling without this file having to keep its own list of the
    names ``app/core/constants.py`` derives.
    """
    rows = client.request('GET', f'/_cat/indices/{index_base}*?format=json&h=index')
    if not isinstance(rows, list):
        return []
    names = {row['index'] for row in rows if isinstance(row, dict) and row.get('index')}
    return sorted(names)


def scroll_documents(client: _Client, index: str) -> list:
    """Read every document in ``index`` as ``(doc_id, source)`` pairs via the scroll API."""
    documents = []
    body = {'size': _SCROLL_PAGE, 'query': {'match_all': {}}, 'sort': ['_doc']}
    response = client.request('POST', f'/{index}/_search?scroll={_SCROLL_KEEPALIVE}', body)
    scroll_id = response.get('_scroll_id')
    try:
        while True:
            hits = response.get('hits', {}).get('hits', [])
            if not hits:
                break
            for hit in hits:
                documents.append((hit['_id'], hit.get('_source') or {}))
            if not scroll_id:
                break
            response = client.request(
                'POST', '/_search/scroll', {'scroll': _SCROLL_KEEPALIVE, 'scroll_id': scroll_id}
            )
            scroll_id = response.get('_scroll_id', scroll_id)
    finally:
        if scroll_id:
            # A leaked scroll context expires on its own — never fail the export over it.
            with contextlib.suppress(VoiceprintBackupError):
                client.request('DELETE', '/_search/scroll', {'scroll_id': [scroll_id]})
    return documents


def _restorable_settings(raw: dict) -> dict:
    """Filter a live index's settings down to the keys a ``PUT /<index>`` accepts."""
    index_settings = (raw or {}).get('index', {})
    kept = {key: index_settings[key] for key in _RESTORABLE_SETTINGS if key in index_settings}
    return {'index': kept} if kept else {}


def _alias_target(client: _Client, index_base: str) -> dict:
    """Describe which concrete index the read alias points at, if it is an alias at all."""
    if not client.exists(f'/_alias/{index_base}'):
        return {}
    mapping = client.request('GET', f'/_alias/{index_base}')
    targets = sorted(mapping) if isinstance(mapping, dict) else []
    if not targets:
        return {}
    return {'name': index_base, 'target': targets[0]}


def do_export(client: _Client, index_base: str, out) -> int:
    """Write the manifest line plus ``_bulk``-ready pairs for every speaker index.

    Returns:
        The total number of documents written.
    """
    indices = []
    bulk_lines = []
    total = 0
    for name in speaker_index_names(client, index_base):
        definition = client.request('GET', f'/{name}')
        meta = definition.get(name, {})
        documents = scroll_documents(client, name)
        total += len(documents)
        indices.append(
            {
                'name': name,
                'doc_count': len(documents),
                'digest': digest_documents(documents),
                'mappings': meta.get('mappings', {}),
                'settings': _restorable_settings(meta.get('settings', {})),
            }
        )
        for doc_id, source in sorted(documents, key=lambda pair: pair[0]):
            bulk_lines.append(json.dumps({'index': {'_index': name, '_id': doc_id}}))
            bulk_lines.append(json.dumps(source))

    manifest = {
        'format': FORMAT_NAME,
        'version': FORMAT_VERSION,
        # time.gmtime, not datetime.now(UTC): this file executes on the OpenSearch
        # container's python3, which is 3.9 — `datetime.UTC` landed in 3.11.
        'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'index_base': index_base,
        'alias': _alias_target(client, index_base),
        'indices': indices,
        'total_docs': total,
    }
    out.write(json.dumps(manifest) + '\n')
    for line in bulk_lines:
        out.write(line + '\n')
    return total


def read_manifest(stream) -> tuple:
    """Split an artifact into ``(manifest, bulk_lines)``, validating the format header."""
    first = stream.readline()
    if not first.strip():
        raise VoiceprintBackupError(
            'voiceprint artifact is empty — refusing to treat it as "no voiceprints"'
        )
    try:
        manifest = json.loads(first)
    except ValueError as exc:
        raise VoiceprintBackupError(
            f'voiceprint artifact has no manifest on line 1: {exc}'
        ) from exc
    if manifest.get('format') != FORMAT_NAME:
        raise VoiceprintBackupError(
            f'not a voiceprint artifact (format={manifest.get("format")!r})'
        )
    if manifest.get('version') != FORMAT_VERSION:
        raise VoiceprintBackupError(
            f'unsupported voiceprint artifact version {manifest.get("version")!r} '
            f'(this build reads version {FORMAT_VERSION})'
        )
    return manifest, [line for line in (raw.rstrip('\n') for raw in stream) if line]


def _bulk_load(client: _Client, lines: list) -> None:
    """Replay the artifact's action/source pairs through ``_bulk`` in bounded batches."""
    per_request = _BULK_DOCS_PER_REQUEST * 2
    for start in range(0, len(lines), per_request):
        batch = lines[start : start + per_request]
        response = client.request('POST', '/_bulk', '\n'.join(batch) + '\n')
        if response.get('errors'):
            failures = [
                item['index']['error']
                for item in response.get('items', [])
                if item.get('index', {}).get('error')
            ]
            raise VoiceprintBackupError(
                f'bulk restore reported {len(failures)} failure(s): {failures[:3]}'
            )


def do_import(client: _Client, stream) -> dict:
    """Recreate any missing speaker index, bulk-load the artifact, restore the alias.

    Returns:
        The artifact's manifest, so the caller can report what was expected.
    """
    manifest, lines = read_manifest(stream)
    for entry in manifest.get('indices', []):
        name = entry['name']
        if client.exists(f'/{name}'):
            continue
        body = {'mappings': entry.get('mappings', {})}
        settings = entry.get('settings') or {}
        if settings:
            body['settings'] = settings
        client.request('PUT', f'/{name}', body)
        print(f'   created index {name} from the backup mapping', file=sys.stderr)

    _bulk_load(client, lines)

    names = [entry['name'] for entry in manifest.get('indices', [])]
    if names:
        client.request('POST', '/' + ','.join(names) + '/_refresh')

    alias = manifest.get('alias') or {}
    if alias.get('name') and alias.get('target') and not client.exists('/_alias/' + alias['name']):
        client.request('PUT', f'/{alias["target"]}/_alias/{alias["name"]}')
        print(f'   restored alias {alias["name"]} -> {alias["target"]}', file=sys.stderr)
    return manifest


def do_verify(client: _Client, stream) -> list:
    """Re-read the live cluster and compare it to the artifact.

    Returns:
        A list of human-readable mismatches; empty means the restore is proven.
    """
    manifest, _ = read_manifest(stream)
    problems = []
    for entry in manifest.get('indices', []):
        name = entry['name']
        if not client.exists(f'/{name}'):
            problems.append(f'index {name} is missing after the restore')
            continue
        live = scroll_documents(client, name)
        if len(live) != entry['doc_count']:
            problems.append(
                f'index {name}: {len(live)} document(s) present, backup holds {entry["doc_count"]}'
            )
            continue
        live_digest = digest_documents(live)
        if live_digest != entry['digest']:
            problems.append(
                f'index {name}: content digest mismatch — live {live_digest}, backup {entry["digest"]}'
            )
    alias = manifest.get('alias') or {}
    if alias.get('name') and not client.exists('/_alias/' + alias['name']):
        problems.append(f'read alias {alias["name"]} is missing after the restore')
    return problems


def _parse_args(argv: list) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('export', 'import', 'verify'))
    parser.add_argument('--index-base', default='speakers')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=9200)
    return parser.parse_args(argv)


def main(argv: list) -> int:
    """Entry point. Returns a process exit code; never raises for an expected failure."""
    args = _parse_args(argv)
    client = _Client(args.host, args.port)
    try:
        if args.mode == 'export':
            total = do_export(client, args.index_base, sys.stdout)
            print(f'   exported {total} voiceprint document(s)', file=sys.stderr)
            return 0
        if args.mode == 'import':
            manifest = do_import(client, sys.stdin)
            print(
                f'   imported {manifest.get("total_docs", 0)} voiceprint document(s)',
                file=sys.stderr,
            )
            return 0
        problems = do_verify(client, sys.stdin)
        if problems:
            for problem in problems:
                print(f'   MISMATCH: {problem}', file=sys.stderr)
            return 1
        return 0
    except VoiceprintBackupError as exc:
        print(f'   ERROR: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
