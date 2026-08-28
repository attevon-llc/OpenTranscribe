"""Which backend `SpeakerEmbeddingService` runs, and how it degrades (issue #571).

The service can serve v4 (256-d) embeddings from the diar-native sidecar instead of
loading `pyannote/wespeaker-voxceleb-resnet34-LM` in-process — the sidecar runs the
same weights, measured at cosine 0.9999997 against the in-process model on 134 AMI
ground-truth windows. What must never happen is the sidecar serving a mode it cannot:
`pyannote/embedding` (v3) is a *different* 512-d network, so routing v3 there would
write 256-d vectors into a 512-d index — a dimension mismatch that OpenSearch would
reject document-by-document, or, worse, that a permissive index would accept and then
score against incomparable neighbours.

No real model or sidecar is loaded here: `_initialize_model` is the seam, and asserting
it was NOT called is the whole point of the native cases.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.services.speaker_embedding_service import SpeakerEmbeddingService


def _stub_in_process_model(monkeypatch, loaded: list[str], vector=None):
    """Replace the in-process model load with a recorder, optionally returning `vector`."""

    def fake_initialize(self):
        loaded.append(self.model_name)
        self.inference = (lambda _audio: vector) if vector is not None else object()

    monkeypatch.setattr(SpeakerEmbeddingService, "_initialize_model", fake_initialize)


@pytest.fixture
def service_factory(monkeypatch, tmp_path):
    """Build a service with the model load and the sidecar probe both under our control."""
    loaded: list[str] = []
    _stub_in_process_model(monkeypatch, loaded)
    monkeypatch.setattr(
        "app.utils.hardware_detection.detect_hardware",
        lambda: type(
            "HW", (), {"get_pyannote_config": lambda self: {"device": "cpu"}, "__init__": None}
        )(),
    )

    def build(*, mode, sidecar_up=True, native_env="true", model_name=None):
        monkeypatch.setenv("USE_NATIVE_SPEAKER_EMBEDDINGS", native_env)
        monkeypatch.setattr(
            "app.services.native_embedding_client.native_embedding_available",
            lambda base_url=None: sidecar_up,
        )
        return SpeakerEmbeddingService(
            mode=mode, models_dir=str(tmp_path), model_name=model_name
        ), loaded

    return build


class TestBackendSelection:
    def test_v4_with_a_healthy_sidecar_loads_no_in_process_model(self, service_factory) -> None:
        """The point of the change: the standalone PyAnnote load is eliminated, not bypassed."""
        service, loaded = service_factory(mode="v4", sidecar_up=True)
        assert service.backend == "native"
        assert loaded == [], f"an in-process model was loaded anyway: {loaded}"
        assert service.inference is None

    def test_v3_never_uses_the_sidecar_even_when_it_is_healthy(self, service_factory) -> None:
        """pyannote/embedding is 512-d and a different network; the sidecar has no equivalent."""
        service, loaded = service_factory(mode="v3", sidecar_up=True)
        assert service.backend == "pyannote"
        assert loaded == ["pyannote/embedding"]
        assert service.get_embedding_dimension() == 512

    def test_an_unreachable_sidecar_falls_back_to_the_in_process_model(
        self, service_factory
    ) -> None:
        service, loaded = service_factory(mode="v4", sidecar_up=False)
        assert service.backend == "pyannote"
        assert loaded == ["pyannote/wespeaker-voxceleb-resnet34-LM"]

    def test_the_env_escape_hatch_forces_the_in_process_model(self, service_factory) -> None:
        service, loaded = service_factory(mode="v4", sidecar_up=True, native_env="false")
        assert service.backend == "pyannote"
        assert loaded == ["pyannote/wespeaker-voxceleb-resnet34-LM"]

    def test_an_explicitly_pinned_model_is_honoured_in_process(self, service_factory) -> None:
        """A caller naming a specific model must get that model, not the sidecar's."""
        service, loaded = service_factory(
            mode="v4", sidecar_up=True, model_name="some/other-embedding-model"
        )
        assert service.backend == "pyannote"
        assert loaded == ["some/other-embedding-model"]

    def test_the_native_backend_reports_the_weights_it_runs(self, service_factory) -> None:
        """`model_name` names the weights, so the warm-cache key survives a backend switch.

        `get_cached_embedding_service` compares `model_name` against
        `EmbeddingModeService.get_embedding_model_name(mode)`; a sidecar-specific name
        there would miss on every lookup and rebuild the service each call.
        """
        service, _ = service_factory(mode="v4", sidecar_up=True)
        assert service.model_name == "pyannote/wespeaker-voxceleb-resnet34-LM"
        assert service.get_embedding_dimension() == 256


