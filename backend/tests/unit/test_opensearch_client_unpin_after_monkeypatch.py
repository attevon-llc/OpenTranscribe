"""The re-export unpin must hold even when the pin is made by monkeypatch's own teardown.

The root conftest's ``_unpin_opensearch_client_reexport`` used to clean up only AFTER the
test. ``monkeypatch`` is instantiated by an earlier autouse fixture, so it is torn down
AFTER that cleanup: a test that did ``monkeypatch.setattr("app.services.opensearch_service
.opensearch_client", fake)`` had its undo re-pin the forwarded real client as a package
attribute once the cleanup had already run, and the next test on the worker (here
``test_speaker_match_query_tenant_filter``) bypassed its own fake and sent a real POST.

The two tests run in order on one worker; the first pins through teardown, the second checks.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.xdist_group("opensearch_client_unpin_teardown")


def test_1_a_package_level_patch_pins_on_monkeypatch_teardown(monkeypatch):
    import app.services.opensearch_service as package

    fake = MagicMock()
    monkeypatch.setattr("app.services.opensearch_service.opensearch_client", fake)
    assert package.opensearch_client is fake


def test_2_the_next_test_still_sees_the_client_module_not_a_pin():
    import app.services.opensearch_service as package

    assert "opensearch_client" not in vars(package)
