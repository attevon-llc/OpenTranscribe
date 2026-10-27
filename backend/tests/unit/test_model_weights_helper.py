"""``fixtures/model_weights`` decides skip-vs-fail for the redaction ``models`` tests.

The failure it prevents: a ``models`` test that skips when its detector fails to load hides a
library upgrade that broke the load while the weights sit on disk. These tests prove the three
outcomes -- and that "cached" means what the app's own hub resolver says.
"""

from __future__ import annotations

import pytest
from fixtures.hf_cache import make_complete_repo
from fixtures.model_weights import hf_weights_cached
from fixtures.model_weights import package_installed
from fixtures.model_weights import require_loaded


def test_loaded_model_passes_through() -> None:
    assert require_loaded(True, available=True, what="x") is None
    assert require_loaded(True, available=False, what="x") is None


def test_absent_model_skips() -> None:
    with pytest.raises(pytest.skip.Exception, match="unavailable"):
        require_loaded(False, available=False, what="the model")


def test_present_but_unloadable_model_fails_not_skips() -> None:
    with pytest.raises(pytest.fail.Exception, match="present on this machine but failed to load"):
        require_loaded(False, available=True, what="the model")


def test_hf_weights_cached_follows_the_hub_cache(hf_cache) -> None:
    assert hf_weights_cached("org/never-downloaded") is False
    make_complete_repo(hf_cache, "org/downloaded")
    assert hf_weights_cached("org/downloaded") is True


def test_package_installed() -> None:
    assert package_installed("json") is True
    assert package_installed("definitely_not_a_real_package_zz") is False
