"""v434 data migration + detection-arm consistency (issue #1225).

``v434`` rewrites a stored ``pyannote`` speaker-detection choice to ``provider`` (Automatic)
for exactly the users for whom #1225 would otherwise turn a silently-ignored choice into
every file failing: no pyannote.ai key, and local transcription. Everyone else keeps their
choice. Like the other ``test_v43x`` suites this EXECUTES the revision's SQL, twice, because
the startup runner re-runs a revision over its own output.

The reset is replayed only over this test's own users (``AND s.user_id = ANY(:ids)``), so
even inside the rolled-back savepoint it never rewrites another account's row.
"""

from __future__ import annotations

import importlib.util
import uuid as uuid_pkg
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text

REVISION = "v434_reset_unusable_pyannote_source"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"
_SOURCE_KEY = "transcription_diarization_source"


def _revision_module():
    spec = importlib.util.spec_from_file_location(REVISION, _REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def local_env(monkeypatch):
    """A deployment whose users with no active config transcribe locally."""
    monkeypatch.delenv("ASR_PROVIDER", raising=False)


def _new_user(conn) -> int:
    return int(
        conn.execute(
            text(
                'INSERT INTO "user" (email, hashed_password, is_active, is_superuser, '
                "role, auth_type) VALUES (:e, 'x', true, false, 'user', 'local') RETURNING id"
            ),
            {"e": f"v434_{uuid_pkg.uuid4().hex[:10]}@example.com"},
        ).scalar()
    )


def _setting(conn, user_id: int, key: str, value: str) -> None:
    conn.execute(
        text("INSERT INTO user_setting (user_id, setting_key, setting_value) VALUES (:u, :k, :v)"),
        {"u": user_id, "k": key, "v": value},
    )


def _asr_config(conn, user_id: int, provider: str, *, active: bool) -> None:
    config_id = int(
        conn.execute(
            text(
                "INSERT INTO user_asr_settings (user_id, name, provider, model_name, is_active) "
                "VALUES (:u, :n, :p, 'm', true) RETURNING id"
            ),
            {"u": user_id, "n": f"{provider}-{uuid_pkg.uuid4().hex[:6]}", "p": provider},
        ).scalar()
    )
    if active:
        _setting(conn, user_id, "active_asr_config_id", str(config_id))


def _pyannote_key(conn, user_id: int, api_key: str | None = "v3:ciphertext") -> None:
    conn.execute(
        text(
            "INSERT INTO user_diarization_settings (user_id, name, provider, model_name, "
            "api_key, is_active) VALUES (:u, 'pyannote.ai', 'pyannote', 'precision-3', :k, true)"
        ),
        {"u": user_id, "k": api_key},
    )


def _user(conn, source: str, *, key: bool = False, asr: str | None = None) -> int:
    """A user with a stored source, optionally a pyannote.ai key and an active ASR config."""
    user_id = _new_user(conn)
    _setting(conn, user_id, _SOURCE_KEY, source)
    if key:
        _pyannote_key(conn, user_id)
    if asr is not None:
        _asr_config(conn, user_id, asr, active=True)
    return user_id


def _source(conn, user_id: int) -> str:
    return str(
        conn.execute(
            text("SELECT setting_value FROM user_setting WHERE user_id = :u AND setting_key = :k"),
            {"u": user_id, "k": _SOURCE_KEY},
        ).scalar()
    )


def _reset(conn, module, user_ids: list[int]) -> None:
    conn.execute(
        text(module.RESET_SQL + " AND s.user_id = ANY(:ids)"),
        {"env_local": module.env_asr_is_local(), "ids": user_ids},
    )


def test_v434_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    heads = set(scripts.get_heads())

    assert rev.down_revision == "v433_add_requested_transcription_options"
    assert len(heads) == 1, "two heads mean two branches both claimed a revision number"
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_the_revision_is_vendor_neutral():
    source = _REVISION_PATH.read_text().lower()
    assert "clerk" not in source
    assert "stripe" not in source


def test_only_unusable_pyannote_choices_are_reset(db_session, local_env):
    module = _revision_module()
    conn = db_session.connection()
    try:
        no_key_env_local = _user(conn, "pyannote")
        no_key_local_config = _user(conn, "pyannote", asr="local")
        with_key_local = _user(conn, "pyannote", key=True)
        no_key_cloud = _user(conn, "pyannote", asr="deepgram")
        empty_key_local = _user(conn, "pyannote")
        _pyannote_key(conn, empty_key_local, api_key="")
        automatic = _user(conn, "provider")
        ids = [
            no_key_env_local,
            no_key_local_config,
            with_key_local,
            no_key_cloud,
            empty_key_local,
            automatic,
        ]

        _reset(conn, module, ids)
        _reset(conn, module, ids)  # idempotent: nothing left to match

        assert _source(conn, no_key_env_local) == "provider"
        assert _source(conn, no_key_local_config) == "provider"
        assert _source(conn, empty_key_local) == "provider"
        # Left alone: pyannote.ai now runs (and bills) as chosen ...
        assert _source(conn, with_key_local) == "pyannote"
        # ... and cloud transcription keeps #1204's visible refusal.
        assert _source(conn, no_key_cloud) == "pyannote"
        assert _source(conn, automatic) == "provider"
    finally:
        db_session.rollback()


def test_a_cloud_env_default_keeps_the_choice(db_session, monkeypatch):
    """A user with no active config transcribes with a usable cloud ``ASR_PROVIDER``."""
    monkeypatch.setenv("ASR_PROVIDER", "deepgram")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "fixture-not-a-key")
    module = _revision_module()
    assert module.env_asr_is_local() is False
    conn = db_session.connection()
    try:
        user_id = _user(conn, "pyannote")
        _reset(conn, module, [user_id])
        assert _source(conn, user_id) == "pyannote"
    finally:
        db_session.rollback()


