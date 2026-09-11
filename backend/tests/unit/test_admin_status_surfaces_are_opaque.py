"""#914 STEP 4 (Tier 2): admin/super_admin status surfaces must never echo a
caught exception's raw text, even though the reader is an admin.

Sentinels are planted via the REAL service functions named in the plan
(``_perform_backup_local``, ``perform_mirror``, ``sweep_ldap``,
``ensure_neural_search_bootstrap``) -- never a hand-written result dict, which
would pass even if the service itself still leaked. Each test then reads the
result back through the real status-endpoint FUNCTION. Two of the four
(``get_backup_status``, and the neural-search endpoint's collaborators) are
called directly rather than through ``client.get(...)``: ``get_backup_status``
calls ``db.close()`` mid-handler (issue tracked in
``test_endpoint_session_lifetime.py``), which would leave the shared test
``db_session`` closed for any assertion after a real HTTP round trip through
the same TestClient-overridden session -- this repo's own precedent for that
endpoint calls the function directly for the same reason.
"""

from __future__ import annotations

import logging
import subprocess

#: A pg_dump stderr shape carrying PGHOST/PGUSER/the database name.
PG_DUMP_SENTINEL = "PGHOST=db-internal.corp PGUSER=postgres_admin dbname=opentranscribe_prod"
#: A host filesystem / mount path.
PATH_SENTINEL = "/mnt/nas/secret-share"
#: The directory URL + bind DN shape #914 named for LDAP.
DIRECTORY_URL_SENTINEL = "ldaps://dc01.corp.internal:636"
BIND_DN_SENTINEL = "cn=svc-bind,ou=svc,dc=corp"
#: A local-mode ML service failure detail.
ML_SENTINEL = "ml_commons index unreachable at /var/lib/opensearch/ml_cache"


# --------------------------------------------------------------------------- #
# 1. backup_service._perform_backup_local -> GET /admin/backup/status
# --------------------------------------------------------------------------- #


def test_backup_status_never_leaks_pg_dump_stderr(db_session, admin_user, monkeypatch, caplog):
    from app.api.endpoints import backup_settings
    from app.services import backup_service

    def _raise_pg_dump(*_args, **_kwargs):
        raise subprocess.CalledProcessError(
            1, ["pg_dump"], stderr=f"pg_dump: error: connection failed: {PG_DUMP_SENTINEL}".encode()
        )

    monkeypatch.setattr(backup_service, "run_pg_dump", _raise_pg_dump)

    cfg = backup_service.get_settings(db_session)
    cfg = {**cfg, "encrypt": False, "include_opensearch": False}
    # destination_status() must report writable; the coded default under
    # DEFAULT_BACKUP_DESTINATION is not guaranteed to exist in this sandbox.
    import tempfile

    cfg["destination"] = tempfile.mkdtemp(prefix="ot-backup-status-test-")

    with caplog.at_level(logging.ERROR):
        result = backup_service._perform_backup_local(cfg, db_session)

    assert result["ok"] is False
    assert PG_DUMP_SENTINEL not in result["error"]
    assert PG_DUMP_SENTINEL in caplog.text

    status = backup_settings.get_backup_status(db=db_session, current_user=admin_user)

    assert PG_DUMP_SENTINEL not in str(status.last_result)


# --------------------------------------------------------------------------- #
# 2. media_mirror_engine.perform_mirror -> GET /admin/backup/mirror
# --------------------------------------------------------------------------- #


def test_media_mirror_status_never_leaks_a_host_path(db_session, admin_user, monkeypatch, caplog):
    from app.api.endpoints import media_mirror_settings
    from app.services import media_mirror_engine

    def _raise_build_destination(*_args, **_kwargs):
        raise ValueError(f"Media mirror destination {PATH_SENTINEL!r} is not a writable mount")

    monkeypatch.setattr(media_mirror_engine, "_build_destination", _raise_build_destination)

    with caplog.at_level(logging.ERROR):
        result = media_mirror_engine.perform_mirror(db_session)

    assert result["ok"] is False
    assert PATH_SENTINEL not in result["error"]
    assert PATH_SENTINEL in caplog.text

    response = media_mirror_settings.get_mirror_settings(db=db_session, current_user=admin_user)

    assert PATH_SENTINEL not in str(response.last_result)


# --------------------------------------------------------------------------- #
# 3. directory_sync_service.sweep_ldap -> GET /admin/directory-sync/status
# --------------------------------------------------------------------------- #


