"""``record_degradation``: the shared "control ran without its state store" counter.

One copy in ``auth/degradation.py`` replaced four byte-identical ones. Its contract is that
it can never raise: it runs on the fallback path, when infrastructure is already degraded.
"""

import pytest

from app.auth.degradation import record_degradation
from app.core import metrics as metrics_module


class _Counter:
    def __init__(self):
        self.captured: dict[str, str] = {}
        self.incs = 0

    def labels(self, **kwargs):
        self.captured = kwargs
        return self

    def inc(self):
        self.incs += 1


def test_control_and_fallback_reach_the_metric_as_labels(monkeypatch):
    fake = _Counter()
    monkeypatch.setattr(metrics_module, "security_state_degraded_total", fake)

    record_degradation("account_lockout", "local")

    assert fake.captured == {"control": "account_lockout", "fallback": "local"}
    assert fake.incs == 1


def test_a_different_call_carries_its_own_values_not_a_stale_default(monkeypatch):
    fake = _Counter()
    monkeypatch.setattr(metrics_module, "security_state_degraded_total", fake)

    record_degradation("rate_limit", "memory")

    assert fake.captured == {"control": "rate_limit", "fallback": "memory"}


def test_a_raising_metric_is_swallowed_so_auth_never_fails_on_it(monkeypatch, caplog):
    class _Exploding:
        def labels(self, **kwargs):
            raise RuntimeError("metrics backend down")

    monkeypatch.setattr(metrics_module, "security_state_degraded_total", _Exploding())

    with caplog.at_level("DEBUG", logger="app.auth.degradation"):
        record_degradation("oidc_state", "local")  # must not raise

    assert any("Could not record security degradation metric" in r.message for r in caplog.records)


@pytest.mark.parametrize("module", ["session", "lockout", "rate_limit", "revocation_fallback"])
def test_every_auth_module_uses_the_shared_helper(module):
    import importlib

    mod = importlib.import_module(f"app.auth.{module}")
    assert mod.record_degradation is record_degradation
    assert not hasattr(mod, "_record_degradation")
