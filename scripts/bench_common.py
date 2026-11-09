"""Settings both benchmark packages (bench_parallel, e2e_bench) derive from the environment."""

from __future__ import annotations

import os
from pathlib import Path

REPO_ENV_FILE = Path(__file__).resolve().parent.parent / '.env'


def load_redis_url(env_file: Path = REPO_ENV_FILE) -> str:
    """Build the Redis URL: BENCHMARK_REDIS_URL, then the project .env, then localhost defaults."""
    explicit = os.environ.get('BENCHMARK_REDIS_URL', '')
    if explicit:
        return explicit
    password = ''
    port = '5177'
    try:
        with env_file.open() as f:
            for line in f:
                line = line.strip()
                if line.startswith('REDIS_PASSWORD='):
                    password = line.split('=', 1)[1].split('#', 1)[0].strip().strip('"\'')
                elif line.startswith('REDIS_PORT=') and 'already set' not in line:
                    # Strip inline comments + whitespace (e.g. "5177  # debug").
                    port = line.split('=', 1)[1].split('#', 1)[0].strip().strip('"\'')
    except OSError:
        pass
    if password:
        return f'redis://:{password}@localhost:{port}/0'
    return f'redis://localhost:{port}/0'
