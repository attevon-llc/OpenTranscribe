#!/usr/bin/env python3
"""A fake Gladia (cloud ASR) API v2 endpoint, for exercising cloud ASR without a vendor.

Everything except transcription itself is real when the app is pointed at this:
the upload is validated as real WAV audio (RIFF/WAVE header, fmt chunk, data
chunk duration), the submitted request (diarization/language/custom_vocabulary)
is recorded verbatim so a test can assert what the app actually sent, and the
job goes through a real "processing" → "done" poll cycle. Only the transcript
content is canned — that is exactly the part that needs a paid vendor account.

Why this and not a stub inside the app: mocks belong in test fixtures only
(the repo's own rule); this is an external HTTP server that
`GladiaProvider._base` (via `GLADIA_API_BASE_URL`) talks to, so nothing about
the app changes.

Usage:
    python scripts/mock-asr-server.py [--port 5198]

⚠️ Never run this as a bare host process in a context where a container also
wants port 5198 — it will bind the port and block the container from starting.
The container form (docker-compose.mock-asr.yml) is host-loopback-only,
exactly like scripts/mock-llm-server.py's own warning about this.

This validates real WAV audio structurally — it does NOT transcribe it. The
returned transcript is always the same canned utterances, reshaped from
backend/tests/fixtures/media/sample_transcript.json.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Scenario models, selected per-job via the `custom_vocabulary`-sibling field
# `?scenario=` query param on POST /v2/transcription (not a server-start env
# var): a request-level knob mirrors how a real test would drive Gladia
# per-job, and lets one running mock exercise several scenarios in one suite
# run without a container restart between them. MOCK_ASR_SCENARIO (env, read
# at server start) is the fallback default when a request doesn't specify one.
#
#   ok             normal job: processing → done, canned 2-speaker transcript
#   error          job status becomes "error" with a sanitized-looking message
#                  that itself contains something secret-shaped, to prove the
#                  app's error-sanitization strips it before it reaches logs/UI
#   malformed      200 "done" but `result.transcription` key is missing
#   upload-reject  POST /v2/upload always 400s, regardless of audio validity
SCENARIOS = ('ok', 'error', 'malformed', 'upload-reject')

# Overridable so the container form (which bind-mounts only the script, not
# the whole repo) can point at its own mount location; defaults to the
# repo-relative path for a bare host run.
FIXTURE_PATH = Path(
    os.environ.get('MOCK_ASR_FIXTURE_PATH')
    or (
        Path(__file__).resolve().parent.parent
        / 'backend'
        / 'tests'
        / 'fixtures'
        / 'media'
        / 'sample_transcript.json'
    )
)

# A rare, made-up token injected into the canned transcript text so a search
# test can assert this specific mock-transcribed file (and no other) is found.
DISTINCTIVE_TOKEN = 'Zylofenix'

# In-memory job/audio state. Fine for a test-only single-process server; never
# meant to survive a restart.
_UPLOADS: dict[str, bytes] = {}
_JOBS: dict[str, dict] = {}
_LAST_REQUEST: dict = {}


def _load_canned_utterances() -> list[dict]:
    """Reshape the shared transcript fixture into Gladia's utterances schema."""
    if not FIXTURE_PATH.is_file():
        # Minimal built-in fallback so the server still starts if the fixture
        # ever moves — but this should never be what a real test run sees.
        return [
            {
                'speaker': 0,
                'text': f'Fallback transcript, fixture missing. {DISTINCTIVE_TOKEN}.',
                'start': 0.0,
                'end': 1.0,
                'confidence': 0.9,
                'words': [],
            }
        ]

    data = json.loads(FIXTURE_PATH.read_text(encoding='utf-8'))
    segments = data.get('segments', [])
    speaker_index = {name: i for i, name in enumerate(sorted({s['speaker'] for s in segments}))}

    utterances = []
    for i, seg in enumerate(segments):
        text = seg['text']
        if i == len(segments) - 1:
            # Append the distinctive token to the LAST utterance so it is
            # unambiguous which segment a search hit should resolve to.
            text = f'{text} {DISTINCTIVE_TOKEN}.'
        utterances.append(
            {
                'speaker': speaker_index[seg['speaker']],
                'text': text,
                'start': seg['start'],
                'end': seg['end'],
                'confidence': seg.get('confidence', 0.9),
                'words': [
                    {
                        'word': w['word'],
                        'start': w['start'],
                        'end': w['end'],
                        'confidence': w.get('score', 0.9),
                    }
                    for w in seg.get('words', [])
                ],
            }
        )
    return utterances


