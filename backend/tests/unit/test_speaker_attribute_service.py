"""
Unit tests for SpeakerAttributeService.

Tests cover service initialization, inference output format, and the
GENDER_ID2LABEL constant. All ML model calls are mocked so tests run on CPU
without downloading the actual HuggingFace model.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

import numpy as np
import pytest

from app.services.speaker_attribute_service import GENDER_ID2LABEL
from app.services.speaker_attribute_service import SpeakerAttributeService
from app.services.speaker_attribute_service import get_cached_attribute_service

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def service():
    """Return an un-loaded SpeakerAttributeService instance."""
    return SpeakerAttributeService()


@pytest.fixture
def loaded_service():
    """Return a SpeakerAttributeService with mocked model already loaded."""
    svc = SpeakerAttributeService()
    svc._model_loaded = True
    svc._model = MagicMock()
    svc._feature_extractor = MagicMock()
    return svc


# ---------------------------------------------------------------------------
# 1. Service initialization
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestServiceInitialization:
    """Test that the service initialises with the correct default state."""

    def test_model_not_loaded_at_init(self, service):
        """_model_loaded starts as False before any call to load_models()."""
        assert service._model_loaded is False

    def test_model_attribute_is_none_at_init(self, service):
        """_model starts as None."""
        assert service._model is None

    def test_feature_extractor_is_none_at_init(self, service):
        """_feature_extractor starts as None."""
        assert service._feature_extractor is None

    def test_cleanup_resets_loaded_state(self, loaded_service):
        """cleanup() sets _model_loaded back to False and clears references."""
        loaded_service.cleanup()
        assert loaded_service._model_loaded is False
        assert loaded_service._model is None
        assert loaded_service._feature_extractor is None

    def test_load_models_sets_loaded_flag(self, service):
        """load_models() sets _model_loaded=True on success."""
        mock_fe = MagicMock()
        mock_fe.from_pretrained.return_value = MagicMock()

        mock_model_instance = MagicMock()
        mock_model_cls = MagicMock()
        mock_model_cls.from_pretrained.return_value = mock_model_instance

        mock_transformers = MagicMock()
        mock_transformers.Wav2Vec2FeatureExtractor = mock_fe
        mock_transformers.Wav2Vec2ForSequenceClassification = mock_model_cls

        # load_models() does `from transformers import ...` locally, so we patch
        # the module in sys.modules so those local imports resolve to our mocks.
        with patch.dict("sys.modules", {"transformers": mock_transformers}):
            service.load_models()

        assert service._model_loaded is True

    def test_load_models_idempotent(self, loaded_service):
        """Calling load_models() when already loaded is a no-op (no error)."""
        # Should return immediately without touching transformers
        with patch("builtins.__import__", side_effect=ImportError("should not be called")):
            # The guard `if self._model_loaded: return` prevents any import
            loaded_service.load_models()
        assert loaded_service._model_loaded is True

    def test_get_cached_service_returns_same_instance(self):
        """get_cached_attribute_service() returns the same object on repeated calls."""
        import app.services.speaker_attribute_service as mod

        # Reset cached instance for a clean test
        original = mod._cached_service
        mod._cached_service = None
        try:
            # get_cached_attribute_service() calls load_models(), which downloads the
            # gender model from HuggingFace and re-raises on failure. This test asserts
            # only the caching contract, so the real load is irrelevant to it — but
            # unpatched it made a @pytest.mark.unit test depend on huggingface.co being
            # reachable, and it randomly reddened CI on unrelated PRs (issue #325).
            # CI has no model cache (requirements-ci.txt is CPU-only), so it refetched
            # on every run.
            with patch.object(SpeakerAttributeService, "load_models", return_value=None):
                svc1 = get_cached_attribute_service()
                svc2 = get_cached_attribute_service()
            assert svc1 is svc2
        finally:
            mod._cached_service = original


# ---------------------------------------------------------------------------
# 2. _run_inference mock — format verification
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRunInference:
    """Test that _run_inference returns (gender_str, confidence_float) correctly."""

    def test_run_inference_returns_tuple(self, loaded_service):
        """_run_inference returns a (str, float) tuple."""
        import torch

        # Build a minimal softmax output: female=0.8, male=0.2
        mock_logits = torch.tensor([[2.0, 0.5]])  # higher logit → female (index 0)

        mock_output = MagicMock()
        mock_output.logits = mock_logits
        loaded_service._model.return_value = mock_output

        mock_inputs = MagicMock()
        loaded_service._feature_extractor.return_value = mock_inputs
        # Make the model callable via **mock_inputs (unpack)
        loaded_service._model.__call__ = MagicMock(return_value=mock_output)
        # feature_extractor returns a dict-like object
        loaded_service._feature_extractor.return_value = {"input_values": torch.zeros(1, 16000)}

        audio = np.zeros(16000, dtype=np.float32)
        gender, confidence = loaded_service._run_inference(audio)

        assert isinstance(gender, str)
        assert gender in ("female", "male")
        assert isinstance(confidence, float)
        assert 0.0 <= confidence <= 1.0

    def test_run_inference_female_label(self, loaded_service):
        """When logits favour index 0 → gender='female' (GENDER_ID2LABEL mapping)."""
        import torch

        mock_logits = torch.tensor([[5.0, 0.1]])  # index 0 strongly wins
        mock_output = MagicMock()
        mock_output.logits = mock_logits
        loaded_service._model.return_value = mock_output
        loaded_service._feature_extractor.return_value = {"input_values": torch.zeros(1, 16000)}

        audio = np.zeros(16000, dtype=np.float32)
        gender, confidence = loaded_service._run_inference(audio)

        assert gender == GENDER_ID2LABEL[0]  # "female"
        assert confidence > 0.5

    def test_run_inference_male_label(self, loaded_service):
        """When logits favour index 1 → gender='male' (GENDER_ID2LABEL mapping)."""
        import torch

        mock_logits = torch.tensor([[0.1, 5.0]])  # index 1 strongly wins
        mock_output = MagicMock()
        mock_output.logits = mock_logits
        loaded_service._model.return_value = mock_output
        loaded_service._feature_extractor.return_value = {"input_values": torch.zeros(1, 16000)}

        audio = np.zeros(16000, dtype=np.float32)
        gender, confidence = loaded_service._run_inference(audio)

        assert gender == GENDER_ID2LABEL[1]  # "male"
        assert confidence > 0.5


# ---------------------------------------------------------------------------
# 3. GENDER_ID2LABEL constant
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGenderIdToLabel:
    """Test the GENDER_ID2LABEL mapping constant."""

    def test_index_0_is_female(self):
        assert GENDER_ID2LABEL[0] == "female"

    def test_index_1_is_male(self):
        assert GENDER_ID2LABEL[1] == "male"

    def test_only_two_labels(self):
        assert len(GENDER_ID2LABEL) == 2
