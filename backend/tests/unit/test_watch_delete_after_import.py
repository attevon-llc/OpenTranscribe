"""``delete_after_import`` deletes the original only for a local watch client."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.services.watch_sources import processing
from app.services.watch_sources.base import BaseWatchSourceClient
from app.services.watch_sources.base import RemoteFileInfo
from app.services.watch_sources.local_client import LocalWatchClient

pytestmark = pytest.mark.unit


@contextmanager
def _fake_session():
    yield MagicMock()


def _import(client, source_type: str) -> str | None:
    info = RemoteFileInfo(path="/watch/a.mp3", name="a.mp3", size=10)
    with (
        patch.object(processing, "_claim_import", return_value=(1, source_type, True)),
        patch.object(processing, "session_scope", _fake_session),
        patch.object(
            processing, "ingest_prepared_file", return_value=SimpleNamespace(status="imported")
        ),
    ):
        return processing.import_single_file(1, info, client)


def test_local_client_deletes_the_imported_original():
    client = MagicMock(spec=LocalWatchClient)
    assert _import(client, "local") == "imported"
    client.delete_file.assert_called_once_with("/watch/a.mp3")


def test_a_client_that_is_not_the_local_one_never_deletes():
    client = MagicMock(spec=BaseWatchSourceClient)
    client.delete_file = MagicMock()  # not part of the base contract; must not be reached
    assert _import(client, "local") == "imported"
    client.delete_file.assert_not_called()
