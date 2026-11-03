"""URL resolution for the optional owner/app database role split (config + Alembic env)."""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.db.migration_connection import AUDIT_MAINTENANCE_GUC
from app.db.migration_connection import migration_connect_args
from app.db.migration_connection import migration_database_url

_BASE = {"POSTGRES_HOST": "db.example", "POSTGRES_PORT": "5432", "POSTGRES_DB": "d"}


def _settings(**kw):
    kw.setdefault("DATABASE_URL", "")
    return Settings(POSTGRES_USER="owner", POSTGRES_PASSWORD="ownerpw", **_BASE, **kw)


def test_default_is_single_role_and_both_urls_are_the_owner():
    s = _settings()
    assert s.DATABASE_URL == "postgresql://owner:ownerpw@db.example:5432/d"
    assert s.MIGRATION_DATABASE_URL == s.DATABASE_URL
    assert s.db_role_split_requested is False


def test_app_user_builds_runtime_url_from_app_credentials_and_keeps_owner_for_migrations():
    s = _settings(POSTGRES_APP_USER="ot_app", POSTGRES_APP_PASSWORD="p@ss/word")
    assert s.DATABASE_URL == "postgresql://ot_app:p%40ss%2Fword@db.example:5432/d"
    assert s.MIGRATION_DATABASE_URL == "postgresql://owner:ownerpw@db.example:5432/d"
    assert s.db_role_split_requested is True


def test_explicit_database_url_with_app_user_is_a_configuration_error():
    with pytest.raises(ValueError, match="MIGRATION_DATABASE_URL"):
        _settings(POSTGRES_APP_USER="ot_app", DATABASE_URL="postgresql://x:y@h/d")


def test_explicit_urls_on_both_sides_request_the_split():
    s = Settings(
        POSTGRES_APP_USER="ot_app",
        DATABASE_URL="postgresql://app:a@h/d",
        MIGRATION_DATABASE_URL="postgresql://own:o@h/d",
    )
    assert s.DATABASE_URL.startswith("postgresql://app:")
    assert s.MIGRATION_DATABASE_URL.startswith("postgresql://own:")
    assert s.db_role_split_requested is True


def test_explicit_database_url_alone_is_used_for_migrations_too():
    s = Settings(DATABASE_URL="postgresql://x:y@elsewhere/d", MIGRATION_DATABASE_URL="")
    assert s.MIGRATION_DATABASE_URL == "postgresql://x:y@elsewhere/d"
    assert s.db_role_split_requested is False


def _clean_db_env(monkeypatch):
    for key in ("DATABASE_URL", "MIGRATION_DATABASE_URL", "POSTGRES_APP_USER", "POSTGRES_SSLMODE"):
        monkeypatch.delenv(key, raising=False)


def test_alembic_env_uses_the_migration_url_with_ssl_mode(monkeypatch):
    """F1: Alembic used to hand-build its URL from POSTGRES_*, ignoring DATABASE_URL and SSL."""
    _clean_db_env(monkeypatch)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql://own:p%25w@h:5432/d")
    monkeypatch.setenv("POSTGRES_SSLMODE", "require")
    assert migration_database_url() == "postgresql://own:p%25w@h:5432/d?sslmode=require"
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql://o:x@h/d?sslmode=verify-full")
    assert migration_database_url().endswith("sslmode=verify-full")
    monkeypatch.setenv("POSTGRES_SSLMODE", "disable")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql://o:x@h/d")
    assert "sslmode" not in migration_database_url()


def test_migration_url_follows_postgres_env_set_after_import(monkeypatch):
    """Per-call POSTGRES_* must retarget Alembic; the import-time settings must not pin it."""
    _clean_db_env(monkeypatch)
    monkeypatch.setenv("POSTGRES_SSLMODE", "disable")
    for key, value in {
        "POSTGRES_HOST": "elsewhere.example",
        "POSTGRES_PORT": "6543",
        "POSTGRES_USER": "someone",
        "POSTGRES_PASSWORD": "pw",
        "POSTGRES_DB": "other_db",
    }.items():
        monkeypatch.setenv(key, value)
    assert migration_database_url() == "postgresql://someone:pw@elsewhere.example:6543/other_db"


def test_explicit_migration_url_still_beats_postgres_env(monkeypatch):
    _clean_db_env(monkeypatch)
    monkeypatch.setenv("POSTGRES_SSLMODE", "disable")
    monkeypatch.setenv("POSTGRES_HOST", "ignored.example")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql://own:o@pinned.example/d")
    assert migration_database_url() == "postgresql://own:o@pinned.example/d"


def test_alembic_config_and_env_source_use_the_migration_url():
    from pathlib import Path

    from app.db.migrations import get_alembic_config

    env_source = (Path(__file__).resolve().parents[2] / "alembic" / "env.py").read_text()
    assert "migration_database_url()" in env_source
    assert "POSTGRES_USER" not in env_source, "env.py must not hand-build a URL again"
    url = get_alembic_config().get_main_option("sqlalchemy.url")
    assert url is not None
    assert url.replace("%%", "%") == migration_database_url()


def test_migration_connections_set_the_maintenance_switch():
    assert migration_connect_args() == {"options": f"-c {AUDIT_MAINTENANCE_GUC}=on"}