class TestExtractionThroughTheNativeBackend:
    def test_extraction_returns_the_sidecar_vector_l2_normalized(
        self, service_factory, monkeypatch
    ) -> None:
        import torch

        service, loaded = service_factory(mode="v4", sidecar_up=True)
        vec = np.arange(1, 257, dtype=np.float32)
        monkeypatch.setattr(
            "app.services.native_embedding_client.embed_waveform",
            lambda samples, base_url=None: vec / np.linalg.norm(vec),
        )
        out = service.extract_embedding_from_waveform(torch.zeros(1, 160_000), 16_000)
        assert out is not None
        assert out.shape == (256,)
        assert float(np.linalg.norm(out)) == pytest.approx(1.0, abs=1e-5)
        assert loaded == []

    def test_non_16khz_audio_is_embedded_in_process_not_by_the_sidecar(
        self, service_factory, monkeypatch
    ) -> None:
        """The sidecar's fbank front-end assumes 16 kHz and takes no rate argument.

        A 44.1 kHz clip sent there comes back the right shape and the wrong vector —
        reachable because `_load_audio`'s torchaudio/scipy fallbacks do not resample.
        """
        import torch

        service, loaded = service_factory(mode="v4", sidecar_up=True)
        assert service.backend == "native"

        called: list = []

        def recording_embed(samples, base_url=None):
            called.append(samples)
            return np.ones(256, dtype=np.float32)

        monkeypatch.setattr("app.services.native_embedding_client.embed_waveform", recording_embed)
        fallback_vec = np.zeros(256, dtype=np.float32)
        fallback_vec[3] = 1.0
        _stub_in_process_model(monkeypatch, loaded, fallback_vec)

        out = service.extract_embedding_from_waveform(torch.zeros(1, 44_100), 44_100)
        assert called == [], "44.1 kHz audio was sent to the 16 kHz-only sidecar"
        assert out is not None
        assert float(out[3]) == pytest.approx(1.0, abs=1e-6)
        assert loaded == ["pyannote/wespeaker-voxceleb-resnet34-LM"]

    def test_a_sidecar_loss_mid_run_loads_the_in_process_model_and_continues(
        self, service_factory, monkeypatch
    ) -> None:
        """Degrade, don't crash — the discipline NativeSpeakerDiarizer already applies."""
        import torch

        service, loaded = service_factory(mode="v4", sidecar_up=True)
        assert service.backend == "native"

        monkeypatch.setattr(
            "app.services.native_embedding_client.embed_waveform",
            lambda samples, base_url=None: None,
        )
        fallback_vec = np.zeros(256, dtype=np.float32)
        fallback_vec[7] = 4.0  # un-normalized on purpose: the service must normalize it
        _stub_in_process_model(monkeypatch, loaded, fallback_vec)

        out = service.extract_embedding_from_waveform(torch.zeros(1, 160_000), 16_000)
        assert out is not None, "a sidecar loss produced no embedding instead of falling back"
        assert loaded == ["pyannote/wespeaker-voxceleb-resnet34-LM"]
        assert service.backend == "pyannote", "the instance did not switch backends"
        assert float(out[7]) == pytest.approx(1.0, abs=1e-6), "not the in-process vector"


class TestExistingV3DataKeepsItsOwnBackend:
    """The v3 exit path, which is the migration this change relies on already existing.

    A 512-dim install moves to 256-dim via the v4 migration tasks
    (`extract_v4_embeddings_batch` / `speaker_embedding_consistency_repair_batch`), both
    of which ask for a service **by mode**. That means the same one switch decides the
    backend for a live transcription and for a migration batch: v4 work goes to the
    sidecar, v3 work stays on `pyannote/embedding`. Nothing re-embeds v3 vectors *into*
    a v3 index using native 256-d output, and nothing compares the two dimensions.
    """

    @pytest.mark.parametrize(
        ("mode", "expected_backend", "expected_dim"),
        [("v4", "native", 256), ("v3", "pyannote", 512)],
    )
    def test_the_migration_path_picks_the_backend_from_the_mode_it_asks_for(
        self, service_factory, monkeypatch, mode, expected_backend, expected_dim
    ) -> None:
        import app.services.speaker_embedding_service as mod

        monkeypatch.setattr(mod, "_cached_embedding_service", None, raising=False)
        service, _ = service_factory(mode=mode, sidecar_up=True)
        assert service.backend == expected_backend
        assert service.get_embedding_dimension() == expected_dim

    def test_a_native_v4_service_never_reports_a_v3_dimension(self, service_factory) -> None:
        """The failure that would corrupt an index: 256-d vectors written as if they were 512."""
        service, _ = service_factory(mode="v4", sidecar_up=True)
        vec = np.ones(256, dtype=np.float32)
        assert service.get_embedding_dimension() == len(vec) == 256


class TestCleanup:
    def test_cleanup_on_the_native_backend_is_a_no_op(self, service_factory) -> None:
        """There is no in-process model to free; cleanup must not explode reaching for one."""
        service, _ = service_factory(mode="v4", sidecar_up=True)
        service.cleanup()
        assert service.inference is None

    def test_cleanup_releases_the_in_process_model(self, service_factory) -> None:
        service, _ = service_factory(mode="v4", sidecar_up=False)
        assert service.inference is not None
        service.cleanup()
        assert service.inference is None
