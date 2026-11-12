"""Issue #791: the monitoring overlay is wired for alerting, not just scraping.

The rule semantics are proven by ``promtool test rules``
(``scripts/check-prometheus-rules.sh``); these tests pin the wiring around them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
PROM = REPO_ROOT / "monitoring" / "prometheus"


def _yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_prometheus_loads_rules_and_talks_to_alertmanager():
    cfg = _yaml(PROM / "prometheus.yml")
    assert cfg["rule_files"] == ["/etc/prometheus/rules/*.yml"]
    targets = cfg["alerting"]["alertmanagers"][0]["static_configs"][0]["targets"]
    assert targets == ["alertmanager:9093"]
    jobs = {j["job_name"] for j in cfg["scrape_configs"]}
    assert {
        "opentranscribe-backend",
        "opentranscribe-workers",
        "node-exporter",
        "alertmanager",
    } <= jobs


def test_worker_scrape_targets_the_worker_metrics_port():
    cfg = _yaml(PROM / "prometheus.yml")
    job = next(j for j in cfg["scrape_configs"] if j["job_name"] == "opentranscribe-workers")
    targets = job["static_configs"][0]["targets"]
    assert targets and all(t.endswith(":9808") for t in targets)


def test_every_alert_has_a_firing_and_a_quiet_promtool_case():
    alerts = {
        r["alert"]
        for g in _yaml(PROM / "rules" / "opentranscribe.yml")["groups"]
        for r in g["rules"]
    }
    cases = _yaml(PROM / "tests" / "opentranscribe_test.yml")["tests"]
    firing: set[str] = set()
    quiet: set[str] = set()
    for case in cases:
        for t in case["alert_rule_test"]:
            (firing if t["exp_alerts"] else quiet).add(t["alertname"])
    assert firing == alerts, f"alerts without a firing case: {alerts - firing}"
    assert quiet == alerts, f"alerts without a quiet case: {alerts - quiet}"


def test_required_signals_are_alerted_on():
    text = (PROM / "rules" / "opentranscribe.yml").read_text(encoding="utf-8")
    for expr in (
        "audit_outbox_stuck_events > 0",
        "security_state_degraded_total",
        "celery_queue_depth",
        "node_filesystem_avail_bytes",
        'up{job="opentranscribe-backend"} == 0',
        "alert: BackupStale",
        "backup_last_success_timestamp_seconds",
        "alert: HttpServerErrorRate",
        'http_requests_total{status=~"5.."}',
    ):
        assert expr in text


def test_overlay_images_are_pinned_by_digest_and_alertmanager_is_wired():
    overlay = _yaml(REPO_ROOT / "docker-compose.monitoring.yml")
    for name in ("alertmanager", "node-exporter"):
        assert re.search(r"@sha256:[0-9a-f]{64}$", overlay["services"][name]["image"]), name
    am = overlay["services"]["alertmanager"]
    assert all(p.startswith("127.0.0.1:") for p in am.get("ports", []))
    assert "alertmanager_webhook_token" in am["secrets"]
    assert overlay["secrets"]["alertmanager_webhook_token"] == {
        "environment": "ALERTMANAGER_WEBHOOK_TOKEN"
    }


def test_alertmanager_webhook_sends_a_bearer_credential_to_the_internal_path():
    cfg = _yaml(REPO_ROOT / "monitoring" / "alertmanager" / "alertmanager.yml")
    hook = cfg["receivers"][0]["webhook_configs"][0]
    assert hook["url"] == "http://backend:8080/internal/alertmanager/webhook"
    auth = hook["http_config"]["authorization"]
    assert auth["type"] == "Bearer" and auth["credentials_file"].startswith("/run/secrets/")


def test_dependabot_compose_ecosystem_covers_the_monitoring_overlay():
    """The `docker-compose` ecosystem at `/` matches every docker-compose.*.yml, overlays included."""
    deps = _yaml(REPO_ROOT / ".github" / "dependabot.yml")["updates"]
    assert any(u["package-ecosystem"] == "docker-compose" and u["directory"] == "/" for u in deps)
