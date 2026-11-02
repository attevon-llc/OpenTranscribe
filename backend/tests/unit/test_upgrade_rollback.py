"""The ``rollback`` arm of opentranscribe.sh: refuses without a target or an unsafe head, proceeds when safe."""

from __future__ import annotations

from pathlib import Path

from tests.unit._shell_extract import MANAGER
from tests.unit._shell_extract import extract_case_block
from tests.unit._shell_extract import requires_install_scripts
from tests.unit._shell_extract import run_shell

pytestmark = requires_install_scripts


def test_rollback_refuses_without_a_recorded_target(tmp_path: Path):
    """Behavioral: `update --rollback` against a .env with no recorded target.

    Runs the REAL `update)` case body (extracted, not re-typed) with
    `check_environment`/`fix_model_cache_permissions` stubbed — the real
    versions require a live `.env`/`docker-compose.yml` and can shell out to
    Docker to fix cache ownership, neither of which this fast unit test should
    do. It is still exercising the real do_rollback branch: with `set --
    update --rollback` and no `# OT_PREVIOUS_IMAGE_TAG` in .env, execution
    must reach `exit 1` before ever calling `compose_down_for_upgrade` (not
    stubbed here — an uncaught call to it would itself fail the snippet).
    """
    (tmp_path / ".env").write_text("OT_IMAGE_TAG=v0.5.0\n")
    body = extract_case_block(MANAGER, r"    update)", r"    update-full)")
    snippet = f"""
YELLOW='\\033[1;33m'; GREEN='\\033[0;32m'; RED='\\033[0;31m'; BLUE='\\033[0;34m'; NC='\\033[0m'
cd {tmp_path}
check_environment() {{ :; }}
fix_model_cache_permissions() {{ :; }}
set -- update --rollback
{body}
"""
    out = run_shell(snippet)
    assert "No previous version recorded" in out
    assert "update --version vX.Y.Z" in out


def _rollback_snippet(tmp_path: Path, body: str, docker_stub: str, extra_args: str = "") -> str:
    """Shared scaffolding for the issue #610 Part B rollback-preflight tests below.

    Stubs everything the real `update)` case body calls before/around the preflight so
    it needs neither a live stack nor a real backend image: `get_compose_files`
    (defined elsewhere in opentranscribe.sh, invisible to this extracted snippet),
    `preflight_upgrade_env`, and `compose_down_for_upgrade` — the last one echoes a
    marker and exits immediately, so reaching it (or not) is the observable signal for
    whether the preflight refused.
    """
    return f"""
YELLOW='\\033[1;33m'; GREEN='\\033[0;32m'; RED='\\033[0;31m'; BLUE='\\033[0;34m'; NC='\\033[0m'
cd {tmp_path}
check_environment() {{ :; }}
fix_model_cache_permissions() {{ :; }}
get_compose_files() {{ echo "-f docker-compose.yml"; }}
preflight_upgrade_env() {{ :; }}
compose_down_for_upgrade() {{ echo "REACHED_TEARDOWN"; exit 0; }}
{docker_stub}
set -- update --rollback {extra_args}
{body}
"""


def test_rollback_refuses_when_target_image_does_not_know_the_live_head(tmp_path: Path):
    """Issue #610 Part B — the mirror-image gap: `update --rollback` swaps in an OLDER

    image without checking whether its migration chain can even read the CURRENT
    schema. Without this preflight, Alembic aborts with a cryptic "Can't locate
    revision identified by '<head>'" only after the stack is already torn down.

    `docker` is stubbed as a bash function (functions shadow PATH binaries for
    unqualified calls) so this needs no live stack and no real backend image:
    `docker compose ... exec ... psql` returns a fake live head, `docker run ...`
    (the "does the target image know this revision" check) returns 1 — grep found
    nothing, i.e. the target image does NOT know it.
    """
    (tmp_path / ".env").write_text("OT_IMAGE_TAG=v0.5.0\n# OT_PREVIOUS_IMAGE_TAG=v0.4.1\n")
    body = extract_case_block(MANAGER, r"    update)", r"    update-full)")
    docker_stub = """
docker() {
  if [ "$1" = "compose" ]; then
    echo "v393_add_overlap_timing_columns"
    return 0
  elif [ "$1" = "run" ]; then
    return 1
  fi
  return 0
}
"""
    out = run_shell(_rollback_snippet(tmp_path, body, docker_stub))
    assert "Refusing to roll back" in out, out
    assert "does not know" in out, out
    assert "REACHED_TEARDOWN" not in out, (
        f"must refuse BEFORE compose_down_for_upgrade tears the stack down: {out}"
    )


def test_rollback_proceeds_when_target_image_knows_the_live_head(tmp_path: Path):
    """Positive control for the test above: same setup, but the target image DOES know

    the live head (`docker run`'s grep succeeds) — rollback must proceed to teardown,
    not just "the preflight never fires at all" (which would pass the negative test
    for the wrong reason too).
    """
    (tmp_path / ".env").write_text("OT_IMAGE_TAG=v0.5.0\n# OT_PREVIOUS_IMAGE_TAG=v0.4.1\n")
    body = extract_case_block(MANAGER, r"    update)", r"    update-full)")
    docker_stub = """
docker() {
  if [ "$1" = "compose" ]; then
    echo "v393_add_overlap_timing_columns"
    return 0
  elif [ "$1" = "run" ]; then
    return 0
  fi
  return 0
}
"""
    out = run_shell(_rollback_snippet(tmp_path, body, docker_stub))
    assert "Refusing to roll back" not in out, out
    assert "REACHED_TEARDOWN" in out, f"expected the rollback to proceed to teardown: {out}"


def test_rollback_force_downgrade_skips_the_preflight_check(tmp_path: Path):
    """`--force-downgrade` is the documented override — it must bypass this preflight

    entirely (not just the pre-existing semver-ordering guard it already overrides).
    `docker` is stubbed to fail loudly if called at all, so any call — the live-head
    read or the target-image check — fails this test.
    """
    (tmp_path / ".env").write_text("OT_IMAGE_TAG=v0.5.0\n# OT_PREVIOUS_IMAGE_TAG=v0.4.1\n")
    body = extract_case_block(MANAGER, r"    update)", r"    update-full)")
    docker_stub = """
docker() { echo "DOCKER_SHOULD_NOT_BE_CALLED_UNDER_FORCE_DOWNGRADE"; return 1; }
"""
    out = run_shell(_rollback_snippet(tmp_path, body, docker_stub, extra_args="--force-downgrade"))
    assert "DOCKER_SHOULD_NOT_BE_CALLED_UNDER_FORCE_DOWNGRADE" not in out, out
    assert "Refusing to roll back" not in out, out
    assert "REACHED_TEARDOWN" in out, f"expected the rollback to proceed to teardown: {out}"
