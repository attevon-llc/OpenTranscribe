"""Issue #791: Docker's backend healthcheck probes readiness, not bare liveness."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_backend_healthcheck_probes_health_ready():
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    test = compose["services"]["backend"]["healthcheck"]["test"]
    assert test[-1] == "http://localhost:8080/health/ready"
    assert test[2] == "-sf", "curl must fail on the 503 readiness returns"
