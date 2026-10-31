"""Every model-cache directory compose bind-mounts must be created by the installer scripts.

A bind-mount source that does not exist is created by dockerd as ``root``, after
``fix_model_cache_permissions`` has already run. ``password-blocklist`` (the breached-password
list, v0.6.0) was missing from all three ``mkdir`` lists, so a fresh install ended with it
root-owned and the rehearsal's "model cache is owned by UID 1000 after start" check failed. The
set is derived from ``docker-compose.yml`` so the next mounted directory is covered too.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = (
    "opentranscribe.sh",
    "scripts/common.sh",
    "setup-opentranscribe.sh",
    "scripts/release-tests/test-lite-mode.sh",
)


def _mounted_cache_dirs() -> set[str]:
    text = (REPO / "docker-compose.yml").read_text()
    return set(re.findall(r"\$\{MODEL_CACHE_DIR:-\./models\}/([A-Za-z0-9_-]+):", text))


def test_the_derivation_finds_the_known_mounts():
    dirs = _mounted_cache_dirs()

    assert {"huggingface", "password-blocklist"} <= dirs, dirs


@pytest.mark.parametrize("script", SCRIPTS)
def test_every_mounted_model_cache_dir_is_precreated(script):
    # Fold `\`-continued lines first: the opentranscribe.sh mkdir spans three physical lines.
    logical = (REPO / script).read_text().replace("\\\n", " ")
    block = " ".join(line for line in logical.splitlines() if "mkdir -p" in line)
    missing = sorted(d for d in _mounted_cache_dirs() if d not in block)

    assert missing == [], f"{script} does not pre-create: {missing}"
