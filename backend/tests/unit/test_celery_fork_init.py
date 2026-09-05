"""What runs inside a forked Celery prefork child, and what must never (issue #631).

``worker_process_init`` fires from ``billiard.pool.Worker.after_fork()``, which runs
**before** ``on_loop_start()`` puts ``WORKER_UP`` on the out-queue. The parent armed
``celery.concurrency.asynpool.verify_process_alive`` at fork time and ``os.kill(pid, 9)``s
any child that misses ``worker_proc_alive_timeout``. The replacement child then runs the
same initializer and is killed too — a fork/SIGKILL loop that accepts zero tasks and ends
only when whatever was slow stops being slow.

Observed in production on ``cpu-processor``: 69,231 kills over 10h46m, zero tasks
consumed, then a spontaneous recovery. The initializer's only slow step was
``huggingface_hub.login()``, whose ``whoami()`` round trip goes out through ``requests``
with **no** ``timeout=`` — against a 4.0s budget that nothing overrode.

These tests pin both halves of the fix: the initializer performs no Hub network call, and
the budget it runs against is no longer celery's bare default.
"""

from __future__ import annotations

import logging
import time
from unittest.mock import MagicMock

import pytest
from celery.concurrency.asynpool import PROC_ALIVE_TIMEOUT

from app.core.celery import celery_app
from app.core.celery import init_worker_process
from app.core.celery import publish_hf_token_to_environment


@pytest.fixture(autouse=True)
def _clean_hf_env(monkeypatch):
    """Start every test from a known HuggingFace environment."""
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    monkeypatch.delenv("HUGGINGFACE_TOKEN", raising=False)


@pytest.mark.unit
class TestPublishHfTokenToEnvironment:
    def test_exports_the_configured_token_as_hf_token(self, monkeypatch):
        monkeypatch.setenv("HUGGINGFACE_TOKEN", "hf_configured_value")

        assert publish_hf_token_to_environment() is True

        import os

        assert os.environ["HF_TOKEN"] == "hf_configured_value"

    def test_is_a_noop_when_no_token_is_configured(self):
        import os

        assert publish_hf_token_to_environment() is False
        assert "HF_TOKEN" not in os.environ

    def test_does_not_override_an_operator_supplied_hf_token(self, monkeypatch):
        monkeypatch.setenv("HUGGINGFACE_TOKEN", "hf_from_dotenv")
        monkeypatch.setenv("HF_TOKEN", "hf_set_by_operator")

        assert publish_hf_token_to_environment() is False

        import os

        assert os.environ["HF_TOKEN"] == "hf_set_by_operator"

    def test_does_not_override_the_legacy_hub_token_variable(self, monkeypatch):
        """``huggingface_hub`` reads ``HUGGING_FACE_HUB_TOKEN`` when ``HF_TOKEN`` is unset.

        Writing ``HF_TOKEN`` anyway would silently take precedence over it and swap which
        credential gated downloads authenticate with.
        """
        monkeypatch.setenv("HUGGINGFACE_TOKEN", "hf_from_dotenv")
        monkeypatch.setenv("HUGGING_FACE_HUB_TOKEN", "hf_legacy_operator_value")

        assert publish_hf_token_to_environment() is False

        import os

        assert "HF_TOKEN" not in os.environ

    def test_makes_the_token_resolvable_by_huggingface_hub_itself(self, monkeypatch):
        """The point of the export, asserted against the library rather than the variable.

        ``get_token()`` is what every un-parameterised Hub call resolves through; if it
        cannot see the token, exporting it achieved nothing.
        """
        huggingface_hub = pytest.importorskip("huggingface_hub")
        monkeypatch.setenv("HUGGINGFACE_TOKEN", "hf_resolvable_value")

        publish_hf_token_to_environment()

        assert huggingface_hub.get_token() == "hf_resolvable_value"


