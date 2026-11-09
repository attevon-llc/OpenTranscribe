"""``GET /admin/engine-settings/metrics`` says plainly that nothing is collected.

The route read ``engine:metrics:<hostname>`` Redis hashes that no worker ever wrote, so it
answered ``{}`` forever, which an admin reads as "no worker is reporting right now". The
frontend never calls it, so the fix is an honest, documented empty state rather than a
second metrics system next to Prometheus.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.api.endpoints import engine_settings

pytestmark = pytest.mark.unit

_APP = Path(engine_settings.__file__).resolve().parents[2]


def test_response_states_that_metrics_are_not_collected():
    result = engine_settings.get_engine_metrics(current_user=MagicMock())

    assert result["metrics_collected"] is False
    assert result["workers"] == {}
    assert "Prometheus" in result["detail"]


def test_answer_does_not_depend_on_redis():
    """The old reader answered 503 when Redis was down; with nothing to read it must not."""
    with patch("app.core.redis.get_request_redis", side_effect=RuntimeError("must not be read")):
        result = engine_settings.get_engine_metrics(current_user=MagicMock())

    assert result["metrics_collected"] is False


def test_no_code_in_app_publishes_engine_metrics_keys():
    """If a writer for ``engine:metrics:`` is ever added, this route must read it again.

    The honest empty state is only true while nothing publishes those keys; this fails the
    day one does, instead of letting the route keep claiming nothing is collected.
    """
    publishers = [
        str(path.relative_to(_APP))
        for path in _APP.rglob("*.py")
        if "engine:metrics:" in path.read_text(encoding="utf-8")
        and path != Path(engine_settings.__file__).resolve()
    ]

    assert publishers == []
