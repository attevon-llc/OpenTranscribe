"""``DEPLOYMENT_MODE`` is a closed vocabulary, so a new value cannot disarm the lite guards (#628).

The readers compare against exact strings (``services/asr/factory.py`` raises for ``"lite"``).
Before the validator, ``DEPLOYMENT_MODE=demo`` loaded fine and fell into every reader's
``else`` branch: the factory resolved ``LocalASRProvider`` on an image with no Whisper. The
parametrized tests below iterate the vocabulary itself rather than values someone thought of,
so adding a member without teaching the guards about it fails here.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.config import settings
from app.core.exceptions import ASRConfigurationError
from app.core.settings_deployment import VALID_DEPLOYMENT_MODES
from app.services.asr.factory import _guard_local_allowed
from app.utils.lite_mode import is_lite_deployment


@pytest.mark.parametrize("value", ["demo", "cloud", "lite-demo"])
def test_an_unknown_mode_is_refused_at_startup(monkeypatch, value):
    monkeypatch.setenv("DEPLOYMENT_MODE", value)

    with pytest.raises(ValidationError, match="DEPLOYMENT_MODE must be one of"):
        Settings()


@pytest.mark.parametrize(("raw", "expected"), [(" Lite ", "lite"), ("FULL", "full")])
def test_a_known_mode_is_normalised(monkeypatch, raw, expected):
    monkeypatch.setenv("DEPLOYMENT_MODE", raw)

    assert expected == Settings().DEPLOYMENT_MODE


@pytest.mark.parametrize("mode", sorted(VALID_DEPLOYMENT_MODES - {"full"}))
def test_every_mode_but_full_refuses_local_asr(monkeypatch, mode):
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", mode)

    with pytest.raises(ASRConfigurationError):
        _guard_local_allowed()
    assert is_lite_deployment() is True


def test_full_allows_local_asr(monkeypatch):
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "full")

    _guard_local_allowed()
    assert is_lite_deployment() is False
