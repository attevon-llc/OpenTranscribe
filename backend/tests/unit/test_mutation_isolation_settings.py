"""The app honours the per-module env the parallel runner exports (EFFA, design 2a)."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import redis

_LIB = Path(__file__).resolve().parents[3] / "scripts" / "lib"


def test_settings_follow_postgres_db_and_the_redis_index(monkeypatch):
    from app.core.config import Settings

    monkeypatch.syspath_prepend(str(_LIB))
    sys.modules.pop("mutation_isolation", None)
    iso = importlib.import_module("mutation_isolation")
    parts = {"REDIS_HOST": "cache.example", "REDIS_PORT": "6400", "REDIS_PASSWORD": "fake-pw"}
    # An explicit REDIS_URL/CELERY_* would win over the REDIS_* parts; blank them like an unset var.
    env = {
        "REDIS_URL": "",
        "CELERY_BROKER_URL": "",
        "CELERY_RESULT_BACKEND": "",
        "POSTGRES_DB": "ot_mut_spans",
        **parts,
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("REDIS_URL", iso.derive_redis_url({**os.environ}, 5))

    settings = Settings()
    assert settings.DATABASE_URL.endswith("/ot_mut_spans")
    kwargs = redis.Redis.from_url(settings.REDIS_URL).connection_pool.connection_kwargs
    assert kwargs["db"] == 5
    assert (kwargs["host"], kwargs["port"], kwargs["password"]) == (
        "cache.example",
        6400,
        "fake-pw",
    )


def test_no_module_under_mutation_builds_a_redis_client_from_host_kwargs():
    """`redis.Redis(host=...)` ignores the URL's DB index, so it would escape the isolation."""
    backend = Path(__file__).resolve().parents[2]
    script = (backend.parent / "scripts" / "run-mutation-tests.sh").read_text()
    import re

    paths = re.findall(r'\[\w+\]="(app/[^"]+\.py)"', script)
    assert len(paths) >= 10
    offenders = [
        p for p in paths if re.search(r"redis\.Redis\(\s*host=", (backend / p).read_text())
    ]
    assert offenders == []
