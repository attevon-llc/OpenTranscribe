"""Unit tests for app.utils.hf_hub_offline.

Two independent guarantees are pinned here, matching the module's own docstring:

1. ``load_with_timeout`` bounds a stalled loader to a hard wall-clock budget
   regardless of the offline flag (issue: a DNS retry storm against
   huggingface.co, ~23-30s per cycle, on every backend process restart even
   though the model was fully cached locally).
2. ``force_offline_if_requested`` makes ``HF_HUB_OFFLINE=1`` actually prevent an
   outbound HTTP call for loaders (pyannote's ``Pipeline.from_pretrained``) that
   expose no ``local_files_only`` kwarg of their own — proven by intercepting
   the network layer, not merely asserting the load succeeded offline.
"""

from __future__ import annotations

import time

import pytest

from app.utils.hf_hub_offline import force_offline_if_requested
from app.utils.hf_hub_offline import hf_offline_requested
from app.utils.hf_hub_offline import load_with_timeout


@pytest.mark.unit
class TestHfOfflineRequested:
    def test_true_when_env_var_is_exactly_one(self, monkeypatch):
        monkeypatch.setenv("HF_HUB_OFFLINE", "1")
        assert hf_offline_requested() is True

    def test_false_when_unset(self, monkeypatch):
        monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
        assert hf_offline_requested() is False

    def test_false_when_zero(self, monkeypatch):
        """The .env.example dev default (`HF_HUB_OFFLINE=0`) must not read as offline."""
        monkeypatch.setenv("HF_HUB_OFFLINE", "0")
        assert hf_offline_requested() is False

    def test_false_for_any_other_value(self, monkeypatch):
        monkeypatch.setenv("HF_HUB_OFFLINE", "true")
        assert hf_offline_requested() is False


@pytest.mark.unit
class TestLoadWithTimeout:
    def test_returns_the_loaders_result(self):
        assert load_with_timeout(lambda: 42, timeout=5, label="thing") == 42

    def test_propagates_the_loaders_own_exception(self):
        def _boom():
            raise RuntimeError("no model")

        with pytest.raises(RuntimeError, match="no model"):
            load_with_timeout(_boom, timeout=5, label="thing")

    def test_a_stalled_loader_is_bounded_not_left_to_hang(self):
        """THE regression this exists for: a stall must surface fast, not eventually.

        Stands in for a DNS-retry-storm Hub call with `time.sleep`, mirroring
        `tests/unit/test_speaker_attribute_task_tracking.py`'s proven approach for
        the sibling site. The budget (1s) is far shorter than the simulated stall
        (5s); the assertion on elapsed time is what proves the wait was actually
        bounded rather than merely raising a timeout after the fact.
        """

        def _stall():
            time.sleep(5)
            return "unreachable"

        started = time.monotonic()
        with pytest.raises(TimeoutError, match="did not complete within"):
            load_with_timeout(_stall, timeout=1, label="a slow model")
        elapsed = time.monotonic() - started

        assert elapsed < 4, f"the timeout did not bound the wait: took {elapsed:.1f}s"

    def test_timeout_message_names_the_label_and_the_offline_escape_hatch(self):
        with pytest.raises(TimeoutError) as exc_info:
            load_with_timeout(lambda: time.sleep(2), timeout=0.2, label="Widget model")

        assert "Widget model" in str(exc_info.value)
        assert "HF_HUB_OFFLINE=1" in str(exc_info.value)


@pytest.mark.unit
class TestForceOfflineIfRequested:
    def test_is_a_no_op_when_offline_not_requested(self, monkeypatch):
        import huggingface_hub.constants as hf_constants

        monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
        before = hf_constants.HF_HUB_OFFLINE
        with force_offline_if_requested():
            assert before == hf_constants.HF_HUB_OFFLINE
        assert before == hf_constants.HF_HUB_OFFLINE

    def test_sets_and_restores_the_huggingface_hub_offline_constant(self, monkeypatch):
        import huggingface_hub.constants as hf_constants

        monkeypatch.setenv("HF_HUB_OFFLINE", "1")
        hf_constants.HF_HUB_OFFLINE = False  # simulate: imported before the env var was set

        with force_offline_if_requested():
            assert hf_constants.HF_HUB_OFFLINE is True

        assert hf_constants.HF_HUB_OFFLINE is False, "must restore, not leave forced globally"

    def test_restores_even_when_the_body_raises(self, monkeypatch):
        import huggingface_hub.constants as hf_constants

        monkeypatch.setenv("HF_HUB_OFFLINE", "1")
        hf_constants.HF_HUB_OFFLINE = False

        with pytest.raises(ValueError, match="boom"), force_offline_if_requested():
            raise ValueError("boom")

        assert hf_constants.HF_HUB_OFFLINE is False

    def test_makes_a_hub_http_call_fail_instantly_instead_of_attempting_the_network(
        self, monkeypatch
    ):
        """The behavioral proof: not a message change, an actual blocked call.

        Uses huggingface_hub's OWN session machinery rather than mocking `requests`
        directly, so this proves the exact mechanism `force_offline_if_requested`
        relies on (constants.HF_HUB_OFFLINE + reset_sessions -> a fresh session
        mounts OfflineAdapter) rather than a stand-in for it.
        """
        from huggingface_hub.errors import OfflineModeIsEnabled
        from huggingface_hub.utils._http import get_session
        from huggingface_hub.utils._http import reset_sessions

        monkeypatch.setenv("HF_HUB_OFFLINE", "1")
        reset_sessions()  # drop any session cached by an earlier (online) test
        try:
            with force_offline_if_requested():
                session = get_session()
                with pytest.raises(OfflineModeIsEnabled):
                    session.get("https://huggingface.co/api/models/pyannote/does-not-matter")
        finally:
            reset_sessions()  # don't leak an offline session into a later test
