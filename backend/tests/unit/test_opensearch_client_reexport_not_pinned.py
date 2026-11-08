"""A test that patches the re-exported OpenSearch client must not pin it for the next test.

``app.services.opensearch_service.opensearch_client`` is forwarded by a module ``__getattr__``.
``monkeypatch.setattr`` on it records the forwarded value and SETS it back on undo, as a real
attribute that shadows ``__getattr__``. Patch the client module first and the package second
(``test_voiceprint_document_stamps_by_writer``'s fixture did) and the fake stays on the
package for the rest of the worker: the second test here then reads a ``MagicMock``. The root
conftest's ``_unpin_opensearch_client_reexport`` removes the pinned entry after every test.

The two tests run in order on one worker (``xdist_group``); the first pins, the second checks.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.xdist_group("opensearch_client_reexport")


def test_1_patching_both_modules_the_way_a_fixture_did(monkeypatch):
    import app.services.opensearch_service as package
    from app.services.opensearch_service import client

    fake = MagicMock(name="opensearch")
    monkeypatch.setattr(client, "opensearch_client", fake)
    monkeypatch.setattr("app.services.opensearch_service.opensearch_client", fake)
    monkeypatch.undo()

    # The pin happens here, in monkeypatch's undo: the fake is now a real package attribute.
    assert vars(package).get("opensearch_client") is fake


def test_2_the_next_test_reads_the_client_module_again():
    import app.services.opensearch_service as package
    from app.services.opensearch_service import client

    assert "opensearch_client" not in vars(package)
    assert package.opensearch_client is client.opensearch_client
