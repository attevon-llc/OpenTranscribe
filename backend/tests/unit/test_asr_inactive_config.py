"""``UserASRSettings.is_active`` is honoured, not just stored (#1207).

The column was written on create/update and serialized, but nothing read it: an inactive
configuration that the user's ``active_asr_config_id`` still pointed at kept running. An
inactive row now behaves exactly like a dangling pointer (env / local fallback) for job
selection and as a speaker detector, and ``POST /set-active`` refuses it.

Fake sessions only, so these need no Postgres.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import HTTPException
from fastapi import Request
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.endpoints import asr_settings
from app.schemas.asr_settings import SetActiveASRConfigRequest
from app.services.asr.deepgram_provider import DeepgramProvider
from app.services.asr.factory import ASRProviderFactory
from app.services.asr.local_provider import LocalASRProvider
from app.services.diarization.factory import get_diarizer_asr_config
from app.utils.encryption import encrypt_api_key

FACTORY_LOGGER = "app.services.asr.factory"


class _Query:
    def __init__(self, result):
        self._result = result

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._result


class _DB:
    """``UserSetting`` -> the pointer row, ``UserASRSettings`` -> the config row."""

    def __init__(self, cfg):
        self._setting = SimpleNamespace(setting_value="7")
        self._cfg = cfg

    def query(self, model):
        name = getattr(model, "__name__", str(model))
        return _Query(self._cfg if name == "UserASRSettings" else self._setting)


def _session(cfg) -> Session:
    return cast(Session, _DB(cfg))


def _deepgram(*, is_active: bool):
    return SimpleNamespace(
        id=7,
        uuid="0190a1b2-0000-7000-8000-000000000007",
        provider="deepgram",
        model_name="nova-3",
        api_key=encrypt_api_key("dg-secret-key"),
        access_key_id=None,
        base_url=None,
        region=None,
        is_active=is_active,
    )


@pytest.fixture(autouse=True)
def _deployment_default_is_local(monkeypatch):
    monkeypatch.delenv("ASR_PROVIDER", raising=False)
    monkeypatch.delenv("WHISPER_MODEL", raising=False)


def test_an_active_config_still_builds_its_provider():
    """The control: without it, 'always falls back to local' would pass the tests below."""
    provider = ASRProviderFactory.create_for_user(user_id=1, db=_session(_deepgram(is_active=True)))

    assert isinstance(provider, DeepgramProvider)


def test_an_inactive_config_falls_back_to_local_and_says_why(caplog):
    with caplog.at_level(logging.WARNING, logger=FACTORY_LOGGER):
        provider = ASRProviderFactory.create_for_user(
            user_id=1, db=_session(_deepgram(is_active=False))
        )

    assert isinstance(provider, LocalASRProvider)
    assert any("inactive" in r.message for r in caplog.records if r.name == FACTORY_LOGGER)


def test_an_inactive_config_falls_back_to_the_env_provider(monkeypatch):
    monkeypatch.setenv("ASR_PROVIDER", "deepgram")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "env-key")
    cfg = _deepgram(is_active=False)
    cfg.api_key = encrypt_api_key("the-inactive-configs-own-key")

    provider = ASRProviderFactory.create_for_user(user_id=1, db=_session(cfg))

    assert isinstance(provider, DeepgramProvider)
    assert provider._api_key == "env-key"


def test_planning_resolves_an_inactive_config_to_the_default_too():
    """``resolve_active_config`` feeds the pipeline plan; it must agree with the job path."""
    provider, _model, cfg = ASRProviderFactory.resolve_active_config(
        1, _session(_deepgram(is_active=False))
    )

    assert (provider, cfg) == ("local", None)


def _diarizer_lookup(cfg):
    return get_diarizer_asr_config(_session(cfg), 1, "0190a1b2-0000-7000-8000-000000000007")


def test_an_inactive_config_is_not_a_speaker_detector():
    active, inactive = _deepgram(is_active=True), _deepgram(is_active=False)

    assert _diarizer_lookup(active) is active
    assert _diarizer_lookup(inactive) is None


def _call_set_active():
    """``POST /set-active`` called directly, with just the arguments the handler reads."""
    body = SimpleNamespace(resolved_uuid=lambda: "0190a1b2-0000-7000-8000-000000000007")
    ctx = SimpleNamespace(user=SimpleNamespace(id=1))
    return asr_settings.set_active_asr_config(
        request=cast(Request, SimpleNamespace()),
        body=cast(SetActiveASRConfigRequest, body),
        db=_session(None),
        ctx=cast(RequestContext, ctx),
    )


def test_set_active_refuses_an_inactive_config(monkeypatch):
    monkeypatch.setattr(
        asr_settings, "_get_config_or_404", lambda *_a, **_k: _deepgram(is_active=False)
    )
    pointer_writes: list[int] = []
    monkeypatch.setattr(
        asr_settings,
        "_set_active_asr_configuration",
        lambda _db, _uid, cid: pointer_writes.append(cid),
    )

    with pytest.raises(HTTPException) as refused:
        _call_set_active()

    assert refused.value.status_code == 409
    assert cast(dict[str, str], refused.value.detail)["code"] == "asr_config_inactive"
    assert pointer_writes == []


def test_set_active_gets_past_the_inactive_check_for_an_active_config(monkeypatch):
    """The control for the refusal above: an active row reaches the switch assessment."""

    class _ReachedError(Exception):
        pass

    def _assess(*_a, **_k):
        raise _ReachedError

    monkeypatch.setattr(
        asr_settings, "_get_config_or_404", lambda *_a, **_k: _deepgram(is_active=True)
    )
    monkeypatch.setattr(asr_settings, "assess_asr_switch", _assess)

    with pytest.raises(_ReachedError):
        _call_set_active()
