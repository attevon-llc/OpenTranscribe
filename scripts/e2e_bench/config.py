"""Backend, Redis and database settings for the end-to-end pipeline benchmark."""

import os

DEFAULT_BACKEND_URL = 'http://localhost:5174'
POLL_INTERVAL = 3.0
POLL_TIMEOUT = 3600  # 60 minutes max (3hr files need time)


def _load_redis_url() -> str:
    """Build Redis URL from BENCHMARK_REDIS_URL env var, then .env, then sensible default."""
    explicit = os.environ.get('BENCHMARK_REDIS_URL', '')
    if explicit:
        return explicit
    # Try to read password and port from project .env
    env_file = os.path.join(os.path.dirname(__file__), '..', '.env')
    password = ''
    port = '5177'
    try:
        with open(env_file) as f:
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


DEFAULT_REDIS_URL = _load_redis_url()
INTER_ITERATION_WAIT = 10  # seconds between iterations for cleanup

DB_CONTAINER = 'opentranscribe-postgres'
DB_USER = 'postgres'
DB_NAME = 'opentranscribe'
