"""Environment-derived settings shared by every part of the parallel benchmark."""

from __future__ import annotations

import os

from bench_common import load_redis_url

BACKEND_URL = os.environ.get('BENCHMARK_BACKEND_URL', 'http://localhost:5174')
POLL_INTERVAL = 5.0  # seconds between status polls


REDIS_URL = load_redis_url()
POLL_TIMEOUT = int(os.environ.get('BENCHMARK_POLL_TIMEOUT', '14400'))  # 4 hours default
VRAM_SAMPLE_INTERVAL = 2.0  # seconds between nvidia-smi samples
STATS_SAMPLE_INTERVAL = 2.0  # seconds between docker-stats CPU/RAM samples
GPU_DEVICE_ID = 0  # host GPU to monitor (overridable via --gpu-id)
# Container/DB names are overridable so the bench stack (otbench-*) and the dev
# stack (opentranscribe-*) never get confused. The orchestrator sets these.
DB_CONTAINER = os.environ.get('BENCHMARK_DB_CONTAINER', 'opentranscribe-postgres')
DB_USER = 'postgres'
DB_NAME = 'opentranscribe'