@pytest.mark.unit
class TestForkInitializerDoesNoNetworkIo:
    def test_does_not_call_huggingface_login(self, monkeypatch):
        """THE regression. ``login()`` -> ``_login()`` -> ``whoami()`` is a blocking HTTPS
        call with no timeout, issued once per forked child against a hard kill timer.
        """
        huggingface_hub = pytest.importorskip("huggingface_hub")
        monkeypatch.setenv("HUGGINGFACE_TOKEN", "hf_configured_value")
        login = MagicMock(name="huggingface_hub.login")
        monkeypatch.setattr(huggingface_hub, "login", login)

        init_worker_process()

        assert login.call_count == 0, (
            "init_worker_process called huggingface_hub.login(); that is an un-timed "
            "network round trip inside a forked child (issue #631)"
        )
        # ...and the state the call used to produce is produced anyway, so "not called"
        # means "replaced", not "dropped".
        import os

        assert os.environ["HF_TOKEN"] == "hf_configured_value"

    def test_a_stalled_hub_call_cannot_stall_the_fork_initializer(self, monkeypatch):
        """The incident's actual shape: not an error, a *duration*.

        ``login()`` swallowed failures already — its ``except Exception`` was never the
        problem. What killed the pool was the call taking longer than the fork budget
        while succeeding-or-failing eventually. So the discriminating test has to stall
        the Hub rather than break it.

        ⚠️ Patch ``hf_api.whoami`` — the module-level **bound alias** — not
        ``HfApi.whoami``: ``_login`` does ``from .hf_api import whoami`` at call time and
        binds the alias, so patching the class method intercepts nothing. An earlier draft
        of this test did exactly that and passed against the unfixed code.
        """
        hf_api = pytest.importorskip("huggingface_hub.hf_api")
        monkeypatch.setenv("HUGGINGFACE_TOKEN", "hf_configured_value")
        stall_seconds = PROC_ALIVE_TIMEOUT + 2.0
        monkeypatch.setattr(hf_api, "whoami", lambda *a, **k: time.sleep(stall_seconds))

        started = time.monotonic()
        init_worker_process()
        elapsed = time.monotonic() - started

        assert elapsed < PROC_ALIVE_TIMEOUT / 10, (
            f"init_worker_process took {elapsed:.3f}s against a {stall_seconds:.1f}s Hub "
            f"stall; celery SIGKILLs a forked child that has not signalled UP within "
            f"worker_proc_alive_timeout, whose floor is {PROC_ALIVE_TIMEOUT}s"
        )

    def test_publishes_the_token_so_the_removed_login_loses_nothing(self, monkeypatch):
        monkeypatch.setenv("HUGGINGFACE_TOKEN", "hf_configured_value")

        init_worker_process()

        import os

        assert os.environ["HF_TOKEN"] == "hf_configured_value"

    def test_logs_the_fork_lifecycle_so_a_stuck_child_is_visible_in_the_logs(
        self, monkeypatch, caplog
    ):
        """Entry with no matching exit, repeating, names the stuck phase directly.

        Issue #631 had to be reconstructed after the fact from ``asynpool``'s SIGKILL
        lines alone, which say a child died but not what it was doing.
        """
        monkeypatch.setenv("HUGGINGFACE_TOKEN", "hf_configured_value")

        with caplog.at_level(logging.INFO, logger="app.core.celery"):
            init_worker_process()

        messages = [record.getMessage() for record in caplog.records]
        assert any("fork init started" in m for m in messages), messages
        assert any("fork init finished" in m for m in messages), messages
        assert any("elapsed=" in m for m in messages), messages


@pytest.mark.unit
class TestForkKillBudget:
    def test_worker_proc_alive_timeout_is_raised_above_celerys_default(self):
        """Nothing overrode celery's 4.0s default, and that is the loop's amplifier.

        This is defence in depth, not the fix: it stops ordinary contention on a loaded
        host from starting the loop at all. It must stay FINITE — a genuinely dead fork
        still has to be reaped.
        """
        configured = celery_app.conf.worker_proc_alive_timeout

        assert configured > PROC_ALIVE_TIMEOUT
        assert configured < 300, "the budget must stay finite; a dead fork must still be reaped"

    def test_the_budget_is_operator_tunable(self, monkeypatch):
        from app.core.celery import _float_env

        monkeypatch.setenv("CELERY_PROC_ALIVE_TIMEOUT", "12.5")
        assert _float_env("CELERY_PROC_ALIVE_TIMEOUT", 30.0) == 12.5

    def test_a_garbage_budget_falls_back_instead_of_crashing_the_worker(self, monkeypatch):
        from app.core.celery import _float_env

        monkeypatch.setenv("CELERY_PROC_ALIVE_TIMEOUT", "not-a-number")
        assert _float_env("CELERY_PROC_ALIVE_TIMEOUT", 30.0) == 30.0