CANNED_UTTERANCES = None  # populated in main() so a fixture read error doesn't kill import


def _parse_wav(body: bytes) -> dict | None:
    """Validate RIFF/WAVE structure and return {sample_rate, channels, duration}.

    Returns None if `body` is not a well-formed WAV file. Deliberately hand-rolled
    (stdlib `wave` module raises on some real-world files with trailing chunks) —
    this only needs the RIFF/WAVE/fmt/data chunks, not full playback support.
    """
    if len(body) < 12 or body[0:4] != b'RIFF' or body[8:12] != b'WAVE':
        return None

    pos = 12
    fmt: dict | None = None
    data_len: int | None = None
    while pos + 8 <= len(body):
        chunk_id = body[pos : pos + 4]
        (chunk_size,) = struct.unpack('<I', body[pos + 4 : pos + 8])
        chunk_body_start = pos + 8
        if chunk_id == b'fmt ':
            if chunk_body_start + 16 > len(body):
                return None
            (
                _audio_format,
                channels,
                sample_rate,
                _byte_rate,
                _block_align,
                _bits_per_sample,
            ) = struct.unpack('<HHIIHH', body[chunk_body_start : chunk_body_start + 16])
            fmt = {'channels': channels, 'sample_rate': sample_rate}
        elif chunk_id == b'data':
            data_len = chunk_size
        pos = chunk_body_start + chunk_size + (chunk_size % 2)  # chunks are word-aligned

    if fmt is None or data_len is None or fmt['sample_rate'] <= 0:
        return None

    bytes_per_frame = max(fmt['channels'] * 2, 1)  # assume 16-bit PCM, matches sample fixtures
    duration = data_len / (fmt['sample_rate'] * bytes_per_frame)
    return {'channels': fmt['channels'], 'sample_rate': fmt['sample_rate'], 'duration': duration}


