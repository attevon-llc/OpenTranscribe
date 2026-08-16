"""Characterization tests for ``app/services/asr/factory.py`` (issue #445).

``test_asr_config_forwarding.py`` already pins the AWS ``access_key_id`` forwarding
regression (#300). This module covers the fallback / error-handling / matching logic
that file does not touch:

1. **``create_for_user``'s broad ``except Exception``** (L558-563) catches EVERYTHING
   coming out of the DB-config lookup, not just the documented ``ValueError`` that
   ``create_from_db_config`` raises on a decrypt failure. An unrelated bug in that path
   (e.g. an ``AttributeError``) is swallowed the same way, logged as a warning, and the
   caller silently gets the env/local fallback instead of a crash.
2. **A fixed gap**: when the ``active_asr_config_id`` setting points at a config row that
   does not exist (or exists but is neither owned nor shared), the ORM query simply returns
   ``None``. This used to fall out of the ``if cfg:`` guard with **no log line at all** —
   not even the warning the exception path produces. ``create_for_user`` now logs a
   ``logger.warning`` in that ``else`` branch before falling back to env/local, matching the
   severity of the sibling exception path.
3. ``get_model_capabilities``'s substring-match tie-break: for two same-length catalog
   model ids that are both substrings of an ambiguous ``model_id``, the earlier entry in
   ``ASR_PROVIDER_CATALOG["local"]["models"]`` wins, because ``sorted(..., reverse=True)``
   is stable and preserves the catalog's declaration order among ties.
4. ``create_from_db_config``'s ``provider == "local"`` bypass never touches
   ``decrypt_api_key`` even when a garbage ``api_key`` is present on the row, and the
   ``access_key_id`` decrypt-or-raise runs unconditionally — for every provider, not just
   "aws" — so a stray corrupt ``access_key_id`` on a non-AWS config still raises.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import cast

import pytest

from app import models
from app.services.asr.deepgram_provider import DeepgramProvider
from app.services.asr.factory import ASR_PROVIDER_CATALOG
from app.services.asr.factory import ASRProviderFactory
from app.services.asr.local_provider import LocalASRProvider
from app.utils.encryption import encrypt_api_key

FACTORY_LOGGER = "app.services.asr.factory"


class _FakeQuery:
    """Minimal SQLAlchemy query stand-in returning a canned row."""

    def __init__(self, result):
        self._result = result

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._result


class _RaisingQuery:
    """A query object whose ``.filter()`` raises something that is NOT ``ValueError`` —
    an unrelated bug, not a documented decrypt failure."""

    def filter(self, *args, **kwargs):
        raise AttributeError("boom: unrelated attribute-access bug, not a decrypt failure")


class _FakeDB:
    """Dispatches ``UserSetting`` to one canned row, ``UserASRSettings`` to another
    query object (canned row or a raising one, depending on the test)."""

    def __init__(self, setting, asr_query):
        self._setting = setting
        self._asr_query = asr_query

    def query(self, model):
        name = getattr(model, "__name__", str(model))
        if name == "UserASRSettings":
            return self._asr_query
        return _FakeQuery(self._setting)


# --------------------------------------------------------------------------
# 1. Broad except swallows an unrelated bug, not just the documented ValueError
# --------------------------------------------------------------------------


def test_create_for_user_falls_back_on_unexpected_exception_type(caplog):
    """The except clause is a bare ``except Exception`` — it must catch an
    ``AttributeError`` from the lookup path exactly as it catches the documented
    decrypt ``ValueError``, and it must log a warning while doing so."""
    db = _FakeDB(
        setting=SimpleNamespace(setting_value="42"),
        asr_query=_RaisingQuery(),
    )

    with caplog.at_level(logging.WARNING, logger=FACTORY_LOGGER):
        provider = ASRProviderFactory.create_for_user(user_id=1, db=db)

    assert isinstance(provider, LocalASRProvider)
    warnings = [r for r in caplog.records if r.name == FACTORY_LOGGER]
    assert any("Failed to load ASR config" in r.message for r in warnings)
    assert any("boom: unrelated attribute-access bug" in r.message for r in warnings)


# --------------------------------------------------------------------------
# 2. A missing/unmatched config id now logs a warning before falling back
# --------------------------------------------------------------------------


def test_create_for_user_logs_a_warning_when_config_id_matches_nothing(
    db_session, normal_user, caplog, monkeypatch
):
    """``active_asr_config_id`` pointing at a row that doesn't exist (or isn't owned/shared)
    makes the DB query return ``None``. That must now log exactly one warning — mentioning
    the offending config id — before falling back to env/local, matching the observability
    of the sibling exception path above."""
    monkeypatch.delenv("ASR_PROVIDER", raising=False)

    nonexistent_id = 999_999_999
    setting = models.UserSetting(
        user_id=normal_user.id,
        setting_key="active_asr_config_id",
        setting_value=str(nonexistent_id),
    )
    db_session.add(setting)
    db_session.commit()

    with caplog.at_level(logging.WARNING, logger=FACTORY_LOGGER):
        provider = ASRProviderFactory.create_for_user(user_id=normal_user.id, db=db_session)

    assert isinstance(provider, LocalASRProvider)
    warnings = [
        r for r in caplog.records if r.name == FACTORY_LOGGER and r.levelno >= logging.WARNING
    ]
    assert len(warnings) == 1
    assert str(nonexistent_id) in warnings[0].message
    assert "not found" in warnings[0].message or "not accessible" in warnings[0].message


# --------------------------------------------------------------------------
# 3. get_model_capabilities substring tie-break: stable sort keeps catalog order
# --------------------------------------------------------------------------


def _catalog_model(provider_id: str, model_id: str) -> dict:
    for m in ASR_PROVIDER_CATALOG[provider_id]["models"]:
        if m["id"] == model_id:
            return cast(dict, m)
    raise AssertionError(f"{model_id!r} not found in {provider_id!r} catalog")


def test_get_model_capabilities_tiebreak_prefers_earlier_catalog_entry_large_v(monkeypatch):
    """``large-v3`` and ``large-v2`` are both 8 characters and both catalog entries are
    declared with ``large-v3`` first. A ``model_id`` containing both substrings must
    resolve via ``large-v3`` — the earlier entry survives the stable-sort tie-break."""
    large_v3 = _catalog_model("local", "large-v3")
    large_v2 = _catalog_model("local", "large-v2")
    assert len(large_v3["id"]) == len(large_v2["id"])  # precondition: a genuine length tie

    # Distinguishing marker neither entry carries today, so we can see which one matched.
    monkeypatch.setitem(large_v3, "languages", 111)
    monkeypatch.setitem(large_v2, "languages", 222)

    caps = ASRProviderFactory.get_model_capabilities("local", "org/large-v3-and-large-v2-mix")

    assert caps["languages"] == 111


def test_get_model_capabilities_tiebreak_prefers_earlier_catalog_entry_base_tiny(monkeypatch):
    """Same tie-break, second pair: ``base``/``tiny`` are both 4 characters and ``base`` is
    declared first."""
    base = _catalog_model("local", "base")
    tiny = _catalog_model("local", "tiny")
    assert len(base["id"]) == len(tiny["id"])

    monkeypatch.setitem(base, "languages", 333)
    monkeypatch.setitem(tiny, "languages", 444)

    # Literally contains both "base" (chars 0-3) and "tiny" (chars 4-7) as substrings.
    caps = ASRProviderFactory.get_model_capabilities("local", "basetiny-custom")

    assert caps["languages"] == 333


# --------------------------------------------------------------------------
# 4. create_from_db_config: local bypass skips decryption; access_key_id decrypt
#    is unconditional on provider
# --------------------------------------------------------------------------


def test_local_provider_bypass_never_attempts_decryption(monkeypatch):
    """A ``provider == "local"`` row with a garbage ``api_key`` must return a working
    ``LocalASRProvider`` without ever calling ``decrypt_api_key`` — proven by making the
    decrypt function raise if it is invoked at all."""

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("decrypt_api_key must not run for provider == 'local'")

    monkeypatch.setattr("app.utils.encryption.decrypt_api_key", _must_not_be_called)

    cfg = SimpleNamespace(
        id=1,
        provider="local",
        model_name=None,
        base_url=None,
        region=None,
        api_key="not-valid-ciphertext-at-all",
        access_key_id=None,
    )

    provider = ASRProviderFactory.create_from_db_config(cfg)

    assert isinstance(provider, LocalASRProvider)
    assert provider.provider_name == "local"


def test_access_key_id_decrypt_or_raise_runs_for_a_non_aws_provider():
    """``access_key_id`` decryption is gated only on ``getattr(cfg, "access_key_id", None)``
    being truthy — not on ``cfg.provider == "aws"``. A corrupt ``access_key_id`` on a
    deepgram config must raise the same ``ValueError`` an AWS config would."""
    cfg = SimpleNamespace(
        id=9,
        provider="deepgram",
        model_name="nova-3",
        base_url=None,
        region=None,
        api_key=encrypt_api_key("real-deepgram-key"),
        access_key_id="not-a-valid-ciphertext",
    )

    with pytest.raises(ValueError, match="access key ID"):
        ASRProviderFactory.create_from_db_config(cfg)


def test_access_key_id_is_decrypted_but_discarded_for_a_non_aws_provider():
    """When the stray ``access_key_id`` DOES decrypt cleanly, decryption still runs (it is
    forwarded into ``create_from_config``) but the resulting provider silently ignores it —
    ``DeepgramProvider`` has no parameter for it."""
    cfg = SimpleNamespace(
        id=10,
        provider="deepgram",
        model_name="nova-3",
        base_url=None,
        region=None,
        api_key=encrypt_api_key("real-deepgram-key"),
        access_key_id=encrypt_api_key("stray-access-key-id"),
    )

    provider = ASRProviderFactory.create_from_db_config(cfg)

    assert isinstance(provider, DeepgramProvider)
    assert provider.provider_name == "deepgram"
    assert provider._api_key == "real-deepgram-key"
