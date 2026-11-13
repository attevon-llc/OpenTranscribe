"""The pure plan and URL derivation of scripts/lib/mutation_isolation.py (EFFA, design 2a)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parents[3] / "scripts" / "lib"
KEYS = [
    "spans", "password_policy", "security", "dependencies", "lifecycle_gates",
    "external_token", "lockout", "lockout_store", "lockout_admin", "session",
]  # fmt: skip


@pytest.fixture
def iso(monkeypatch):
    monkeypatch.syspath_prepend(str(_LIB))
    sys.modules.pop("mutation_isolation", None)
    return importlib.import_module("mutation_isolation")


def test_unique_names_and_indexes_for_all_modules(iso):
    planned = iso.plan(KEYS, 16)
    assert len({r.db for r in planned.values()}) == len(KEYS)
    indexes = [r.redis_index for r in planned.values()]
    assert len(set(indexes)) == len(KEYS)
    assert all(2 <= i < 16 for i in indexes)
    assert all(iso.DB_NAME_RE.match(r.db) for r in planned.values())


def test_plan_is_alphabetical_so_it_is_stable_between_runs(iso):
    assert list(iso.plan(list(reversed(KEYS)), 16)) == sorted(KEYS)


def test_plan_refuses_more_modules_than_redis_databases(iso):
    with pytest.raises(iso.IsolationError, match="databases"):
        iso.plan([f"m{chr(97 + i)}" for i in range(15)], 16)


def test_a_key_that_is_not_a_legal_database_name_is_refused(iso):
    with pytest.raises(iso.IsolationError):
        iso.plan(["spans; DROP DATABASE postgres"], 16)


def test_derive_redis_url_changes_only_the_index(iso):
    env = {"REDIS_HOST": "cache.example", "REDIS_PORT": "6400", "REDIS_PASSWORD": "s3cret"}
    got = iso.derive_redis_url(env, 7)
    assert got == "redis://:s3cret@cache.example:6400/7"
    tls = iso.derive_redis_url({**env, "REDIS_USE_TLS": "true"}, 3)
    assert tls == "rediss://:s3cret@cache.example:6400/3"


def test_masked_url_never_carries_the_password(iso):
    masked = iso.mask_url("redis://:s3cret@cache.example:6400/7")
    assert "s3cret" not in masked
    assert masked == "redis://***@cache.example:6400/7"