def _parse_multipart(body: bytes, content_type: str) -> dict[str, bytes]:
    """Extract named parts from a multipart/form-data body. Minimal, stdlib-only."""
    match = re.search(r'boundary=([^;]+)', content_type)
    if not match:
        return {}
    boundary = match.group(1).strip('"').encode()
    delimiter = b'--' + boundary
    parts: dict[str, bytes] = {}
    for chunk in body.split(delimiter):
        chunk = chunk.strip(b'\r\n')
        if not chunk or chunk == b'--':
            continue
        header_end = chunk.find(b'\r\n\r\n')
        if header_end == -1:
            continue
        headers = chunk[:header_end].decode('utf-8', errors='replace')
        content = chunk[header_end + 4 :]
        name_match = re.search(r'name="([^"]+)"', headers)
        if name_match:
            parts[name_match.group(1)] = content
    return parts


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):  # noqa: A002 - quieter console
        print(f'  {self.address_string()} {fmt % args}')

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _self_base(self) -> str:
        host = self.headers.get('Host') or f'localhost:{self.server.server_address[1]}'
        return f'http://{host}'

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip('/') == '/v2/live':
            self._json({'status': 'ok'})
            return

        if self.path == '/_mock/last-request':
            self._json(_LAST_REQUEST)
            return

        match = re.match(r'^/v2/transcription/([^/?]+)', self.path)
        if match:
            job_id = match.group(1)
            job = _JOBS.get(job_id)
            if job is None:
                self._json({'error': 'unknown job'}, 404)
                return
            job['polls'] += 1
            scenario = job['scenario']

            if scenario == 'error':
                self._json(
                    {
                        'id': job_id,
                        'status': 'error',
                        'error_message': (
                            'Mock provider failure — apikey=sk-not-a-real-secret-12345 '
                            'should never reach a log or the UI'
                        ),
                    }
                )
                return

            if job['polls'] < 2:
                self._json({'id': job_id, 'status': 'processing'})
                return

            if scenario == 'malformed':
                # 200 "done" but the expected result.transcription key is absent.
                self._json({'id': job_id, 'status': 'done', 'result': {}})
                return

            self._json(
                {
                    'id': job_id,
                    'status': 'done',
                    'result': {
                        'transcription': {
                            'utterances': CANNED_UTTERANCES,
                            'languages': [job.get('language') or 'en'],
                        }
                    },
                }
            )
            return

        self._json({'error': 'not found'}, 404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length else b''

        if self.path.rstrip('/') == '/v2/upload':
            content_type = self.headers.get('Content-Type', '')
            parts = _parse_multipart(raw, content_type)
            audio = parts.get('audio')

            scenario = os.environ.get('MOCK_ASR_SCENARIO', 'ok')
            if 'scenario=upload-reject' in self.path or scenario == 'upload-reject':
                self._json({'error': 'Missing audio file'}, 400)
                return

            if audio is None:
                self._json({'error': 'Missing audio file'}, 400)
                return

            wav_info = _parse_wav(audio)
            if wav_info is None:
                self._json({'error': 'Invalid or unsupported audio format'}, 400)
                return

            upload_id = uuid.uuid4().hex
            _UPLOADS[upload_id] = audio
            _LAST_REQUEST['upload'] = {
                'bytes': len(audio),
                'channels': wav_info['channels'],
                'sample_rate': wav_info['sample_rate'],
                'duration': round(wav_info['duration'], 3),
            }
            self._json({'audio_url': f'{self._self_base()}/audio/{upload_id}'})
            return

        if self.path.rstrip('/') == '/v2/transcription':
            try:
                body = json.loads(raw or b'{}')
            except ValueError:
                body = {}

            query_scenario = None
            if '?' in self.path:
                q = self.path.split('?', 1)[1]
                for kv in q.split('&'):
                    if kv.startswith('scenario='):
                        query_scenario = kv.split('=', 1)[1]
            scenario = query_scenario or os.environ.get('MOCK_ASR_SCENARIO', 'ok')
            if scenario not in SCENARIOS:
                scenario = 'ok'

            _LAST_REQUEST['transcription'] = {
                'audio_url': body.get('audio_url'),
                'diarization': body.get('diarization'),
                'language': body.get('language'),
                'detect_language': body.get('detect_language'),
                'custom_vocabulary': body.get('custom_vocabulary'),
            }

            job_id = uuid.uuid4().hex
            _JOBS[job_id] = {
                'polls': 0,
                'scenario': scenario,
                'language': body.get('language'),
            }
            self._json({'result_url': f'{self._self_base()}/v2/transcription/{job_id}'})
            return

        self._json({'error': 'not found'}, 404)


def main() -> None:
    global CANNED_UTTERANCES

    parser = argparse.ArgumentParser(description=__doc__)
    # Derived from the shared MOCK_ASR_PORT convention, never a bare literal
    # (the readiness-probe-target audit detector enforces this repo-wide).
    parser.add_argument('--port', type=int, default=int(os.environ.get('MOCK_ASR_PORT', '5198')))
    # Loopback by DEFAULT, same rationale as scripts/mock-llm-server.py: this
    # server accepts any audio with no authentication, so a bare host run must
    # not expose it to the LAN. The container opts into 0.0.0.0 explicitly in
    # docker-compose.mock-asr.yml, behind a 127.0.0.1-only published port.
    parser.add_argument('--host', default='127.0.0.1')
    args = parser.parse_args()

    CANNED_UTTERANCES = _load_canned_utterances()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f'Mock Gladia (cloud ASR) listening on http://{args.host}:{args.port}')
    print(f'  Base URL for containers: http://{args.host}:{args.port}')
    print(f'  Scenario (env default): {os.environ.get("MOCK_ASR_SCENARIO", "ok")}')
    print(
        '  ⚠️  Validates real WAV audio structurally (RIFF/WAVE/fmt/data chunks) '
        'but does NOT transcribe it — the returned transcript is canned from '
        'backend/tests/fixtures/media/sample_transcript.json.'
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nstopped')


if __name__ == '__main__':
    main()