def test_directory_sync_status_never_leaks_the_directory_url(
    db_session, admin_user, monkeypatch, caplog
):
    from ldap3.core.exceptions import LDAPBindError

    from app.api.endpoints import directory_sync_settings
    from app.services import directory_sync_service as dss

    def _sentinel_config():
        from app.auth.ldap_auth import LdapConfig

        return LdapConfig(
            enabled=True,
            server="dc01.corp.internal",
            port=636,
            use_ssl=True,
            bind_dn=BIND_DN_SENTINEL,
            bind_password="irrelevant",  # noqa: S106 - test fixture, not a real secret
            search_base="dc=corp",
        )

    def _raise_bind_failure(*_args, **_kwargs):
        raise LDAPBindError(
            f"Can't contact LDAP server: {DIRECTORY_URL_SENTINEL} "
            f"(bind DN {BIND_DN_SENTINEL} rejected)"
        )

    monkeypatch.setattr("app.auth.ldap_auth._bind_service_account", _raise_bind_failure)
    monkeypatch.setattr(dss.LdapConfig, "from_db", classmethod(lambda cls, db: _sentinel_config()))

    cfg = dss.SweepConfig(dry_run=True, max_disables=5)

    with caplog.at_level(logging.ERROR):
        result = dss.sweep_ldap(db_session, cfg)

    assert result["status"] == "directory_unavailable"
    assert DIRECTORY_URL_SENTINEL not in result["error"]
    assert BIND_DN_SENTINEL not in result["error"]
    assert DIRECTORY_URL_SENTINEL in caplog.text

    dss.record_result(db_session, result)

    status = directory_sync_settings.get_directory_sync_status(
        db=db_session, current_user=admin_user
    )

    assert DIRECTORY_URL_SENTINEL not in str(status.last_result)
    assert BIND_DN_SENTINEL not in str(status.last_result)


# --------------------------------------------------------------------------- #
# 4. search.neural_bootstrap.ensure_neural_search_bootstrap -> GET /search/models/neural/status
# --------------------------------------------------------------------------- #


class _FakeRedis:
    """Minimal in-memory stand-in -- avoids depending on a reachable Redis."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def get(self, key: str):
        val = self._store.get(key)
        return val.encode() if val is not None else None

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._store[key] = value

    def incr(self, key: str) -> int:
        current = int(self._store.get(key, "0")) + 1
        self._store[key] = str(current)
        return current

    def expire(self, *_args, **_kwargs) -> None:
        pass

    def delete(self, *keys: str) -> None:
        for k in keys:
            self._store.pop(k, None)


def test_neural_search_status_never_leaks_ml_service_detail(monkeypatch, caplog):
    """Exercises the real chain: run_bootstrap_tick -> ensure_neural_search_bootstrap
    (the plan's named target) -> bootstrap_status, which is what
    GET /search/models/neural/status's ``bootstrap`` field returns verbatim (a
    direct, unformatted passthrough) -- so proving this chain opaque proves the
    endpoint field opaque too, with no need to also mock every unrelated
    OpenSearch/ML-Commons collaborator that endpoint touches just to keep it
    from crashing without a live cluster.

    ``_managed_embedding_mode`` and ``get_ml_model_service`` are left real
    (cheap, no I/O of their own) so the natural local-mode branch is taken.
    """
    from app.services.search import neural_bootstrap

    fake_redis = _FakeRedis()
    monkeypatch.setattr("app.core.redis.get_redis", lambda: fake_redis)
    monkeypatch.setattr(neural_bootstrap, "neural_search_ready", lambda: False)
    monkeypatch.setattr(neural_bootstrap, "_text_only_chunk_files_count", lambda: 0)

    def _raise_local_mode(*_args, **_kwargs):
        raise RuntimeError(ML_SENTINEL)

    monkeypatch.setattr(neural_bootstrap, "_bootstrap_local_mode", _raise_local_mode)

    with caplog.at_level(logging.ERROR):
        tick_result = neural_bootstrap.run_bootstrap_tick()

    assert tick_result["state"] == "degraded"
    assert ML_SENTINEL not in tick_result["last_error"]
    assert ML_SENTINEL in caplog.text

    status = neural_bootstrap.bootstrap_status()
    assert ML_SENTINEL not in str(status.get("last_error"))
