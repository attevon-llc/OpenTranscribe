"""``scripts/test-lan-install.sh`` argument parsing: a value flag with no value must fail fast.

``shift 2`` fails without shifting when one argument is left, so the parser spun forever
on a trailing ``--ca`` (found by running it under ``timeout``).
"""

from __future__ import annotations

import subprocess

import pytest

from tests.unit._shell_extract import REPO_ROOT

pytestmark = pytest.mark.unit

SCRIPT = REPO_ROOT / "scripts" / "test-lan-install.sh"
VALUE_FLAGS = ["--host", "--ca", "--https-port", "--http-port", "--plain-app-port"]


@pytest.mark.parametrize("flag", VALUE_FLAGS)
def test_trailing_value_flag_is_rejected_not_looped_on(flag):
    result = subprocess.run(
        ["bash", str(SCRIPT), "--expect-untrusted", flag],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 2
    assert f"{flag} needs a value" in result.stderr
