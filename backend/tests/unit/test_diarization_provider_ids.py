"""Speaker-detection provider ids say which engine ran (issue #1229).

Before #1229 the pyannote.ai cloud service and the local in-process PyAnnote engine both
reported ``pyannote``, so a file diarized in the cloud displayed as "PyAnnote engine (this
server)". The local provider reported a third spelling, ``local``. Provenance needs one
unambiguous id per engine.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

import pandas as pd
import pytest

from app.services.diarization.local_provider import LocalDiarizationProvider
from app.services.diarization.pyannote_provider import PyAnnoteCloudDiarizationProvider
from app.services.diarization.types import DiarizeConfig
from app.services.file_provenance import diarization_facts
from app.services.file_provenance import inline_speakers_by


def test_the_cloud_service_reports_pyannote_ai():
    assert PyAnnoteCloudDiarizationProvider(api_key="k").provider_name == "pyannote.ai"


@pytest.mark.parametrize(
    ("served_by", "model"),
    [("native", "community-1-native"), ("pyannote", "pyannote/speaker-diarization-community-1")],
)
def test_the_local_provider_reports_the_engine_that_served(tmp_path, served_by, model):
    diarizer = MagicMock()
    diarizer.diarize.return_value = (
        pd.DataFrame({"start": [0.0], "end": [1.0], "speaker": ["SPEAKER_00"]}),
        {},
        {},
    )
    diarizer.last_provider = served_by
    diarizer.last_model = model
    manager = MagicMock(get_diarizer=MagicMock(return_value=diarizer))
    with (
        patch("app.transcription.audio.load_audio", return_value=MagicMock()),
        patch("app.transcription.model_manager.ModelManager.get_instance", return_value=manager),
    ):
        result = LocalDiarizationProvider().diarize(str(tmp_path / "a.wav"), DiarizeConfig())

    assert (result.provider_name, result.model_name) == (served_by, model)


@pytest.mark.parametrize(
    ("provider", "asr_provider", "where", "engine"),
    [
        ("native", "local", "server", "local"),
        ("pyannote", "local", "server", "local"),
        ("pyannote", "deepgram", "server", "local"),
        ("pyannote.ai", "local", "cloud", "pyannote_ai"),
        ("pyannote.ai", "deepgram", "cloud", "pyannote_ai"),
        ("deepgram", "deepgram", "cloud", "asr"),
        # The pyannote.ai STT service returning its own speakers: same spelling as the
        # local engine, told apart only by the transcription provider that ran.
        ("pyannote", "pyannote", "cloud", "asr"),
    ],
)
def test_diarization_facts_name_the_engine_and_where_it_ran(provider, asr_provider, where, engine):
    facts = diarization_facts(provider, "m", asr_provider=asr_provider)
    assert (facts["where"], facts["engine"], facts["provider"]) == (where, engine, provider)


def test_unknown_diarization_records_no_location():
    assert diarization_facts(None, None) == {"model": None}


def test_inline_speakers_are_credited_only_when_asked_for_and_returned():
    result = SimpleNamespace(has_speakers=True, provider_name="deepgram", model_name="nova-3")
    assert inline_speakers_by(result, True) == {"provider": "deepgram", "model": "nova-3"}
    # Speaker detection was not requested of the transcription provider (another engine
    # detects speakers, or it is off): the provider gets no credit even if labels came back.
    assert inline_speakers_by(result, False) == {}
    assert inline_speakers_by(SimpleNamespace(has_speakers=False), True) == {}
