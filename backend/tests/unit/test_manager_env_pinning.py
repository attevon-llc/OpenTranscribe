"""opentranscribe.sh edits .env without corrupting it or deleting the operator's files.

``update_pin_image_tag`` appended with ``echo >> .env``: a hand-edited .env whose last line has
no trailing newline got ``OT_IMAGE_TAG=`` glued onto the previous value. It also finished with
``rm -f .env.bak`` and so deleted a backup the operator kept under that name. The functions
are extracted from the real script and run in a temp directory.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.unit._shell_extract import extract_function

pytestmark = pytest.mark.unit

MANAGER = Path(__file__).resolve().parents[3] / "opentranscribe.sh"
FUNCS = ("sed_env_inplace", "append_env_line", "update_pin_image_tag")


def _pin(tmp_path: Path, env_text: str) -> None:
    (tmp_path / ".env").write_text(env_text)
    source = "\n".join(extract_function(MANAGER, name) for name in FUNCS)
    script = "\n".join(
        [
            "GREEN=; YELLOW=; NC=",
            "UPDATE_CURRENT_TAG=v0.6.1; UPDATE_TARGET_VERSION=v0.7.0",
            source,
            "update_pin_image_tag",
        ]
    )
    result = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_pin_after_an_unterminated_last_line_keeps_both_keys(tmp_path):
    _pin(tmp_path, "FLOWER_PASSWORD=secret")
    lines = (tmp_path / ".env").read_text().splitlines()
    assert lines == [
        "FLOWER_PASSWORD=secret",
        "# OT_PREVIOUS_IMAGE_TAG=v0.6.1",
        "OT_IMAGE_TAG=v0.7.0",
    ]


def test_pin_leaves_an_operator_env_bak_alone(tmp_path):
    (tmp_path / ".env.bak").write_text("older copy\n")
    _pin(tmp_path, "OT_IMAGE_TAG=v0.6.1\n# OT_PREVIOUS_IMAGE_TAG=v0.6.0\n")
    assert (tmp_path / ".env.bak").read_text() == "older copy\n"
    assert (tmp_path / ".env").read_text() == (
        "OT_IMAGE_TAG=v0.7.0\n# OT_PREVIOUS_IMAGE_TAG=v0.6.1\n"
    )
    assert [p.name for p in tmp_path.iterdir() if "ot-sed" in p.name] == []


def _update(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / ".env").write_text("OT_IMAGE_TAG=v0.6.1\n")
    funcs = (
        "update_parse_args",
        "sed_env_inplace",
        "append_env_line",
        "update_refuse_unforced_downgrade",
        "update_pin_image_tag",
        "cmd_update",
    )
    script = "\n".join(
        [
            "set -e",
            "GREEN=; YELLOW=; RED=; NC=",
            "check_environment() { :; }",
            "fix_model_cache_permissions() { :; }",
            'read_env_value() { grep "^$1=" .env | cut -d= -f2; }',
            "apply_deployment_pins() { :; }",
            "get_compose_files() { echo x; }",
            "run_upgrade_sequence() { echo REACHED_UPGRADE; }",
            "show_access_info() { :; }",
            *(extract_function(MANAGER, name) for name in funcs),
            'cmd_update update "$@"',
        ]
    )
    return subprocess.run(
        ["bash", "-c", script, "bash", *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )


def test_update_version_without_a_value_is_reported(tmp_path):
    result = _update(tmp_path, "--version")
    assert result.returncode == 2
    assert "--version needs a value" in result.stdout


@pytest.mark.parametrize("bad", ["v1|x", "v1&2", "v1'; touch pwned; '", "v1 2"])
def test_update_version_refuses_characters_that_can_not_be_in_a_tag(tmp_path, bad):
    result = _update(tmp_path, "--version", bad)
    assert result.returncode == 2
    assert "REACHED_UPGRADE" not in result.stdout
    assert (tmp_path / ".env").read_text() == "OT_IMAGE_TAG=v0.6.1\n"
    assert not (tmp_path / "pwned").exists()


def test_update_version_accepts_a_release_candidate_tag(tmp_path):
    result = _update(tmp_path, "--version", "v0.7.0-rc.1")
    assert result.returncode == 0, result.stderr
    assert "REACHED_UPGRADE" in result.stdout
