"""Runs opentranscribe.sh's REAL ``update`` command with every docker-facing step stubbed.

Shared by the tests of what ``update --version`` / ``--rollback`` decide (which tag is pinned,
which move is refused), so each test drives the extracted functions instead of re-typing them.
"""

from __future__ import annotations

from pathlib import Path

from tests.unit._shell_extract import MANAGER
from tests.unit._shell_extract import extract_functions
from tests.unit._shell_extract import run_shell

UPDATE_FUNCTIONS = [
    "read_env_value",
    "update_parse_args",
    "update_resolve_rollback_target",
    "update_refuse_unforced_downgrade",
    "sed_env_inplace",
    "append_env_line",
    "update_pin_image_tag",
    "update_check_rollback_compat",
    "run_upgrade_sequence",
    "cmd_update",
]


def run_update(tmp_path: Path, env: str, args: str) -> tuple[str, str]:
    """Write ``env`` as .env, run ``update <args>``; return (output, the OT_IMAGE_TAG pinned after)."""
    (tmp_path / ".env").write_text(env)
    snippet = f"""
YELLOW=''; GREEN=''; RED=''; BLUE=''; NC=''
cd {tmp_path}
check_environment() {{ :; }}
fix_model_cache_permissions() {{ :; }}
apply_deployment_pins() {{ :; }}
get_compose_files() {{ echo "-f docker-compose.yml"; }}
preflight_upgrade_env() {{ :; }}
compose_pull_for_upgrade() {{ :; }}
compose_down_for_upgrade() {{ :; }}
perform_phased_restart() {{ :; }}
show_access_info() {{ :; }}
docker() {{ return 0; }}
set -- update {args}
{extract_functions(MANAGER, UPDATE_FUNCTIONS)}
cmd_update "$@"
"""
    out = run_shell(snippet)
    pinned = [
        line.removeprefix("OT_IMAGE_TAG=")
        for line in (tmp_path / ".env").read_text().splitlines()
        if line.startswith("OT_IMAGE_TAG=")
    ]
    return out, pinned[-1] if pinned else ""
