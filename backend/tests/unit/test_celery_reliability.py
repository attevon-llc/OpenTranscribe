"""Celery broker and migration-lock reliability (#284 Phase 1).

A1.1 — no `visibility_timeout` was configured, so the Redis broker kept kombu's 3600s
default. The transcription tasks are `acks_late=True`, so ANY run over one hour was
redelivered to a second worker and the same file was transcribed twice, on the GPU,
concurrently. A 4-hour media limit makes that reachable with an ordinary file.

A1.4 — `pg_advisory_lock(42)` was taken on a pooled connection that was then returned
and the engine disposed BEFORE `command.upgrade()` ran, so migrations executed unlocked
and concurrent replicas could race Alembic. The matching unlock used a fresh connection,
which is a no-op since advisory locks are session-scoped.

A1.6 — `engine.dispose()` on every `task_postrun` tore down the pool after each task.

A1.17 — kombu will not connect over `rediss://` without an SSL context.
"""

from __future__ import annotations

import inspect

import pytest

# ── A1.1 visibility_timeout ──────────────────────────────────────────────────────


def test_visibility_timeout_is_configured():
    from app.core.celery import celery_app

    opts = celery_app.conf.broker_transport_options
    assert "visibility_timeout" in opts, (
        "without this the Redis broker uses kombu's 3600s default and any transcription "
        "over an hour is redelivered -> duplicate GPU work"
    )


def test_visibility_timeout_exceeds_the_longest_supported_job():
    """Media is capped at 4h; the timeout must clear that with headroom."""
    from app.core.celery import celery_app

    timeout = celery_app.conf.broker_transport_options["visibility_timeout"]
    assert timeout >= 4 * 3600, f"{timeout}s does not cover a 4-hour job"


def test_visibility_timeout_is_not_absurdly_high():
    """Crash recovery keys off DB status, so an inflated value only delays requeue."""
    from app.core.celery import celery_app

    assert celery_app.conf.broker_transport_options["visibility_timeout"] <= 86400


# ── A1.6 pooling ─────────────────────────────────────────────────────────────────


def test_postrun_does_not_dispose_the_engine():
    """Disposing per task defeats pooling — every task paid a fresh handshake."""
    from app.core.celery import close_session_after_task

    source = inspect.getsource(close_session_after_task)
    body = source.split('"""')[-1]
    assert "dispose()" not in body


def test_post_fork_dispose_is_retained():
    """The dispose that DOES matter — a forked child must not inherit sockets."""
    from app.core.celery import init_worker_process

    assert "dispose()" in inspect.getsource(init_worker_process)


# ── A1.8 prefetch ────────────────────────────────────────────────────────────────


def test_global_prefetch_stays_gpu_safe():
    """The GPU worker must never hold a task it cannot start; queues override per-worker."""
    from app.core.celery import celery_app

    assert celery_app.conf.worker_prefetch_multiplier == 1


# ── A1.17 TLS Redis ──────────────────────────────────────────────────────────────


def test_no_ssl_options_on_plain_redis():
    from app.core.celery import celery_app

    assert celery_app.conf.broker_use_ssl in (None, {})


def test_ssl_options_are_set_for_rediss(run_in_clean_process):
    """config.py builds rediss:// under REDIS_USE_TLS; kombu refuses without a context."""
    out = run_in_clean_process(
        "from app.core.config import settings;"
        "from app.core.celery import celery_app;"
        "print(str(settings.CELERY_BROKER_URL).split('://')[0],"
        "celery_app.conf.broker_use_ssl, celery_app.conf.redis_backend_use_ssl, sep='|')",
        REDIS_USE_TLS="true",
    )
    scheme, broker_ssl, backend_ssl = out.split("|")

    assert scheme == "rediss"
    assert "CERT_REQUIRED" in broker_ssl
    assert "CERT_REQUIRED" in backend_ssl


# ── A1.4 migration advisory lock ─────────────────────────────────────────────────


def test_lock_is_held_on_a_dedicated_connection_across_the_upgrade():
    from app.db.migrations import run_migrations

    source = inspect.getsource(run_migrations)
    acquire_at = source.index("pg_advisory_lock(42)")
    upgrade_at = source.index('command.upgrade(config, "head")')
    dispose_at = source.index("engine.dispose()")

    assert acquire_at < upgrade_at, "the lock must be taken before migrations run"
    assert "lock_conn" in source, "the lock must be held on a dedicated connection"
    # The engine.dispose() that used to drop the lock must not touch the lock engine.
    assert dispose_at < upgrade_at
    assert "lock_engine = create_engine" in source


def test_unlock_uses_the_same_session():
    """Advisory locks are session-scoped — unlocking from a fresh connection is a no-op."""
    from app.db.migrations import run_migrations

    source = inspect.getsource(run_migrations)
    unlock_at = source.index("pg_advisory_unlock(42)")
    tail = source[unlock_at - 300 : unlock_at]

    assert "unlock_engine = create_engine" not in source, (
        "unlocking from a fresh engine cannot release a session-scoped lock"
    )
    assert "lock_conn" in tail


def test_migration_startup_gate_exists():
    from app.core.config import settings

    assert hasattr(settings, "RUN_MIGRATIONS_ON_STARTUP")
    assert settings.RUN_MIGRATIONS_ON_STARTUP is True, "self-host default must still migrate"


def test_readiness_asserts_schema_when_migrations_are_gated_off():
    """A replica that didn't migrate must prove the DB is at head before taking traffic."""
    from app.main import readiness_check

    source = inspect.getsource(readiness_check)
    assert "RUN_MIGRATIONS_ON_STARTUP" in source
    assert "get_current_head" in source
    assert 'checks.get("schema"' in source


@pytest.mark.parametrize("flag", ["true", "false"])
def test_gate_is_env_driven(run_in_clean_process, flag):
    out = run_in_clean_process(
        "from app.core.config import settings;print(settings.RUN_MIGRATIONS_ON_STARTUP)",
        RUN_MIGRATIONS_ON_STARTUP=flag,
    )
    assert out == str(flag == "true")
