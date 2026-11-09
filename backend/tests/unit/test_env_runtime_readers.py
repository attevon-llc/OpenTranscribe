"""Runtime environment knobs read through ``app.core.env`` still honour their variable and default."""

from __future__ import annotations

import pytest

from app.core.env import celery_visibility_timeout_s
from app.core.env import testing_mode
from app.core.task_cancellation import _cancel_ttl_seconds
from app.utils.hf_hub_offline import hf_offline_requested


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, False), ("", False), ("true", True), ("TRUE", True), ("1", False)],
)
def test_testing_mode_follows_the_testing_variable(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: bool
) -> None:
    if value is None:
        monkeypatch.delenv("TESTING", raising=False)
    else:
        monkeypatch.setenv("TESTING", value)
    assert testing_mode() is expected


@pytest.mark.unit
@pytest.mark.parametrize(("value", "expected"), [(None, 21600), ("900", 900), ("junk", 21600)])
def test_visibility_timeout_follows_its_variable_and_default(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: int
) -> None:
    if value is None:
        monkeypatch.delenv("CELERY_VISIBILITY_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("CELERY_VISIBILITY_TIMEOUT", value)
    assert celery_visibility_timeout_s() == expected
    assert _cancel_ttl_seconds() == expected


@pytest.mark.unit
@pytest.mark.parametrize(("value", "expected"), [(None, False), ("1", True), ("0", False)])
def test_hf_offline_follows_hf_hub_offline(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: bool
) -> None:
    if value is None:
        monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    else:
        monkeypatch.setenv("HF_HUB_OFFLINE", value)
    assert hf_offline_requested() is expected
