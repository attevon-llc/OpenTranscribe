"""Target guard, identities and small helpers for the concurrent load test (locust-free).

``locustfile.py`` and the persona modules import this module, and the backend's unit tests import
it without Locust (Locust monkey-patches the standard library with gevent on import, so it must
never load inside the backend test process). The route table lives in ``routes.py`` for the same
reason.

The guard reuses the isolation rule of ``scripts/seed-query-review.sh``: the target is a
``./opentr.sh --fresh NAME`` stack, its port comes from ``.fresh/NAME.offset``, and the shared
dev stack's ports are refused. There is no override and no remote mode.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
BASE_BACKEND_PORT = 5174  # BACKEND_PORT in .env.example; a --fresh stack adds its offset
LIVE_DEV_PORTS = frozenset({5174, 5175, 5176, 5177, 5178, 5179, 5180})

PASSWORD = 'loadtest-password'
EMAIL_DOMAIN = 'loadtest.invalid'
ADMIN_INDEX = 1  # perf-1: the seed's one admin
HEAVY_INDEX = 2  # perf-2: owns 20,000 files in the default shape
ORG_ADMIN_INDEX = 3  # perf-3: the admin of the seed's first organization
FIRST_POOL_INDEX = 4

# Each persona that writes, chats or listens gets its own block of identities, so two personas
# never edit the same person's files. Shares of perf-4 .. perf-<seeded users>, in this order.
POOL_SHARES: tuple[tuple[str, float], ...] = (
    ('viewer', 0.55),
    ('editor', 0.15),
    ('chat', 0.10),
    ('ws', 0.10),
    ('pipeline', 0.10),
)
MIN_SEEDED_USERS = FIRST_POOL_INDEX + len(POOL_SHARES) - 1

# A small real recording (10 s, 16 kHz mono PCM) the editor and the pipeline profile upload.
SAMPLE_AUDIO = REPO_ROOT / 'backend' / 'tests' / 'fixtures' / 'media' / 'sample_short.wav'


class NotIsolatedError(RuntimeError):
    """The load target is not a ``--fresh`` stack."""


@dataclass(frozen=True)
class Target:
    fresh: str
    offset: int
    base_url: str

    @property
    def compose_project(self) -> str:
        """The compose project ``./opentr.sh --fresh NAME`` runs the stack under."""
        return f'otfresh-{self.fresh}'


def resolve_target(
    fresh: str | None, host: str | None = None, repo_root: Path = REPO_ROOT
) -> Target:
    """Return the ``--fresh NAME`` stack's backend URL, or raise ``NotIsolatedError``.

    Args:
        fresh: The ``./opentr.sh --fresh`` deployment name.
        host: A ``--host`` the operator also passed; it must equal the derived URL.
        repo_root: Checkout holding ``.fresh/`` (overridable for tests).
    """
    if not fresh:
        raise NotIsolatedError(
            '--fresh NAME is required: load runs only against an isolated '
            '`./opentr.sh start dev --fresh NAME --port-offset N` stack.'
        )
    offset_file = repo_root / '.fresh' / f'{fresh}.offset'
    if not offset_file.is_file():
        raise NotIsolatedError(f"No --fresh deployment '{fresh}' ({offset_file} is missing).")
    raw = offset_file.read_text().strip()
    if not raw.isdigit() or int(raw) == 0:
        raise NotIsolatedError(f"'{fresh}' has port offset {raw!r}; it would share the dev ports.")
    port = BASE_BACKEND_PORT + int(raw)
    if port in LIVE_DEV_PORTS:
        raise NotIsolatedError(f'Port {port} belongs to the shared dev stack.')
    base_url = f'http://localhost:{port}'
    if host and host.rstrip('/') not in (base_url, f'http://127.0.0.1:{port}'):
        raise NotIsolatedError(f"--host {host} is not the '{fresh}' stack's backend ({base_url}).")
    return Target(fresh=fresh, offset=int(raw), base_url=base_url)


def identity(index: int) -> str:
    """The seeded login for user ``index`` (``perf-<index>@loadtest.invalid``)."""
    return f'perf-{index}@{EMAIL_DOMAIN}'


def pool_bounds(persona: str, seeded_users: int) -> tuple[int, int]:
    """First and last seeded user index (inclusive) of ``persona``'s identity block."""
    if seeded_users < MIN_SEEDED_USERS:
        raise ValueError(f'the load test needs a seed of at least {MIN_SEEDED_USERS} users')
    span = seeded_users - FIRST_POOL_INDEX + 1
    lo = FIRST_POOL_INDEX
    for i, (name, share) in enumerate(POOL_SHARES):
        size = max(1, int(span * share))
        if i == len(POOL_SHARES) - 1:
            size = seeded_users - lo + 1  # the last block takes the rounding remainder
        if name == persona:
            return lo, lo + size - 1
        lo += size
    raise KeyError(f'unknown identity pool {persona!r}')


def pool_index(persona: str, n: int, seeded_users: int) -> int:
    """Map the n-th simulated user (0-based) of ``persona`` onto its identity block."""
    lo, hi = pool_bounds(persona, seeded_users)
    return lo + n % (hi - lo + 1)


def cookie_value(jar: Iterable[Any], name: str) -> str:
    """The value of cookie ``name`` in a ``http.cookiejar.CookieJar`` (``''`` when absent)."""
    for cookie in jar:
        if cookie.name == name:
            return str(cookie.value)
    return ''


def ws_url(base_url: str) -> str:
    """The live-notification socket of the backend at ``base_url``."""
    return base_url.replace('http://', 'ws://', 1).replace('https://', 'wss://', 1) + '/api/ws'


def unique_wav(audio: bytes, marker: str) -> bytes:
    """``audio`` with a RIFF ``JUNK`` chunk holding ``marker`` appended, so no two uploads match.

    RIFF readers skip chunks they do not know (the WAVE format allows ``JUNK`` for padding), so
    the recording is unchanged; its size and last bytes differ, which is what the upload's
    content fingerprint samples. The RIFF size field at bytes 4..8 is updated.
    """
    if audio[:4] != b'RIFF' or audio[8:12] != b'WAVE':
        raise ValueError('not a RIFF/WAVE file')
    payload = marker.encode()
    if len(payload) % 2:
        payload += b'\0'  # chunks are word-aligned
    chunk = b'JUNK' + struct.pack('<I', len(payload)) + payload
    body = audio + chunk
    return body[:4] + struct.pack('<I', len(body) - 8) + body[8:]
