"""DiarizationProviderFactory builds the detector a pipeline plan names (issue #1225).

The plan (``services/pipeline_plan``) decides WHICH detector runs; the factory only turns
the plan's engine into a provider. ``local`` is finally live (the post stage runs this
server's engine through ``LocalDiarizationProvider``), and the source-switching entry points
(``create_for_source`` / ``create_for_user``) are gone: a stored source re-read here could
disagree with the plan the run was dispatched with.
"""

from __future__ import annotations

import pytest

from app.core.constants import DEFAULT_DIARIZATION_SOURCE
from app.core.constants import VALID_DIARIZATION_SOURCES
from app.services.diarization.factory import DiarizationProviderFactory
from app.services.diarization.factory import DiarizerConfigUnusableError
from app.services.diarization.local_provider import LocalDiarizationProvider
from app.services.error_categorization_service import DIARIZER_CONFIG_UNUSABLE_MESSAGE
from app.services.error_categorization_service import ErrorCategorizationService
from app.services.error_categorization_service import UserErrorReason
from app.utils.error_classification import ErrorCategory


class TestCreateForEngine:
    def test_local_builds_this_servers_engine(self, db_session, normal_user):
        provider = DiarizationProviderFactory.create_for_engine("local", normal_user.id, db_session)
        assert isinstance(provider, LocalDiarizationProvider)

    @pytest.mark.parametrize("engine", ["asr_config", "asr", "off", "nonsense"])
    def test_an_engine_it_cannot_build_raises_the_fixed_configuration_error(
        self, db_session, normal_user, engine
    ):
        with pytest.raises(DiarizerConfigUnusableError) as raised:
            DiarizationProviderFactory.create_for_engine(engine, normal_user.id, db_session)
        assert str(raised.value) == DIARIZER_CONFIG_UNUSABLE_MESSAGE

    def test_the_configuration_error_is_classified_as_permanent(self):
        failure = ErrorCategorizationService.classify_failure(str(DiarizerConfigUnusableError()))
        assert failure.reason == UserErrorReason.PIPELINE_NOT_RUNNABLE
        assert failure.retry_category == ErrorCategory.CONFIGURATION_REQUIRED
        assert failure.user_message == DIARIZER_CONFIG_UNUSABLE_MESSAGE


class TestSourceConstants:
    def test_default_source_is_provider(self):
        assert DEFAULT_DIARIZATION_SOURCE == "provider"

    def test_valid_sources(self):
        assert set(VALID_DIARIZATION_SOURCES) == {
            "provider",
            "local",
            "pyannote",
            "asr_config",
            "off",
        }

    def test_the_factory_holds_no_second_copy_of_the_sources(self):
        """One definition (core.constants); the factory's duplicate drifted from it once."""
        from app.services.diarization import factory as factory_mod

        assert not hasattr(factory_mod, "VALID_DIARIZATION_SOURCES")


def test_the_source_switching_entry_points_are_gone():
    """The plan is the one decision point; nothing may re-derive the detector from the
    stored source at run time."""
    assert not hasattr(DiarizationProviderFactory, "create_for_source")
    assert not hasattr(DiarizationProviderFactory, "create_for_user")
