"""DiarizationProviderFactory: source resolution, incl. the dead "local" branch removal
(issue #672).

``LocalDiarizationProvider`` used to be constructed by both ``create_for_source`` and
``create_for_user`` for ``source == "local"``, but neither call site is reachable in
production: the single production caller,
``tasks/transcription/cloud_asr.py::_run_parallel_cloud_asr_and_diarization``, is only
entered when ``diarization_source == "pyannote"`` (see ``_run_cloud_asr_pipeline``);
``source == "local"`` is served entirely by ``rediarize_task`` calling
``ModelManager.get_diarizer()`` directly, never through this factory. These tests pin the
fix: ``"local"`` must resolve to ``None`` here (same as ``"provider"``/``"off"``), and the
factory module must not import ``LocalDiarizationProvider`` for that source.
"""

from __future__ import annotations

from app.services.diarization.factory import DEFAULT_DIARIZATION_SOURCE
from app.services.diarization.factory import VALID_DIARIZATION_SOURCES
from app.services.diarization.factory import DiarizationProviderFactory


class TestCreateForSource:
    def test_local_resolves_to_none_not_a_provider_instance(self):
        assert DiarizationProviderFactory.create_for_source("local") is None

    def test_provider_and_off_still_resolve_to_none(self):
        assert DiarizationProviderFactory.create_for_source("provider") is None
        assert DiarizationProviderFactory.create_for_source("off") is None

    def test_unknown_source_still_raises(self):
        import pytest

        with pytest.raises(ValueError, match="Unknown diarization source"):
            DiarizationProviderFactory.create_for_source("not-a-real-source")


class TestCreateForUser:
    def test_local_resolves_to_none(self, db_session):
        """A user with diarization_source='local' gets no provider from this factory —
        that source is served by rediarize_task instead, never by anything here."""
        from app.models import User
        from app.models import UserSetting

        user = User(
            email="local-diar-factory-test@example.com",
            hashed_password="x",
            full_name="Local Diar Factory Test",
        )
        db_session.add(user)
        db_session.flush()
        db_session.add(
            UserSetting(
                user_id=user.id,
                setting_key="transcription_diarization_source",
                setting_value="local",
            )
        )
        db_session.flush()

        assert DiarizationProviderFactory.create_for_user(user.id, db_session) is None

    def test_default_source_is_provider(self):
        assert DEFAULT_DIARIZATION_SOURCE == "provider"

    def test_valid_sources_unchanged(self):
        assert set(VALID_DIARIZATION_SOURCES) == {"provider", "local", "pyannote", "off"}


def test_factory_module_never_imports_local_diarization_provider():
    """The dead branches are gone, not merely unreachable at runtime: nothing in
    factory.py's source references LocalDiarizationProvider at all."""
    import inspect

    from app.services.diarization import factory as factory_mod

    source = inspect.getsource(factory_mod)
    assert "LocalDiarizationProvider" not in source
