"""Settings both benchmark packages (bench_parallel, e2e_bench) derive from the environment."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import ModuleType

REPO_ENV_FILE = Path(__file__).resolve().parent.parent / '.env'


def _stack_target() -> ModuleType:
    sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
    import stack_target

    return stack_target


def backend_url() -> str:
    """BENCHMARK_BACKEND_URL, else the backend port the resolved stack publishes ('' if none)."""
    explicit = os.environ.get('BENCHMARK_BACKEND_URL', '')
    if explicit:
        return explicit
    port = _stack_target().observed_port('BACKEND_PORT')
    return f'http://localhost:{port}' if port else ''


def postgres_container() -> str:
    """BENCHMARK_DB_CONTAINER, else the resolved stack's running postgres ('' if none)."""
    return os.environ.get('BENCHMARK_DB_CONTAINER') or _stack_target().observed_container(
        'postgres'
    )


def load_redis_url(env_file: Path = REPO_ENV_FILE) -> str:
    """Build the Redis URL: BENCHMARK_REDIS_URL, else .env's password and the resolved stack's port."""
    explicit = os.environ.get('BENCHMARK_REDIS_URL', '')
    if explicit:
        return explicit
    password = ''
    port = ''
    try:
        with env_file.open() as f:
            for line in f:
                line = line.strip()
                if line.startswith('REDIS_PASSWORD='):
                    password = line.split('=', 1)[1].split('#', 1)[0].strip().strip('"\'')
    except OSError:
        pass
    # The port is the stack's PUBLISHED one (an exported REDIS_PORT, else the resolved project),
    # never the .env value: a --fresh stack publishes at an offset the .env does not know.
    port = _stack_target().observed_port('REDIS_PORT')
    if password:
        return f'redis://:{password}@localhost:{port}/0'
    return f'redis://localhost:{port}/0'
