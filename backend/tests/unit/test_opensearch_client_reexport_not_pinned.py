"""A test that patches the re-exported OpenSearch client must not pin it for the next test.

``app.services.opensearch_service.opensearch_client`` is forwarded by a module ``__getattr__``.
Before pytest 9.2, ``monkeypatch.setattr`` on it recorded the forwarded value and SET it back on
undo, as a real attribute that shadowed ``__getattr__``. Patch the client module first and the
package second (``test_voiceprint_document_stamps_by_writer``'s fixture did) and the fake stayed
on the package for the rest of the worker. pytest 9.2 records the instance ``__dict__`` entry
instead (pytest #10644), so undo deletes the attribute. The root conftest's old
``_unpin_opensearch_client_reexport`` workaround popped the entry itself; with pytest 9.2 that
pop ran before monkeypatch's own ``delattr`` whenever an earlier autouse fixture had already
created ``monkeypatch``, and the teardown raised. The first test pins the pytest behaviour the
suite now relies on.

The two tests run in order on one worker (``xdist_group``); the first patches, the second checks.
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

    # monkeypatch's undo must delete the entry it created, not pin the fake (or the client it
    # read through ``__getattr__``) as a real package attribute.
    assert "opensearch_client" not in vars(package)
    assert package.opensearch_client is client.opensearch_client


def test_2_the_next_test_reads_the_client_module_again():
    import app.services.opensearch_service as package
    from app.services.opensearch_service import client

    assert "opensearch_client" not in vars(package)
    assert package.opensearch_client is client.opensearch_client
