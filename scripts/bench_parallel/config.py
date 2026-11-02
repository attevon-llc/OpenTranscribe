"""Environment-derived settings shared by every part of the parallel benchmark."""

from __future__ import annotations

import os

BACKEND_URL = os.environ.get('BENCHMARK_BACKEND_URL', 'http://localhost:5174')
POLL_INTERVAL = 5.0  # seconds between status polls


def _load_redis_url() -> str:
    """Build Redis URL from BENCHMARK_REDIS_URL env var, then .env, then sensible default."""
    explicit = os.environ.get('BENCHMARK_REDIS_URL', '')
    if explicit:
        return explicit
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


REDIS_URL = _load_redis_url()
POLL_TIMEOUT = int(os.environ.get('BENCHMARK_POLL_TIMEOUT', '14400'))  # 4 hours default
VRAM_SAMPLE_INTERVAL = 2.0  # seconds between nvidia-smi samples
STATS_SAMPLE_INTERVAL = 2.0  # seconds between docker-stats CPU/RAM samples
GPU_DEVICE_ID = 0  # host GPU to monitor (overridable via --gpu-id)
# Container/DB names are overridable so the bench stack (otbench-*) and the dev
# stack (opentranscribe-*) never get confused. The orchestrator sets these.
DB_CONTAINER = os.environ.get('BENCHMARK_DB_CONTAINER', 'opentranscribe-postgres')
DB_USER = 'postgres'
DB_NAME = 'opentranscribe'
