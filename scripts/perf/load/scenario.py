"""Route table and target guard for the concurrent load test (locust-free).

``locustfile.py`` imports this module for every path it calls, and
``backend/tests/unit/test_load_scenario_routes.py`` imports it without Locust (Locust
monkey-patches the standard library with gevent on import, so it must never load inside the
backend test process). Keeping the routes here is what lets that test check each one against
``backend/openapi.json``.

The guard reuses the isolation rule of ``scripts/seed-query-review.sh``: the target is a
``./opentr.sh --fresh NAME`` stack, its port comes from ``.fresh/NAME.offset``, and the shared
dev stack's ports are refused. There is no override.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
BASE_BACKEND_PORT = 5174  # BACKEND_PORT in .env.example; a --fresh stack adds its offset
LIVE_DEV_PORTS = frozenset({5174, 5175, 5176, 5177, 5178, 5179, 5180})

PASSWORD = 'loadtest-password'
EMAIL_DOMAIN = 'loadtest.invalid'
ADMIN_INDEX = 1  # perf-1: the seed's one admin
HEAVY_INDEX = 2  # perf-2: owns 20,000 files in the default shape
FIRST_ORDINARY_INDEX = 3

# (method, OpenAPI path template). Every request the scenario makes goes through one of these.
ROUTES: dict[str, tuple[str, str]] = {
    'login': ('POST', '/api/auth/login'),
    'session': ('GET', '/api/auth/session'),
    'me': ('GET', '/api/auth/me'),
    'files.list': ('GET', '/api/files'),
    'files.metadata_filters': ('GET', '/api/files/metadata-filters'),
    'files.owners': ('GET', '/api/files/owners'),
    'files.detail': ('GET', '/api/files/{file_uuid}'),
    'files.segments': ('GET', '/api/files/{file_uuid}/segments'),
    'files.prepare': ('POST', '/api/files/prepare'),
    'files.delete': ('DELETE', '/api/files/{file_uuid}'),
    'files.purge': ('DELETE', '/api/files/{file_uuid}/purge'),
    'comments.list': ('GET', '/api/comments/files/{file_uuid}/comments'),
    'comments.create': ('POST', '/api/comments/files/{file_uuid}/comments'),
    'comments.delete': ('DELETE', '/api/comments/{comment_uuid}'),
    'tags.list': ('GET', '/api/tags'),
    'collections.list': ('GET', '/api/collections'),
    'search': ('GET', '/api/search'),
    'speakers.for_filter': ('GET', '/api/speakers'),
    'profiles.list': ('GET', '/api/speaker-profiles/profiles'),
    'tasks.list': ('GET', '/api/tasks'),
    'admin.stats': ('GET', '/api/admin/stats'),
    'system.stats': ('GET', '/api/system/stats'),
}

SORT_FIELDS = ('upload_time', 'duration', 'file_size', 'completed_at')


class NotIsolatedError(RuntimeError):
    """The load target is not a ``--fresh`` stack."""


@dataclass(frozen=True)
class Target:
    fresh: str
    offset: int
    base_url: str


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


def ordinary_index(n: int, seeded_users: int) -> int:
    """Map the n-th simulated ordinary user (0-based) onto perf-3..perf-<seeded_users>."""
    span = seeded_users - FIRST_ORDINARY_INDEX + 1
    if span < 1:
        raise ValueError(f'the seed needs at least {FIRST_ORDINARY_INDEX} users')
    return FIRST_ORDINARY_INDEX + (n % span)


def path(name: str, **params: str) -> str:
    """Fill a route template, e.g. ``path("files.detail", file_uuid=u)``."""
    return ROUTES[name][1].format(**params)
