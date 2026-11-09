"""Backend, Redis and database settings for the end-to-end pipeline benchmark."""

from bench_common import load_redis_url

DEFAULT_BACKEND_URL = 'http://localhost:5174'
POLL_INTERVAL = 3.0
POLL_TIMEOUT = 3600  # 60 minutes max (3hr files need time)


DEFAULT_REDIS_URL = load_redis_url()
INTER_ITERATION_WAIT = 10  # seconds between iterations for cleanup

DB_CONTAINER = 'opentranscribe-postgres'
DB_USER = 'postgres'
DB_NAME = 'opentranscribe'
