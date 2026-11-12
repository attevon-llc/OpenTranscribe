"""The stored description of an engine setting must match what the API accepts."""

from __future__ import annotations

from app.api.endpoints.engine_settings import _DESCRIPTIONS
from app.transcription.engine.backends import VALID_TRANSCRIBER_BACKENDS
from app.transcription.engine.config import DEFAULT_TRANSCRIBER_BACKEND


def test_the_transcriber_description_offers_only_the_backend_the_update_accepts() -> None:
    """``update`` refuses every transcriber but the default until #366, so the description
    written beside the setting must not list the others as choices."""
    description = _DESCRIPTIONS["transcriber_backend"]
    refused = [name for name in VALID_TRANSCRIBER_BACKENDS if name != DEFAULT_TRANSCRIBER_BACKEND]

    assert refused, "the registry no longer has an unwired backend; revisit this test with #366"
    assert DEFAULT_TRANSCRIBER_BACKEND in description
    assert [name for name in refused if name in description] == []
    assert "#366" in description