@pytest.mark.parametrize(
    ("provider", "key", "local"),
    [
        (None, None, True),
        ("local", None, True),
        ("deepgram", "fixture-not-a-key", False),
        # The runtime's rule: a cloud ASR_PROVIDER with no key falls back to local.
        ("deepgram", None, True),
        ("google", None, False),
    ],
)
def test_env_fallback_matches_the_runtime_rule(monkeypatch, provider, key, local):
    from app.services.asr.factory import ASRProviderFactory

    for var in ("ASR_PROVIDER", "DEEPGRAM_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    if provider is not None:
        monkeypatch.setenv("ASR_PROVIDER", provider)
    if key is not None:
        monkeypatch.setenv("DEEPGRAM_API_KEY", key)

    assert _revision_module().env_asr_is_local() is local
    assert (ASRProviderFactory.resolve_config(None)[0] == "local") is local


def test_the_downgrade_is_a_no_op(db_session, local_env):
    module = _revision_module()
    conn = db_session.connection()
    try:
        user_id = _user(conn, "pyannote")
        _reset(conn, module, [user_id])
        module.downgrade()
        assert _source(conn, user_id) == "provider"
    finally:
        db_session.rollback()


def test_an_unusable_choice_makes_detection_stamp_v433(db_session, local_env):
    from app.db.migrations import _detect_schema_version

    conn = db_session.connection()
    try:
        _user(conn, "pyannote")
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        db_session.rollback()

    assert detected == "v433_add_requested_transcription_options"


def test_with_no_unusable_choice_left_detection_stamps_v434_or_later(db_session, local_env):
    """The whole table is reset inside the savepoint (rolled back), so the result does not
    depend on what the deployment's own users stored."""
    from tests.unit._migration_detection import assert_detected_at_or_after

    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.RESET_SQL), {"env_local": module.env_asr_is_local()})
        assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)
    finally:
        db_session.rollback()
