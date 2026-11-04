"""The dev stack must not let the seeded admin's password expire.

The dev ``admin@example.com`` is long-lived seeded data. Under the production 60-day password
expiry its ``password_changed_at`` aged out, the next login set ``must_change_password``, and
every API call then returned ``403 password_change_required`` -- which failed the unit/API,
integration and e2e phases of the release gate at once (all sign in as that account).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
OVERRIDE = REPO_ROOT / "docker-compose.override.yml"

pytestmark = pytest.mark.skipif(
    not OVERRIDE.is_file(), reason="docker-compose.override.yml is not present in this checkout"
)


def test_dev_backend_disables_password_expiry_by_default() -> None:
    compose = yaml.safe_load(OVERRIDE.read_text(encoding="utf-8"))
    env = compose["services"]["backend"]["environment"]
    value = str(env.get("PASSWORD_MAX_AGE_DAYS", ""))
    assert value == "${DEV_PASSWORD_MAX_AGE_DAYS:-0}", (
        f"dev backend must default PASSWORD_MAX_AGE_DAYS to 0 (expiry off), got {value!r}"
    )
