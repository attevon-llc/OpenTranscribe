"""Offline-guard behavior for SpeakerAttributeService.load_models.

Fourth sibling site from the reranker-offline-guard investigation. This one
already had a bound (`app/tasks/speaker_attribute_task.py::_load_models_with_
timeout`, issue #622), so the only gap was the missing `local_files_only`
kwarg — relying on the `HF_HUB_OFFLINE` env var alone is unreliable here for
the same import-order reason documented in `app/utils/hf_hub_offline.py`.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.speaker_attribute_service import SpeakerAttributeService


@pytest.fixture
def service():
    return SpeakerAttributeService()


def _install_fake_transformers(monkeypatch, *, fe_kwargs: dict, model_kwargs: dict):
    mock_fe_cls = MagicMock()

    def _fe_from_pretrained(model_name, **kwargs):
        fe_kwargs.update(kwargs)
        return MagicMock()

    mock_fe_cls.from_pretrained.side_effect = _fe_from_pretrained

    mock_model_cls = MagicMock()

    def _model_from_pretrained(model_name, **kwargs):
        model_kwargs.update(kwargs)
        instance = MagicMock()
        instance.eval.return_value = None
        return instance

    mock_model_cls.from_pretrained.side_effect = _model_from_pretrained

    mock_transformers = MagicMock()
    mock_transformers.Wav2Vec2FeatureExtractor = mock_fe_cls
    mock_transformers.Wav2Vec2ForSequenceClassification = mock_model_cls

    monkeypatch.setitem(__import__("sys").modules, "transformers", mock_transformers)


@pytest.mark.unit
class TestOfflineKwargThreadedToWav2Vec2:
    def test_local_files_only_passed_to_both_loaders_when_offline_requested(
        self, service, monkeypatch
    ):
        monkeypatch.setenv("HF_HUB_OFFLINE", "1")
        fe_kwargs: dict = {}
        model_kwargs: dict = {}
        _install_fake_transformers(monkeypatch, fe_kwargs=fe_kwargs, model_kwargs=model_kwargs)

        service.load_models()

        assert service._model_loaded is True
        assert fe_kwargs.get("local_files_only") is True
        assert model_kwargs.get("local_files_only") is True

    def test_local_files_only_not_forced_when_online(self, service, monkeypatch):
        monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
        fe_kwargs: dict = {}
        model_kwargs: dict = {}
        _install_fake_transformers(monkeypatch, fe_kwargs=fe_kwargs, model_kwargs=model_kwargs)

        service.load_models()

        assert "local_files_only" not in fe_kwargs
        assert "local_files_only" not in model_kwargs
