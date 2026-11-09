"""The API's database connections say so in ``pg_stat_activity.application_name``."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[3]


def test_setting_the_name_reaches_the_engine_connect_args():
    done = subprocess.run(
        [sys.executable, "-c", "from app.db.base import connect_args; print(connect_args)"],
        env={**os.environ, "DB_APPLICATION_NAME": "opentranscribe-api", "PYTHONPATH": "."},
        cwd=_REPO / "backend",
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert "'application_name': 'opentranscribe-api'" in done.stdout


def test_compose_names_only_the_backend_service():
    services = yaml.safe_load((_REPO / "docker-compose.yml").read_text())["services"]
    named = {
        name
        for name, service in services.items()
        if any(
            str(item).startswith("DB_APPLICATION_NAME=")
            for item in (service.get("environment") or [])
        )
    }
    assert named == {"backend"}
